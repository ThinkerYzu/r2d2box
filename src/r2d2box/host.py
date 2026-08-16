"""The registry of topics and sessions, and the sweeper that retires idle ones.

`AgentHost` is the single object a host application constructs and holds. It
knows nothing about HTTP: the router in `router.py` is built on it, but a cron
job or a CLI can drive an agent through this class alone, which is the whole
reason the two are separate modules.

A **topic** is the caller's key string for the thing being talked about — a
bug, for bzdash; a project, for agent-desktop-env — and sessions live under it
(SPEC D5). r2d2box gives the topic no meaning of its own beyond scoping session
names and transcripts.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .config import AgentConfig
from .proxy import AgentProxy
from .session import BuildPrompt, Session
from .store import MemoryTranscriptStore, SessionInfo, TranscriptStore

_log = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT_S = 20 * 60
DEFAULT_PENDING_EVICT_CAP_S = 4 * 60 * 60
DEFAULT_SWEEP_INTERVAL_S = 60

# Returns the `AgentConfig` for one session, and is called at every spawn
# rather than once at mount (DESIGN Decision 5) — a system prompt built from a
# live database row is only current at the moment the process starts. May be
# sync or async.
AgentConfigCallback = Callable[[str, str], AgentConfig | Awaitable[AgentConfig]]

# The turn a conversation opens with, or None for one that starts empty. Called
# as `opening_prompt(topic, session)` once per new session and never for one
# that is resuming, and may be sync or async. The string is sent to the agent
# unchanged — it is the host's own words, not a person's question, so
# `build_prompt` does not see it.
OpeningPrompt = Callable[[str, str], str | None | Awaitable[str | None]]


class AgentHost:
    """Every conversation a host application is running, grouped by topic.

    Construct one, keep it for the process's lifetime, and close it on
    shutdown. `session()` is the main entry point: it returns the live
    `Session` for a `(topic, name)` pair, creating one if there is none, and
    the transcript in the store means a session recreated after an eviction or
    a restart continues rather than starts over.

    Sessions are created here and never expire from the registry. An idle
    sweep stops the agent-proxy process behind a session but keeps the session
    itself, which costs a few hundred bytes and keeps its claude session id —
    the one piece of state a resume needs.
    """

    def __init__(
        self,
        agent_config: AgentConfigCallback,
        *,
        build_prompt: BuildPrompt | None = None,
        opening_prompt: OpeningPrompt | None = None,
        store: TranscriptStore | None = None,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        pending_evict_cap_s: float = DEFAULT_PENDING_EVICT_CAP_S,
    ) -> None:
        self.store = store if store is not None else MemoryTranscriptStore()
        self.idle_timeout_s = idle_timeout_s
        self.pending_evict_cap_s = pending_evict_cap_s

        self._agent_config = agent_config
        self._build_prompt = build_prompt
        self._opening_prompt = opening_prompt
        self._sessions: dict[tuple[str, str], Session] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None

    async def session(self, topic: str, name: str) -> Session:
        """The live session for `(topic, name)`, created if this is the first ask.

        Creating one costs nothing and starts no process — that waits for the
        first `submit` — so a client may attach to a session that has never
        run, and a host may hand out a session id before anything is said in
        it. The exception is a host with an `opening_prompt`, which is started
        here for a conversation that turns out to be new; it runs in the
        background, so this still returns without waiting for an agent.
        """
        async with self._lock:
            session = self._sessions.get((topic, name))
            if session is not None:
                return session
            session = self._make(topic, name)
            self._sessions[(topic, name)] = session

        # Outside the lock: the opening reads the store and may submit, and
        # both would deadlock against a registry lock still held here.
        if self._opening_prompt is not None:
            session.open_with(lambda: self._open_session(session))
        return session

    async def _open_session(self, session: Session) -> None:
        """Ask the host what this conversation opens with, and submit it.

        Runs once, for a session the registry has just built. Nothing happens
        for a conversation that is resuming: the stored transcript is what
        tells the two apart, and it is the only thing that can — a session id
        looks the same either way, and a `Session` object is rebuilt from
        scratch for a conversation the host restarted under.

        A failure here is reported to whoever is attached rather than only
        logged. The reader would otherwise face an agent that quietly never
        received the briefing the host meant it to have, which is the same
        failure `_assemble_prompt` refuses to let a broken `build_prompt`
        cause.
        """
        assert self._opening_prompt is not None
        try:
            if await self.store.read_turns(session.topic, session.name):
                return
            prompt = self._opening_prompt(session.topic, session.name)
            if inspect.isawaitable(prompt):
                prompt = await prompt
            if not prompt:
                return
            await session.submit(prompt, assemble=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.exception(
                "session %s/%s: the opening prompt failed", session.topic, session.name
            )
            await session.report_error(f"the conversation could not be started: {exc}")

    def _make(self, topic: str, name: str) -> Session:
        """Build a `Session` wired to this host's callbacks and store.

        The caller holds `_lock`. The spawn closure is what keeps `session.py`
        clear of `AgentConfig`: it resolves the host's per-session
        configuration at the moment a process is started, which is every
        respawn and not only the first (DESIGN Decision 5).
        """

        async def spawn(resume: str | None) -> AgentProxy:
            config = self._agent_config(topic, name)
            if inspect.isawaitable(config):
                config = await config
            return await self.start_proxy(config, resume, f"{topic}/{name}")

        return Session(
            topic,
            name,
            spawn=spawn,
            store=self.store,
            build_prompt=self._build_prompt,
        )

    async def start_proxy(
        self, config: AgentConfig, resume: str | None, tag: str
    ) -> AgentProxy:
        """Spawn one agent-proxy for a session that is starting or restarting.

        The one place the registry touches a subprocess, so a subclass that
        overrides it can run a whole host with no `agent-proxy` on the machine
        — which is how the registry is tested, and how a host with an unusual
        launcher (a container, a remote worker) plugs one in. `tag` is the
        `topic/session` label that appears in the agent's log lines.
        """
        return await AgentProxy.start(config, resume, tag=tag)

    def live_sessions(self, topic: str | None = None) -> list[Session]:
        """Sessions this host is holding, for `topic` or across all of them.

        Includes sessions with no process running: eviction stops the agent
        but keeps the session.
        """
        return [
            session
            for (session_topic, _), session in self._sessions.items()
            if topic is None or session_topic == topic
        ]

    async def list_sessions(self, topic: str) -> list[SessionInfo]:
        """Every session under `topic`, stored or live, most recently active first.

        This is what a host's session picker lists (DESIGN Decision 3), so it
        has to cover both halves: a session whose transcript is on disk but
        whose process was evicted, and a session created a moment ago that has
        not stored a turn yet. A session in both is reported once, with the
        live `last_active`, which is the fresher of the two.
        """
        by_name = {info.session: info for info in await self.store.list_sessions(topic)}

        # A session's `last_active` is monotonic, because eviction has to be
        # immune to the clock being set; `SessionInfo.last_active` is epoch,
        # because a picker shows it to a person. How long ago it was is the
        # one reading both agree on, so that is what converts between them.
        now_epoch, now_monotonic = time.time(), time.monotonic()
        for session in self.live_sessions(topic):
            idle = now_monotonic - session.last_active
            by_name[session.name] = SessionInfo(
                session=session.name, last_active=now_epoch - idle
            )
        return sorted(by_name.values(), key=lambda info: info.last_active, reverse=True)

    async def create_session(self, topic: str) -> Session:
        """A new session under `topic`, with a name nothing else is using."""
        return await self.session(topic, uuid.uuid4().hex[:12])

    async def close_session(self, topic: str, name: str, *, clear: bool = False) -> bool:
        """Drop a session from the registry, stopping its agent. True if there was one.

        With `clear`, the transcript goes too — the `DELETE /sessions` case,
        where the point is that the conversation is over rather than merely
        idle. Without it, asking for the same `(topic, name)` again resumes
        what was said.
        """
        async with self._lock:
            session = self._sessions.pop((topic, name), None)
        if session is None:
            if clear:
                await self.store.clear(topic, name)
            return False
        await session.close()
        if clear:
            await session.clear()
        return True

    async def close(self) -> None:
        """Stop every session and the sweeper. The host's shutdown path.

        Turns still running are lost rather than waited for: agent-proxy has no
        cancellation, so a turn in flight can only be abandoned, and each one is
        stored with an error outcome as its session closes.
        """
        await self.stop_sweeper()
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                await session.close()
            except Exception:
                _log.exception("failed to close session %s/%s", session.topic, session.name)

    # ---- idle eviction -------------------------------------------------------

    async def evict_idle(self) -> int:
        """Stop the agent behind every session idle past `idle_timeout_s`; count them.

        The session survives its eviction — only the process goes — so the next
        `submit` resumes the conversation with `--resume` and the client never
        learns it happened.

        A session with a turn still running is spared until
        `pending_evict_cap_s`, because a turn runs on with nobody listening
        (DESIGN Decision 8) and nothing refreshes `last_active` while it does.
        The cap is what stops that exemption becoming permanent: a turn whose
        `turn_end` never arrives would otherwise make its session immortal.
        """
        now = time.monotonic()
        due = []
        async with self._lock:
            for session in self._sessions.values():
                if not session.process_alive:
                    continue
                idle = now - session.last_active
                if idle <= self.idle_timeout_s:
                    continue
                if session.pending_turns and idle <= self.pending_evict_cap_s:
                    continue
                due.append((session, idle))

        for session, idle in due:
            _log.info(
                "evicting idle session %s/%s (idle %.1f min, %d turns pending)",
                session.topic, session.name, idle / 60.0, session.pending_turns,
            )
            try:
                await session.stop_process()
            except Exception:
                _log.exception(
                    "failed to evict session %s/%s", session.topic, session.name
                )
        return len(due)

    def start_sweeper(self, interval_s: float = DEFAULT_SWEEP_INTERVAL_S) -> None:
        """Run `evict_idle` on a loop from now until `stop_sweeper` or `close`.

        Call it from the host's startup, once there is a running event loop —
        a FastAPI lifespan handler is the usual place. Calling it twice leaves
        the first loop running and does nothing.
        """
        if self._sweeper is not None and not self._sweeper.done():
            return
        self._sweeper = asyncio.create_task(
            self._sweep_forever(interval_s), name="r2d2box-idle-sweeper"
        )

    async def stop_sweeper(self) -> None:
        """Stop the sweeper loop and wait for it to finish. Safe with none running."""
        sweeper = self._sweeper
        self._sweeper = None
        if sweeper is None:
            return
        sweeper.cancel()
        try:
            await sweeper
        except asyncio.CancelledError:
            pass

    async def _sweep_forever(self, interval_s: float) -> None:
        """Call `evict_idle` every `interval_s` until cancelled.

        A failed sweep is logged and the loop carries on: a transient error in
        one session must not leave every other session unevictable for the
        life of the process.
        """
        while True:
            try:
                await asyncio.sleep(interval_s)
                await self.evict_idle()
            except asyncio.CancelledError:
                return
            except Exception:
                _log.exception("idle sweep failed; continuing")

    async def __aenter__(self) -> AgentHost:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()
