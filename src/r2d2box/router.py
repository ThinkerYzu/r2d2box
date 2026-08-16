"""The FastAPI routes a host mounts: the WebSocket, the session REST, the assets.

`R2D2Box` is what a host application constructs — an `AgentHost` plus the
router that exposes it — and `app.include_router(box.router, prefix="/chat")`
is the whole integration. Everything above this module is transport: the
conversation, its turns and its fan-out all live in `session.py`, and a router
that started tracking turns would mean the seam is in the wrong place.

Two things here are less obvious than they look.

**A connection queues; it never waits on its peer.** `Session.attach` and every
broadcast call `Subscriber.send` while holding the session lock, so a socket
that waited for a slow browser inside `send` would stall the read pump for
every client on that session. `_ClientConnection.send` puts the message on a
bounded queue and returns; a separate writer task drains it.

**A slow client is dropped, not tolerated.** The queue has a limit, and a
client that fills it is told why and disconnected. The alternative is an
unbounded queue holding a whole conversation for a browser that stopped
reading, which turns one dead tab into the server's memory problem.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from starlette.staticfiles import StaticFiles

from .host import (
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_PENDING_EVICT_CAP_S,
    DEFAULT_SWEEP_INTERVAL_S,
    AgentConfigCallback,
    AgentHost,
    OpeningPrompt,
)
from .proxy import ProxyStartError
from .session import BuildPrompt, Session, SubmitRejected
from .store import TranscriptStore

_log = logging.getLogger(__name__)

# How many messages may wait for one client before it is disconnected.
#
# A turn's worth of messages is a few dozen; a conversation's is a few hundred.
# This is high enough that no client keeping up at all will reach it, and low
# enough that a browser that has stopped reading is noticed within one turn.
DEFAULT_CLIENT_QUEUE_LIMIT = 512

# Where the front-end lives inside the installed package: the chat
# box, its stylesheet, and the vendored `marked` and DOMPurify beside them.
_PACKAGE_STATIC_DIR = Path(__file__).parent / "static"


class _ClientConnection:
    """One WebSocket connection, and the session it currently has attached.

    Created per connection by the `/ws` endpoint, which calls `run` and then
    closes the socket. It implements `Subscriber`, so a session broadcasts to
    it like anything else — but by queueing, for the reason in the module
    docstring.

    A connection attaches to at most one session at a time. Attaching to a
    second detaches the first, which is what a client switching sessions in
    place does.
    """

    def __init__(
        self,
        websocket: WebSocket,
        host: AgentHost,
        *,
        queue_limit: int = DEFAULT_CLIENT_QUEUE_LIMIT,
    ) -> None:
        self._websocket = websocket
        self._host = host
        self._limit = queue_limit
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._session: Session | None = None
        self._closing = False

    # ---- the Subscriber side -------------------------------------------------

    async def send(self, message: dict[str, Any]) -> None:
        """Queue one server→client message. Never waits on the socket.

        Called by `Session` under its lock, so this does no I/O at all: the
        writer task owns the socket, and everything this does is hand it work.
        Raises `ConnectionError` once the connection is finished or its queue
        is full, which is how the session learns to drop this subscriber.
        """
        if not self._offer(message):
            raise ConnectionError("this client is no longer receiving")

    def _offer(self, message: dict[str, Any]) -> bool:
        """Put one message on the writer's queue; False if the connection is done.

        Overflow ends the connection rather than blocking or discarding: a
        client this far behind has stopped reading, and the messages it has
        already missed make everything after them misleading anyway.
        """
        if self._closing:
            return False
        if self._queue.qsize() >= self._limit:
            self._fail(
                f"dropped: {self._limit} messages queued and unread — "
                "reattach to get the conversation from the transcript"
            )
            return False
        self._queue.put_nowait(message)
        return True

    def _fail(self, reason: str) -> None:
        """End the connection, with one last message saying why.

        The queue is cleared first: whatever is in it is what the client was
        too slow to read, and the explanation is the only part still worth
        delivering. The `None` behind it stops the writer, which is what makes
        `run` tear the connection down.
        """
        if self._closing:
            return
        self._closing = True
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(self._connection_message("error", error=reason, fatal=True))
        self._queue.put_nowait(None)

    # ---- the connection's life -----------------------------------------------

    async def run(self) -> None:
        """Serve this connection until either side stops, then let its session go.

        The reader and the writer run as two tasks because they block on
        different things — the socket and the queue — and either ending means
        the connection is over. Detaching in the exit path is what a client
        leaving mid-turn needs: the subscription ends, the turn does not.
        """
        reader = asyncio.create_task(self._read_loop(), name="r2d2box-ws-reader")
        writer = asyncio.create_task(self._write_loop(), name="r2d2box-ws-writer")
        try:
            await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._closing = True
            await self._detach_current()
            for task in (reader, writer):
                task.cancel()
            await asyncio.gather(reader, writer, return_exceptions=True)

    async def _read_loop(self) -> None:
        """Handle client commands until the client disconnects.

        Every frame is answered somehow — a malformed one with an error rather
        than a silent drop, because the alternative is a front-end bug that
        looks like a hung server.
        """
        while True:
            try:
                frame = await self._websocket.receive()
            except (WebSocketDisconnect, RuntimeError):
                return
            if frame.get("type") == "websocket.disconnect":
                return

            command = _decode_frame(frame)
            if command is None:
                await self._error("each frame must be a JSON object")
                continue
            try:
                await self._dispatch(command)
            except Exception as exc:
                # One command failing costs that command. Letting it out of
                # here would drop the socket, so a client would answer a
                # server-side bug by reconnecting into the same one.
                _log.exception("r2d2box: command %r failed", command.get("type"))
                await self._error(f"the command failed: {exc}")

    async def _write_loop(self) -> None:
        """Send queued messages until the queue's `None` sentinel or a dead socket.

        A send that raises ends the connection: the socket is gone, and `run`
        notices this task finishing.
        """
        while True:
            message = await self._queue.get()
            if message is None:
                return
            try:
                await self._websocket.send_json(message)
            except Exception as exc:
                _log.debug("r2d2box: a client socket stopped accepting sends: %s", exc)
                return

    # ---- the four client commands --------------------------------------------

    async def _dispatch(self, command: dict[str, Any]) -> None:
        """Run one client command, answering with an error if it is not one.

        Commands are handled one at a time, in order. `submit` waits for
        agent-proxy's acknowledgement, so it holds the reader for as long as
        that takes — which is what keeps one client from starting two turns at
        once, and is invisible to a client that is only ever mid-turn.
        """
        kind = command.get("type")
        if kind == "attach":
            await self._attach(command)
        elif kind == "submit":
            await self._submit(command)
        elif kind == "status_query":
            await self._status_query()
        elif kind == "detach":
            await self._detach()
        else:
            await self._error(f"unknown command type {kind!r}")

    async def _attach(self, command: dict[str, Any]) -> None:
        """Subscribe to a session, answering with `attached`.

        With no `session` a new one is created and its name comes back in the
        `attached` envelope, so a client that only knows its topic never has to
        call the REST endpoint first. Attaching while already attached leaves
        the old session first.
        """
        topic = command.get("topic")
        if not isinstance(topic, str) or not topic:
            await self._error("attach needs a non-empty `topic`")
            return

        name = command.get("session")
        if name is None:
            session = await self._host.create_session(topic)
        elif isinstance(name, str) and name:
            session = await self._host.session(topic, name)
        else:
            await self._error("`session` must be a non-empty string, or absent for a new one")
            return

        await self._detach_current()
        try:
            await session.attach(self)
        except Exception as exc:
            _log.info("r2d2box: attach to %s/%s failed: %s", topic, session.name, exc)
            await self._error(f"could not attach to {topic}/{session.name}: {exc}")
            return
        self._session = session

    async def _submit(self, command: dict[str, Any]) -> None:
        """Run a turn for this client's text, and let the broadcast report it.

        Nothing is sent back on success: the turn is acknowledged by the
        `turn_start` every attached client receives, so the submitting tab and
        the others learn about it the same way. A refusal is this client's
        business alone and comes back as an error.
        """
        session = self._session
        if session is None:
            await self._error("attach to a session before submitting")
            return

        text = command.get("text")
        if not isinstance(text, str) or not text.strip():
            await self._error("submit needs a non-empty `text`")
            return

        try:
            await session.submit(text, command.get("context"))
        except (SubmitRejected, ConnectionError, ProxyStartError) as exc:
            _log.info(
                "r2d2box: submit to %s/%s was not accepted: %s",
                session.topic, session.name, exc,
            )
            await self._error(f"the prompt was not accepted: {exc}")
        except Exception as exc:
            # Whatever the host's `build_prompt` raises arrives here, and a
            # traceback is the only way to tell a broken hook from a refused
            # prompt.
            _log.exception("r2d2box: submit to %s/%s failed", session.topic, session.name)
            await self._error(f"the prompt failed: {exc}")

    async def _status_query(self) -> None:
        """Answer with the session's live state, for a client reconciling after a gap."""
        session = self._session
        if session is None:
            await self._error("attach to a session before asking for its status")
            return
        self._offer(await session.status())

    async def _detach(self) -> None:
        """Stop receiving this session's messages without closing the socket."""
        session = self._session
        await self._detach_current()
        if session is not None:
            self._offer({
                "type": "detached",
                "topic": session.topic,
                "session": session.name,
            })

    async def _detach_current(self) -> None:
        """Unsubscribe from whatever session this connection holds, if any."""
        session, self._session = self._session, None
        if session is not None:
            await session.detach(self)

    # ---- messages from the router rather than from the conversation -----------

    async def _error(self, text: str) -> None:
        """Tell this one client that its command failed."""
        self._offer(self._connection_message("error", error=text))

    def _connection_message(self, kind: str, **fields: Any) -> dict[str, Any]:
        """A message from the router to this connection alone.

        These carry no `seq`. The session's sequence numbers the conversation
        every attached client sees, and a complaint about one client's command
        is not part of it — so a missing `seq` is how a client tells the two
        apart.
        """
        session = self._session
        located = (
            {"topic": session.topic, "session": session.name} if session is not None else {}
        )
        return {"type": kind, "scope": "connection", **located, **fields}


