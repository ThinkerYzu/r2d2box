"""One conversation: its turns, its subscribers, and the process behind it.

This is `proxy.py`'s counterpart across the seam this library is built on,
where each layer can be tested without the one below it. Nothing here knows
about pipes or JSON lines —
it is handed an `AgentProxy` by a spawn callback and works in messages. That is
what lets the whole module be tested against `tests/fake_proxy.py`, with no
subprocess anywhere.

Three things the two applications this library was extracted from each got
differently, settled here in one place:

- **Every attached client sees the same stream**. A session
  has subscribers, not an owner, and `attach` hands over the transcript and the
  live state together so a late joiner misses nothing.
- **A turn outlives its client**. The read pump runs whether or not
  anyone is listening, so a turn keeps appending to the transcript while the
  browser is away.
- **The server holds the background-task set**. It is updated from
  `task_start`/`task_end` with nobody connected, so a client reconciles against
  it instead of accumulating its own and getting stuck.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from .proxy import AgentProxy, ProxyStartError
from .store import TranscriptStore, Turn

_log = logging.getLogger(__name__)

# How long a `submit` waits for its `ack`.
#
# agent-proxy answers a submit "before anything reaches the inner claude" and
# never on the pty (its API.md § Commands), so this bounds the proxy's own
# processing and not a model's thinking. A submit that waits this long has lost
# its process, not found a slow one.
ACK_TIMEOUT_S = 30.0

# The counts as they read with nothing running, for a session with no process.
_IDLE_OUTSTANDING = {"user": 0, "unowned": 0, "background": 0}


class Subscriber(Protocol):
    """A client attached to a session — in practice one WebSocket connection."""

    async def send(self, message: dict[str, Any]) -> None:
        """Deliver one server→client message. Raising detaches the subscriber."""


# Given a claude session id to resume (or None for a fresh conversation),
# start an agent-proxy for this session. The host supplies it, so `session.py`
# never sees an `AgentConfig` and never calls the host's config callback itself.
SpawnProxy = Callable[[str | None], Awaitable[AgentProxy]]

# Turns what the person typed into what agent-proxy is given. Called as
# `build_prompt(topic, session, text, context)`, and may be sync or async — a
# host that has to look something up while assembling a prompt returns a
# coroutine.
BuildPrompt = Callable[..., str | Awaitable[str]]

# Told when a session starts working and when it stops. Called as
# `on_activity(topic, session, active)` once per edge, never twice for the same
# state, and may be sync or async. It is the server side's own signal: nothing
# about it reaches a client, which learns the same thing from `turn_active` and
# `task_ids`.
ActivityCallback = Callable[..., None | Awaitable[None]]


class SubmitRejected(RuntimeError):
    """agent-proxy refused a submit, so no turn is coming for it."""


@dataclass
class _PendingSubmit:
    """A submit waiting for the `ack` that will name its turn.

    `text` is what the person typed, kept so the turn can be labelled with it
    the moment the ack claims a turn id — recording it after `submit` returns
    would race a turn that has already finished and gone to the store.

    `by_host` says the text is the host application's own words instead, and
    travels with it for the same reason: the turn it belongs to does not exist
    yet.
    """

    text: str
    future: asyncio.Future[str]
    by_host: bool = False


class Session:
    """One conversation, with at most one agent-proxy process behind it.

    Created by `AgentHost`, which owns the registry; a caller that has one uses
    `submit`, `attach`, `detach` and `snapshot`. The process starts on the
    first `submit` and can be stopped and restarted underneath a live session
    any number of times — an idle eviction, a crash — with the claude session
    id carrying the conversation across. Everything up to the last completed
    turn lives in the store rather than here.

    One event loop only. An internal lock orders `attach` against the read
    pump, which is what makes the late-joiner guarantee hold.
    """

    def __init__(
        self,
        topic: str,
        name: str,
        *,
        spawn: SpawnProxy,
        store: TranscriptStore,
        build_prompt: BuildPrompt | None = None,
        on_activity: ActivityCallback | None = None,
        claude_session_id: str | None = None,
    ) -> None:
        self.topic = topic
        self.name = name
        self.claude_session_id = claude_session_id
        self.last_active = time.monotonic()

        self._spawn = spawn
        self._store = store
        self._build_prompt = build_prompt
        self._on_activity = on_activity

        self._proxy: AgentProxy | None = None
        self._pump: asyncio.Task[None] | None = None
        self._opening: asyncio.Task[None] | None = None
        self._closed = False

        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        # Ordering for the activity signal, and the state it last reported. A
        # session starts idle, so the first edge a host hears is a real one.
        self._activity_lock = asyncio.Lock()
        self._reported_active = False
        self._subscribers: set[Subscriber] = set()

        self._seq = 0
        self._ref_counter = 0
        # This conversation's own turn numbering, and the current process's
        # turn ids translated into it. agent-proxy numbers turns per process,
        # so both are needed for a session that outlives one; see
        # `_localize_turn`. The counter is None until the store has been read.
        self._turn_counter: int | None = None
        self._proxy_turn_ids: dict[str, str] = {}
        # Submits still waiting for their `ack`, by the ref each carried.
        self._pending: dict[str, _PendingSubmit] = {}
        # Calls inside `submit`, whether or not they have reached agent-proxy
        # yet. Counted separately from `_pending` because the slow part of a
        # submit — assembling the prompt, spawning a process — happens before a
        # ref exists, and a host watching `active` should see that too.
        self._submits_in_flight = 0
        # Turns that have started and not ended, by turn id. A finished turn
        # leaves here for the store and is not kept.
        self._open_turns: dict[str, Turn] = {}
        self._task_ids: set[str] = set()
        self._outstanding: dict[str, int] = dict(_IDLE_OUTSTANDING)

    # ---- state a host or a router reads -------------------------------------

    @property
    def process_alive(self) -> bool:
        """True while an agent-proxy is running for this session."""
        return self._proxy is not None and self._proxy.alive

    @property
    def active(self) -> bool:
        """True while this session has any work in flight — the server's busy light.

        Work is a submit on its way to agent-proxy, a turn that has started and
        not ended, or a background command still running: the three things that
        outlive the request that started them. A process that is merely alive
        is not work, and neither is a client being attached — a session nobody
        has spoken to in an hour reads as idle however many tabs are watching
        it.

        Broader than `turn_active`, which is only about the composer. A
        background command left running after its turn ended blocks nothing in
        the browser but is very much still work for the machine, and that is
        the difference between the two.

        This is the pull side of the `on_activity` callback; both answer the
        same question, and a host that only ever asks at a moment of its own
        choosing needs nothing else.
        """
        return bool(self._open_turns or self._task_ids or self._submits_in_flight)

    @property
    def turn_active(self) -> bool:
        """True while any turn is running — this session's composer is disabled.

        A property of the session rather than of a connection: a turn one tab
        starts blocks the composer in every other tab attached to the same
        conversation.
        """
        return bool(self._open_turns)

    @property
    def pending_turns(self) -> int:
        """How many turns have started and not ended.

        `AgentHost.evict_idle` spares a session with any, up to
        `pending_evict_cap_s`: a turn keeps running with nobody listening, and
        nothing updates `last_active` while it does.
        """
        return len(self._open_turns)

    @property
    def open_turns(self) -> list[Turn]:
        """The turns that have started and not ended, oldest first.

        The transcript's missing tail: everything up to the last `turn_end` is
        in the store, and these are what has happened since. A caller listing
        conversations reads them to describe one whose first turn is still
        running, which the store cannot yet see.

        The `Turn` objects are the live ones, still being appended to by the
        read pump, not copies.
        """
        return sorted(self._open_turns.values(), key=lambda turn: turn.started_at)

    @property
    def task_ids(self) -> set[str]:
        """Background `run_in_background` commands still running, by task id."""
        return set(self._task_ids)

    @property
    def subscriber_count(self) -> int:
        """How many clients are attached."""
        return len(self._subscribers)

    # ---- the client-facing operations ---------------------------------------

    async def submit(self, text: str, context: Any = None, *, assemble: bool = True) -> str:
        """Run one turn for `text` and return the turn id every message of it will carry.

        Starts the agent if it is not running, so this is also how an evicted
        session comes back. `context` is the client's ride-along JSON, handed
        to the host's `build_prompt` hook; neither it nor anything the hook
        prepends is recorded as the turn's `user` text, which stays what the
        person typed. `assemble=False` skips the hook and sends `text`
        unchanged, which is what an opening prompt does — it is the host's own
        words already, and running them through the host's own prompt
        assembler would wrap them in context meant for a person's question.
        That is the whole meaning of the flag, so it also marks the turn
        `by_host`, which keeps a host's opening out of the preview a session
        listing carries.

        A session with an opening turn queues this behind it, so the
        conversation always starts where the host meant it to.

        Returns once agent-proxy has acknowledged the prompt, which is before
        the turn is typed and long before it runs — so a turn id in hand means
        the turn will happen, not that it has. Raises `SubmitRejected` if
        agent-proxy refused the prompt, `ConnectionError` if the process died
        or never answered, and `ProxyStartError` if no process could be
        started. Whatever `build_prompt` raises comes through unchanged.
        """
        if self._closed:
            raise ConnectionError(f"session {self.topic}/{self.name} is closed")

        # The session counts as working from here rather than from the ack.
        # Everything slow about a submit — waiting out an opening turn,
        # assembling the prompt, spawning a process — happens before any turn
        # exists, and a host told about activity only once the ack lands would
        # see nothing at all during the part that takes seconds.
        self._submits_in_flight += 1
        await self._settle_activity()
        try:
            return await self._run_submit(text, context, assemble)
        finally:
            self._submits_in_flight -= 1
            await self._settle_activity()

    async def _run_submit(self, text: str, context: Any, assemble: bool) -> str:
        """The body of `submit`, from the opening turn it queues behind to the ack.

        Split out so `submit` can count the whole thing as activity in one
        `try`/`finally` without burying the work two levels deeper. Everything
        the caller sees — the return value, every exception — is this method's.
        """
        await self._await_opening()
        prompt = await self._assemble_prompt(text, context) if assemble else text
        async with self._start_lock:
            proxy = await self._ensure_started()
            self._ref_counter += 1
            ref = f"r2d2-{self._ref_counter}"
            pending = _PendingSubmit(
                text=text,
                future=asyncio.get_running_loop().create_future(),
                by_host=not assemble,
            )
            self._pending[ref] = pending
            self.last_active = time.monotonic()
            try:
                await proxy.submit(prompt, ref)
            except BaseException:
                self._pending.pop(ref, None)
                raise

        try:
            return await asyncio.wait_for(pending.future, ACK_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise ConnectionError(
                f"agent-proxy did not acknowledge a prompt within {ACK_TIMEOUT_S:.0f}s"
            ) from exc
        finally:
            self._pending.pop(ref, None)

    def open_with(self, opening: Callable[[], Awaitable[None]]) -> None:
        """Run `opening` as this session's first act, before any submit it races.

        `AgentHost` calls this once, for a session that has just been created
        and has nothing stored — which is the only moment "this conversation is
        new" is knowable. The work runs as a task rather than in the caller,
        because the caller is usually a client attaching and it should not wait
        out a process spawn to be told it is attached.

        Ordering is what the task buys and a bare `create_task` would not:
        `submit` waits for this to finish, so a person who types the instant
        the panel appears still finds their question behind the opening turn
        rather than ahead of it.
        """
        if self._closed or self._opening is not None:
            return
        self._opening = asyncio.create_task(
            opening(), name=f"r2d2box-opening-{self.topic}-{self.name}"
        )

    async def _await_opening(self) -> None:
        """Block until the opening turn has been submitted, if there is one.

        A failed opening is not this caller's problem — whoever started it
        reports it — so this returns rather than raising. Called from within
        the opening itself it does nothing, which is what stops the opening's
        own `submit` waiting for the task making it.
        """
        opening = self._opening
        if opening is None or opening.done() or opening is asyncio.current_task():
            return
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError:
            if not opening.cancelled():
                raise
        except Exception:
            pass

    async def report_error(self, error: str) -> None:
        """Tell every attached client that something went wrong in this session.

        For failures the conversation itself never sees — an opening turn that
        could not start, a host's own background work — where the alternative
        is a panel that silently shows less than it should.
        """
        async with self._lock:
            await self._broadcast_locked(
                {"type": "error", "error": error, **self._envelope()}
            )

    async def attach(self, subscriber: Subscriber) -> None:
        """Subscribe a client and send it the conversation so far, in that order.

        The snapshot, the subscription and the `attached` message all happen
        under one lock, which is what makes a late joiner safe: snapshot
        outside it and a message sent in between is lost; subscribe first and
        send after and the client can see a message before the transcript that
        message belongs after.

        The price is that the read pump waits on this one send, so a transport
        implementing `Subscriber` must queue rather than wait on its peer. A
        `send` that raises here leaves the client unsubscribed and re-raises.
        """
        async with self._lock:
            self._subscribers.add(subscriber)
            stored = await self._store.read_turns(self.topic, self.name)
            try:
                await subscriber.send({"type": "attached", **self._snapshot_fields(stored)})
            except Exception:
                self._subscribers.discard(subscriber)
                raise

    async def detach(self, subscriber: Subscriber) -> None:
        """Unsubscribe a client. The conversation carries on without it."""
        async with self._lock:
            self._subscribers.discard(subscriber)

    async def snapshot(self) -> dict[str, Any]:
        """The whole conversation plus its live state, as `attached` carries it."""
        async with self._lock:
            stored = await self._store.read_turns(self.topic, self.name)
            return self._snapshot_fields(stored)

    async def status(self) -> dict[str, Any]:
        """The live state without the transcript, as the `status` message carries it."""
        async with self._lock:
            return {
                "type": "status",
                **self._position(),
                "turn_active": self.turn_active,
                "turn_ids": sorted(self._open_turns),
                "task_ids": sorted(self._task_ids),
                "process_alive": self.process_alive,
                "outstanding": dict(self._outstanding),
            }

    def _snapshot_fields(self, stored: list[Turn]) -> dict[str, Any]:
        """The `attached` payload for a transcript already read from the store.

        The caller holds `_lock` and passes what `read_turns` gave it, so the
        one awaiting step is out of the way and the state below is sampled with
        nothing able to change it.

        Stored turns come first and the turns still running follow, which is
        the order they happened in: a turn moves from `_open_turns` to the
        store at its `turn_end` and is never in both.
        """
        running = self.open_turns
        return {
            **self._position(),
            "turns": [turn.to_dict() for turn in (*stored, *running)],
            "turn_active": self.turn_active,
            "task_ids": sorted(self._task_ids),
            "process_alive": self.process_alive,
            "outstanding": dict(self._outstanding),
        }

    # ---- the process and its read pump --------------------------------------

    async def _ensure_started(self) -> AgentProxy:
        """The running agent-proxy, spawning one if this session has no live process.

        The caller holds `_start_lock`, so two submits cannot race into two
        processes for one conversation.

        A resume that fails is tried once more against the same id before the
        id is given up: the common cause is transient, and dropping a good id
        starts a second conversation on top of the first with no sign that
        anything went wrong. If the retry fails too the session starts fresh —
        a lost history beats a session that can never talk again — and
        `claude_session_id` becomes the new process's own.
        """
        if self._proxy is not None and self._proxy.alive:
            return self._proxy
        await self._stop_process()

        resume = self.claude_session_id
        attempts: list[str | None] = [resume, resume, None] if resume else [None]
        last_error: BaseException | None = None
        for attempt, resume_id in enumerate(attempts, start=1):
            try:
                proxy = await self._spawn(resume_id)
            except ProxyStartError as exc:
                last_error = exc
                _log.warning(
                    "session %s/%s: spawn attempt %d of %d failed (resume=%s): %s",
                    self.topic, self.name, attempt, len(attempts), resume_id, exc,
                )
                continue
            if resume is not None and resume_id is None:
                _log.warning(
                    "session %s/%s: could not resume %s; starting a new conversation",
                    self.topic, self.name, resume,
                )
            self._proxy = proxy
            self.claude_session_id = proxy.session_id
            # The new process numbers its turns from `t-1` again, so its ids
            # mean nothing the old mapping can answer.
            self._proxy_turn_ids.clear()
            self._pump = asyncio.create_task(
                self._read_messages(proxy), name=f"r2d2box-pump-{self.topic}-{self.name}"
            )
            return proxy

        assert last_error is not None
        raise last_error

    async def _read_messages(self, proxy: AgentProxy) -> None:
        """Consume one process's whole message stream, then close out what it left.

        Runs as a task for as long as `proxy` lives, so a turn keeps being
        recorded with no client attached. One message
        failing to handle costs that message: the loop logs it and reads on,
        because the alternative is a session that goes deaf over a single
        malformed field.
        """
        try:
            async for message in proxy.messages():
                try:
                    await self._handle(message)
                except Exception:
                    _log.exception(
                        "session %s/%s: failed to handle %s",
                        self.topic, self.name, message.get("type"),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("session %s/%s: read pump failed", self.topic, self.name)
        finally:
            await self._on_stream_end(proxy)

    async def _handle(self, message: dict[str, Any]) -> None:
        """Record one message from agent-proxy and pass it on to every client.

        The transcript is updated before the broadcast, so a client attaching
        while this runs sees the message either in its snapshot or on the wire,
        never neither and never twice.

        `_localize_turn` runs first, so everything below this line — and every
        turn id that leaves the session — is in this conversation's numbering.

        The activity signal is settled after the lock is released rather than
        inside it, so a host's callback does not hold up the clients attaching
        to this session the way a slow `Subscriber.send` does.
        """
        try:
            async with self._lock:
                self.last_active = time.monotonic()
                message = await self._localize_turn(message)
                outstanding = message.get("outstanding")
                if isinstance(outstanding, dict):
                    # Absolute, never a delta (agent-proxy API.md § The stream),
                    # so it is replaced rather than accumulated — which is what
                    # makes a session that missed messages correct again on the
                    # next one.
                    self._outstanding = dict(outstanding)

                kind = message.get("type")
                if kind == "ack":
                    # Claimed, then dropped: the ack exists to bind a ref to a
                    # turn id, and the ref is r2d2box's own bookkeeping rather
                    # than anything a client needs. What
                    # the client does need — what was asked — goes out as
                    # `turn_prompt` from inside here.
                    await self._claim_turn(message)
                    return
                if kind == "error":
                    self._reject_pending_submit(message)

                envelope = self._forward(message)
                self._record(envelope)
                await self._broadcast_locked(envelope)
                await self._retire_finished_turn(envelope)
        finally:
            # In a `finally` because the `ack` path returns early and still
            # opens a turn. An ack nobody is waiting on — a submit whose caller
            # timed out — is the one message that can start work with no submit
            # in flight to have raised the signal already.
            await self._settle_activity()

    async def _localize_turn(self, message: dict[str, Any]) -> dict[str, Any]:
        """The message with its `turn.id` replaced by this conversation's own id.

        The caller holds `_lock`. Runs before anything reads the turn id, so
        every later step — the transcript, the broadcast, `_open_turns`, the id
        `submit` returns — works in the session's numbering and never in
        agent-proxy's. A message naming no turn passes through untouched.

        agent-proxy numbers turns per process and restarts at `t-1` with every
        respawn, so its ids collide inside a conversation that outlives a
        process — which one deliberately does. The proxy's own id is kept as
        `proxy_turn_id`, since it is what the agent's log lines say.
        """
        turn = message.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            return message
        proxy_turn_id = turn["id"]
        localized = dict(message)
        localized["turn"] = {**turn, "id": await self._turn_id_for(proxy_turn_id)}
        localized["proxy_turn_id"] = proxy_turn_id
        return localized

    async def _turn_id_for(self, proxy_turn_id: str) -> str:
        """This session's id for one of the current process's turns, minted on first sight.

        The caller holds `_lock`. The same `proxy_turn_id` gives the same
        answer for as long as the process lives, and `_ensure_started` clears
        the mapping when one is replaced, so the next `t-1` is a new turn
        rather than the old one.

        The counter cannot start at zero for a session rebuilt over a stored
        transcript — a host that restarted would mint ids the store already
        holds — so the first mint seeds it from what was stored.
        """
        known = self._proxy_turn_ids.get(proxy_turn_id)
        if known is not None:
            return known
        if self._turn_counter is None:
            stored = await self._store.read_turns(self.topic, self.name)
            self._turn_counter = _highest_turn_number(stored)
        self._turn_counter += 1
        minted = f"t-{self._turn_counter}"
        self._proxy_turn_ids[proxy_turn_id] = minted
        return minted

    def _forward(self, message: dict[str, Any]) -> dict[str, Any]:
        """agent-proxy's message with r2d2box's envelope on it, ready to send.

        The message passes through unchanged and `topic`, `session` and a
        per-session `seq` are added. Two of its fields cannot survive
        as they are. agent-proxy's `seq` restarts at 1 with every process, so
        it cannot number a conversation that outlives one — it moves to
        `proxy_seq`, where a client can still read what the proxy said. `ref`
        is r2d2box's own correlation token and is dropped.
        """
        forwarded = dict(message)
        forwarded.pop("ref", None)
        proxy_seq = forwarded.pop("seq", None)
        if proxy_seq is not None:
            forwarded["proxy_seq"] = proxy_seq
        return {**forwarded, **self._envelope()}

    def _envelope(self) -> dict[str, Any]:
        """The `topic`, `session` and next `seq` for one message sent to every client.

        Every call consumes a sequence number, so this runs once per broadcast
        and a gap in `seq` is a message a client lost. Only `_broadcast_locked`
        should carry one; a message going to a single client uses `_position`
        instead, which is what keeps that promise true.
        """
        self._seq += 1
        return {"topic": self.topic, "session": self.name, "seq": self._seq}

    def _position(self) -> dict[str, Any]:
        """The `topic`, `session` and current `seq` for a message to one client.

        `attached` and `status` answer one client's question, so numbering them
        as if they were part of the stream would put a gap in every other
        client's sequence — and one in this client's too, the moment a second
        tab attaches. The `seq` they carry is the last broadcast the state they
        describe includes, so the next message a client sees is `seq + 1`.
        """
        return {"topic": self.topic, "session": self.name, "seq": self._seq}

    def _record(self, message: dict[str, Any]) -> None:
        """Fold one forwarded message into the turn table and the task set.

        The caller holds `_lock`. Nothing is dropped for being unrecognized: an
        unknown type carrying a `turn` still lands in that turn's transcript,
        which is what agent-proxy's versioning note asks of a client.
        """
        kind = message.get("type")
        if kind == "task_start":
            task_id = _task_id(message)
            if task_id is not None:
                self._task_ids.add(task_id)
        elif kind == "task_end":
            task_id = _task_id(message)
            if task_id is not None:
                self._task_ids.discard(task_id)

        turn = self._turn_for(message)
        if turn is not None:
            turn.events.append(message)

    async def _claim_turn(self, message: dict[str, Any]) -> None:
        """Open the turn an `ack` names, hand its id to the submit, and say what was asked.

        The caller holds `_lock`. An `ack` for a ref nobody is waiting on still
        opens the turn — a submit whose caller timed out is running all the
        same, and the transcript has to show it — but only a claimed turn gets
        the prompt text, since nothing else knows what was asked.

        The `turn_prompt` broadcast is that text reaching the other tabs. They
        are watching this conversation too, and nothing else in the stream
        carries the question: `ack` does not leave the server,
        `turn_start` has no room for it, and `Turn.user` is only read by a
        client that attaches later. Without this a second tab would watch an
        answer to a question it never saw. It is not recorded as an event,
        because the turn already carries the same text in `user` and a client
        replaying a transcript would otherwise draw the prompt twice.
        """
        turn_id = _turn_id(message)
        if turn_id is None:
            _log.warning("session %s/%s: ack with no turn id", self.topic, self.name)
            return
        turn = self._open_turn(turn_id, _turn_kind(message))
        pending = self._pending.get(str(message.get("ref")))
        if pending is None:
            return
        turn.user = pending.text
        turn.by_host = pending.by_host
        # Broadcast before waking the submitter, so `submit` returning means
        # every attached client has already been told what was asked. The other
        # order leaves the caller racing its own prompt onto the wire.
        await self._broadcast_locked({
            "type": "turn_prompt",
            "turn": {"id": turn_id, "kind": turn.kind},
            "text": pending.text,
            **self._envelope(),
        })
        if not pending.future.done():
            pending.future.set_result(turn_id)

    def _reject_pending_submit(self, message: dict[str, Any]) -> None:
        """Fail the submit an `error` is answering, if it is answering one.

        The caller holds `_lock`. A rejected submit produces an `error` with
        the command's `ref` and no `ack` (agent-proxy API.md § Correlating a
        command with its answer), so without this the caller would wait out
        `ACK_TIMEOUT_S` for a turn that was never going to start. A
        session-wide `error` carries no `ref` and matches nothing here.
        """
        pending = self._pending.get(str(message.get("ref")))
        if pending is not None and not pending.future.done():
            pending.future.set_exception(
                SubmitRejected(str(message.get("error", "agent-proxy rejected the prompt")))
            )

    def _turn_for(self, message: dict[str, Any]) -> Turn | None:
        """The turn a message belongs in, or None if it belongs in no transcript.

        The caller holds `_lock`. A message naming a turn opens it if it is not
        open already — an unowned turn is first heard of at its `turn_start`,
        and a `turn_end` can arrive for a turn that never started at all
        (agent-proxy API.md § The shape of one turn).

        A message with no `turn` goes to the running turn when exactly one is
        running. agent-proxy runs at most one turn at a time, so that is not a
        guess: it is where a `tool_result` that outran the proxy's turn
        accounting lands, and where the `error` opening a failure lands. With
        nothing running, or with turns queued behind the running one, it is a
        session-level event — broadcast, but recorded against no turn, because
        there is no one turn it is about.
        """
        turn_id = _turn_id(message)
        if turn_id is not None:
            return self._open_turn(turn_id, _turn_kind(message))
        if len(self._open_turns) == 1:
            return next(iter(self._open_turns.values()))
        return None

    def _open_turn(self, turn_id: str, kind: str | None) -> Turn:
        """The open turn with this id, started now if it was not open already.

        The caller holds `_lock`. `kind` fills in a turn opened by a message
        that did not carry one, and never overwrites a known kind with nothing.
        """
        turn = self._open_turns.get(turn_id)
        if turn is None:
            turn = Turn(id=turn_id, kind=kind or "user")
            self._open_turns[turn_id] = turn
        elif kind:
            turn.kind = kind
        return turn

    async def _retire_finished_turn(self, message: dict[str, Any]) -> None:
        """Move a turn to the store once its `turn_end` is recorded and sent.

        The caller holds `_lock`. Runs after the broadcast so `turn_active`
        stays true for the whole of the `turn_end` message rather than flipping
        halfway through delivering it.
        """
        if message.get("type") != "turn_end":
            return
        turn_id = _turn_id(message)
        turn = self._open_turns.pop(turn_id, None) if turn_id else None
        if turn is None:
            return
        turn.ended_at = time.time()
        turn.outcome = message.get("outcome")
        await self._store.append_turn(self.topic, self.name, turn)

    async def _on_stream_end(self, proxy: AgentProxy) -> None:
        """Close out a session whose agent-proxy has gone.

        Called from the read pump's exit path, whether the process ended
        cleanly, crashed, or was terminated by an eviction. Every turn still
        open is finished with an error outcome and stored, so a client
        attaching afterwards finds a finished conversation rather than a turn
        that never ends — and `pending_turns` falls back to zero, which is what
        stops an evicted session from being exempt from every later eviction.

        The session itself survives: the next `submit` spawns a replacement and
        resumes `claude_session_id`.
        """
        if self._proxy is not proxy:
            return
        _log.info(
            "session %s/%s: agent-proxy exited with %s",
            self.topic, self.name, proxy.returncode,
        )
        self._fail_pending_submits(ConnectionError("agent-proxy exited"))
        async with self._lock:
            for turn_id in list(self._open_turns):
                await self._end_turn_locked(turn_id, "agent-proxy exited")
            self._task_ids.clear()
            self._outstanding = dict(_IDLE_OUTSTANDING)
            await self._broadcast_locked({
                "type": "process_exited",
                "returncode": proxy.returncode,
                **self._envelope(),
            })
        await self._settle_activity()

    async def _end_turn_locked(self, turn_id: str, reason: str) -> None:
        """Finish an open turn the process never finished, and store it.

        The caller holds `_lock`. The synthetic `turn_end` has the shape
        agent-proxy emits on its own failure path — `basis` and `outcome` both
        `error` — so a client needs no separate case for a turn r2d2box ended
        on its behalf.
        """
        turn = self._open_turns.pop(turn_id, None)
        if turn is None:
            return
        ending = {
            "type": "turn_end",
            "turn": {"id": turn_id, "kind": turn.kind},
            "basis": "error",
            "outcome": "error",
            "error": reason,
            **self._envelope(),
        }
        turn.events.append(ending)
        turn.ended_at = time.time()
        turn.outcome = "error"
        await self._store.append_turn(self.topic, self.name, turn)
        await self._broadcast_locked(ending)

    def _fail_pending_submits(self, error: BaseException) -> None:
        """Wake every `submit` still waiting for an ack that is no longer coming."""
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(error)
        self._pending.clear()

    async def _broadcast_locked(self, message: dict[str, Any]) -> None:
        """Send one message to every subscriber, dropping the ones that fail.

        The caller holds `_lock`. Sends run concurrently and no subscriber can
        stop another being served: a `send` that raises costs that client its
        subscription, not the session its stream.
        """
        if not self._subscribers:
            return
        subscribers = list(self._subscribers)
        results = await asyncio.gather(
            *(subscriber.send(message) for subscriber in subscribers),
            return_exceptions=True,
        )
        for subscriber, result in zip(subscribers, results):
            if isinstance(result, BaseException):
                _log.info(
                    "session %s/%s: dropping a subscriber that failed to receive: %s",
                    self.topic, self.name, result,
                )
                self._subscribers.discard(subscriber)

    async def _settle_activity(self) -> None:
        """Tell the host if this session has crossed between working and idle.

        Called after anything that can change `active`, and never while `_lock`
        is held. The callback is the host's own code — a database write, very
        likely — and the read pump does wait for it, the way `submit` waits for
        `build_prompt`. What holding `_lock` across it would add is that
        `attach`, `status` and every other session-lock caller waited too, for
        no gain: nothing this reads needs that lock.

        `active` is sampled here rather than passed in, under a lock of its own.
        That is what makes the signal level-triggered: edges are delivered in
        the order they happened, and a flicker that starts and finishes while a
        slow callback is running collapses to nothing instead of arriving
        backwards. What the host is told is the truth at the moment it is told.

        A callback that raises costs its own notification and nothing else — a
        host's status hook must not be able to end a conversation. The one
        thing it must not do is drive this session: `submit`, `close` and
        `stop_process` all settle the signal themselves and would wait on the
        lock this notification is holding.
        """
        if self._on_activity is None:
            return
        async with self._activity_lock:
            active = self.active
            if active == self._reported_active:
                return
            self._reported_active = active
            try:
                result = self._on_activity(self.topic, self.name, active)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception(
                    "session %s/%s: the activity callback failed",
                    self.topic, self.name,
                )

    async def _assemble_prompt(self, text: str, context: Any) -> str:
        """What agent-proxy is asked, once the host's `build_prompt` hook has had `text`.

        With no hook the prompt is `text` unchanged. The hook may be sync or
        async, and whatever it raises reaches `submit`'s
        caller: a hook that fails has left out something the prompt needed —
        the document text a reader had selected, say — and sending the bare
        text would get a confident answer to a question nobody asked.
        """
        if self._build_prompt is None:
            return text
        result = self._build_prompt(self.topic, self.name, text, context)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, str) or not result:
            raise ValueError(
                f"build_prompt returned {result!r}; it must return a non-empty string"
            )
        return result

    # ---- shutdown -----------------------------------------------------------

    async def stop_process(self) -> None:
        """Terminate the agent but keep the conversation, so the next submit resumes it.

        This is what an idle eviction does. The transcript, the claude session
        id and the attached clients all survive; a turn in flight does not, and
        is stored with an error outcome on the read pump's way out.
        """
        await self._stop_process()

    async def close(self) -> None:
        """Shut the session down for good: tell every client, then drop them.

        Safe to call twice. `submit` raises `ConnectionError` afterwards. The
        transcript stays in the store, so a later `Session` with the same topic
        and name picks the conversation up where this one left it — but the
        `DELETE /sessions` path clears it straight after, which is the case the
        final `session_closed` broadcast exists for.

        A client that only watches has no other way to learn this happened: its
        socket stays healthy and its screen keeps showing a conversation the
        server no longer has. The broadcast goes out before the subscribers are
        dropped, so it is the last message of the session rather than the first
        thing nobody receives.
        """
        self._closed = True
        await self._cancel_opening()
        await self._stop_process()
        self._fail_pending_submits(ConnectionError("session closed"))
        async with self._lock:
            await self._broadcast_locked({"type": "session_closed", **self._envelope()})
            self._subscribers.clear()

    async def clear(self) -> None:
        """Discard the stored conversation, leaving the session able to start a new one."""
        await self._store.clear(self.topic, self.name)

    async def _cancel_opening(self) -> None:
        """Stop an opening turn that has not finished starting. Safe with none."""
        opening, self._opening = self._opening, None
        if opening is None or opening.done():
            return
        opening.cancel()
        try:
            await opening
        except (asyncio.CancelledError, Exception):
            pass

    async def _stop_process(self) -> None:
        """Terminate the process, if there is one, and wait for its read pump to finish.

        The pump is awaited rather than cancelled so its exit path runs: open
        turns are ended and stored and the subscribers hear about it. It ends
        on its own once `AgentProxy.close` shuts stdout, so there is nothing to
        interrupt.

        `_proxy` is cleared only afterwards, because `_on_stream_end` checks it
        to tell its own process's exit from a later one's.
        """
        proxy, pump = self._proxy, self._pump
        if proxy is not None:
            await proxy.close()
        if pump is not None and pump is not asyncio.current_task():
            try:
                await pump
            except asyncio.CancelledError:
                pass
        self._proxy, self._pump = None, None


def _turn_id(message: dict[str, Any]) -> str | None:
    """The turn id a message carries, or None for one that names no turn."""
    turn = message.get("turn")
    if not isinstance(turn, dict):
        return None
    turn_id = turn.get("id")
    return turn_id if isinstance(turn_id, str) else None


def _highest_turn_number(turns: Iterable[Turn]) -> int:
    """The largest N among turn ids shaped `t-N`, or 0 when none is.

    Seeds a session's turn numbering from the transcript it is resuming, so
    the next id it mints is one the store does not already hold. An id of any
    other shape is ignored rather than guessed at: it cannot be continued, and
    counting it would only move the collision somewhere else.
    """
    highest = 0
    for turn in turns:
        if turn.id.startswith("t-") and turn.id[2:].isdigit():
            highest = max(highest, int(turn.id[2:]))
    return highest


def _turn_kind(message: dict[str, Any]) -> str | None:
    """The turn kind a message carries — `user` or `unowned` — or None."""
    turn = message.get("turn")
    if not isinstance(turn, dict):
        return None
    kind = turn.get("kind")
    return kind if isinstance(kind, str) else None


def _task_id(message: dict[str, Any]) -> str | None:
    """The background task id a `task_start` or `task_end` carries, or None."""
    task = message.get("task")
    if not isinstance(task, dict):
        return None
    task_id = task.get("id")
    return task_id if isinstance(task_id, str) else None