class R2D2Box:
    """The agent host and the routes that expose it — what a host application mounts.

    Construct one, include its router, and wire its `lifespan`:

        box = R2D2Box(agent_config=my_config, store=FileTranscriptStore(root))
        app = FastAPI(lifespan=box.lifespan)
        app.include_router(box.router, prefix="/chat")

    That gives the application a WebSocket at `/chat/ws`, the session REST
    endpoints under `/chat/sessions`, and the front-end at `/chat/static`.

    Everything the conversation needs is `AgentHost`'s, reachable as `.host`
    for a caller that also drives agents outside a request — a cron job, a CLI
    — which is why the two are separate classes.
    """

    def __init__(
        self,
        agent_config: AgentConfigCallback | None = None,
        *,
        host: AgentHost | None = None,
        build_prompt: BuildPrompt | None = None,
        opening_prompt: OpeningPrompt | None = None,
        store: TranscriptStore | None = None,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        pending_evict_cap_s: float = DEFAULT_PENDING_EVICT_CAP_S,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
        static_dir: Path | None = None,
        client_queue_limit: int = DEFAULT_CLIENT_QUEUE_LIMIT,
    ) -> None:
        if (agent_config is None) == (host is None):
            raise ValueError(
                "R2D2Box takes either an agent_config callback or a prebuilt host"
            )
        if host is not None:
            self.host = host
        else:
            assert agent_config is not None
            self.host = AgentHost(
                agent_config,
                build_prompt=build_prompt,
                opening_prompt=opening_prompt,
                store=store,
                idle_timeout_s=idle_timeout_s,
                pending_evict_cap_s=pending_evict_cap_s,
            )

        self._sweep_interval_s = sweep_interval_s
        self._client_queue_limit = client_queue_limit
        self.router = APIRouter()
        self._add_routes()
        self._mount_static(static_dir)

    @asynccontextmanager
    async def lifespan(self, app: FastAPI | None = None) -> AsyncIterator[None]:
        """Start the idle sweeper for the application's life and close the host after it.

        Pass it to `FastAPI(lifespan=box.lifespan)`, or call `aclose` from
        whatever shutdown hook the application already has. Closing matters
        more than starting: the sweeper starts on its own with the first
        request, but nothing else terminates the agent-proxy processes.
        """
        self.host.start_sweeper(self._sweep_interval_s)
        try:
            yield
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Stop every session, every agent-proxy, and the sweeper. Safe twice."""
        await self.host.close()

    # ---- the routes ----------------------------------------------------------

    def _add_routes(self) -> None:
        """Register the WebSocket and the session REST endpoints on `self.router`.

        The REST endpoints are the host's own furniture —
        a session picker, a close button — and each is one `AgentHost` call.
        `topic` and `session` are path segments, so a host whose topic keys can
        contain a slash should reach `AgentHost` directly rather than through
        these.
        """

        @self.router.websocket("/ws")
        async def chat_socket(websocket: WebSocket) -> None:
            self.host.start_sweeper(self._sweep_interval_s)
            await websocket.accept()
            connection = _ClientConnection(
                websocket, self.host, queue_limit=self._client_queue_limit
            )
            try:
                await connection.run()
            finally:
                await _close_quietly(websocket)

        @self.router.get("/sessions/{topic}")
        async def list_sessions(topic: str) -> dict[str, Any]:
            """Every session under `topic`, most recently active first."""
            self.host.start_sweeper(self._sweep_interval_s)
            infos = await self.host.list_sessions(topic)
            return {
                "topic": topic,
                "sessions": [
                    {"session": info.session, "last_active": info.last_active}
                    for info in infos
                ],
            }

        @self.router.post("/sessions/{topic}", status_code=201)
        async def create_session(topic: str) -> dict[str, Any]:
            """A new, empty session under `topic`. No process starts until a submit."""
            self.host.start_sweeper(self._sweep_interval_s)
            session = await self.host.create_session(topic)
            return {"topic": topic, "session": session.name}

        @self.router.delete("/sessions/{topic}/{session}")
        async def close_session(topic: str, session: str) -> dict[str, Any]:
            """End a conversation for good: stop its agent and clear its transcript.

            `existed` says whether the session was live; the transcript is
            cleared either way, so deleting a session the registry had already
            forgotten still removes what it said.
            """
            existed = await self.host.close_session(topic, session, clear=True)
            return {"topic": topic, "session": session, "existed": existed}

    def _mount_static(self, static_dir: Path | None) -> None:
        """Serve the front-end at `{prefix}/static`, if the assets are there.

        A missing directory is a warning rather than an error: the server half
        is useful on its own, and a host that only wants the WebSocket — a
        cron job, a CLI, an app with its own renderer — should not be stopped
        by assets it will never ask for. `static_dir` overrides the packaged
        copy, which is how a host serves a modified box.
        """
        directory = Path(static_dir) if static_dir is not None else _PACKAGE_STATIC_DIR
        if not directory.is_dir():
            _log.warning("r2d2box: no static directory at %s; assets not served", directory)
            return
        self.router.mount(
            "/static", StaticFiles(directory=directory), name="r2d2box-static"
        )


def _decode_frame(frame: dict[str, Any]) -> dict[str, Any] | None:
    """The JSON object in one received WebSocket frame, or None if it is not one.

    Binary frames are decoded as UTF-8 rather than refused, since a client
    library that sends its JSON as bytes is not doing anything wrong.
    """
    raw = frame.get("text")
    if raw is None and frame.get("bytes") is not None:
        raw = frame["bytes"].decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return command if isinstance(command, dict) else None


async def _close_quietly(websocket: WebSocket) -> None:
    """Close a WebSocket, ignoring a peer that has already gone.

    Closing a socket the client dropped raises, and by this point the
    connection is over either way.
    """
    try:
        await websocket.close()
    except Exception:
        pass
