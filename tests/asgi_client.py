"""A WebSocket and HTTP client that drives an ASGI app in the test's own event loop.

The third seam, alongside `scripted_proxy.py` and `fake_proxy.py`: it stands in
for the browser the way those stand in for the agent. The app is called
directly — real routing, the real `WebSocket` object, real close codes — with
no socket, no server and no extra dependency.

    async with websocket(app, "/chat/ws") as ws:
        await ws.send_json({"type": "attach", "topic": "bug-1", "session": "s1"})
        assert (await ws.receive_json())["type"] == "attached"

Running in the caller's loop is the point rather than an economy. Starlette's
own `TestClient` runs the app on a second loop in a worker thread, and the
sessions, queues and locks these tests build belong to the first — a fake proxy
created here and awaited there is the kind of failure that reads as flakiness.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

DEFAULT_TIMEOUT_S = 5.0


@dataclass
class Response:
    """One HTTP response: the status, the headers, and the body already read."""

    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class WebSocketClosed(Exception):
    """The app closed the connection. `code` is the WebSocket close code it sent."""

    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"the server closed the connection with code {code}")
        self.code = code
        self.reason = reason


class ASGIWebSocket:
    """One WebSocket connection to an ASGI app, from the client's side.

    Built by `websocket()`, which handles the handshake and the teardown. The
    app runs as a task in this loop and the two queues here are the wire: what
    a test sends goes into one, what the app sends comes out of the other.
    """

    def __init__(self, app: Any, path: str, *, headers: list[tuple[bytes, bytes]] | None = None):
        self._app = app
        self._path, _, self._query = path.partition("?")
        self._headers = headers or [(b"host", b"testserver")]
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._closed_by_app: WebSocketClosed | None = None

    async def connect(self, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        """Run the app and wait for it to accept, or raise what it closed with."""
        self._task = asyncio.create_task(self._run_app(), name="r2d2box-test-ws-app")
        await self._to_app.put({"type": "websocket.connect"})
        first = await self._next_from_app(timeout_s)
        if first["type"] != "websocket.accept":
            raise WebSocketClosed(first.get("code", 1000), first.get("reason", ""))

    async def send_json(self, payload: Any) -> None:
        """Send one JSON text frame, as a browser's `ws.send(JSON.stringify(...))` does."""
        await self._to_app.put({"type": "websocket.receive", "text": json.dumps(payload)})

    async def receive_json(self, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        """The next message the app sent, decoded.

        Raises `WebSocketClosed` if the app closed instead, and fails on the
        timeout rather than hanging, so a message that never arrives is a test
        failure with a name on it.
        """
        message = await self._next_from_app(timeout_s)
        if message["type"] == "websocket.close":
            raise WebSocketClosed(message.get("code", 1000), message.get("reason", ""))
        text = message.get("text")
        if text is None:
            text = message["bytes"].decode("utf-8")
        return json.loads(text)

    async def drain(self, timeout_s: float = 0.2) -> list[dict[str, Any]]:
        """Everything the app has sent and not been read yet, stopping at the first pause.

        For asserting on what a client did *not* receive, where the only other
        option is a sleep long enough to be a guess.
        """
        collected = []
        while True:
            try:
                collected.append(await self.receive_json(timeout_s))
            except (asyncio.TimeoutError, WebSocketClosed):
                return collected

    async def receive_until(
        self, kind: str, *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Read until a message of type `kind` arrives; return it and what came before it."""
        skipped = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            message = await self.receive_json(max(deadline - loop.time(), 0.01))
            if message.get("type") == kind:
                return message, skipped
            skipped.append(message)

    async def close(self, code: int = 1000) -> None:
        """Disconnect the way a closed browser tab does, and wait for the app to finish."""
        await self._to_app.put({"type": "websocket.disconnect", "code": code})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, DEFAULT_TIMEOUT_S)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _next_from_app(self, timeout_s: float) -> dict[str, Any]:
        """The app's next ASGI message, or the close it has already sent.

        A connection the app closed stays closed: the recorded exception is
        re-raised for every later read, rather than the test blocking on a
        queue nothing will fill.
        """
        if self._closed_by_app is not None and self._from_app.empty():
            raise self._closed_by_app
        message = await asyncio.wait_for(self._from_app.get(), timeout_s)
        if message["type"] == "websocket.close":
            self._closed_by_app = WebSocketClosed(
                message.get("code", 1000), message.get("reason", "")
            )
        return message

    async def _run_app(self) -> None:
        """Call the ASGI app for this connection and keep it running until it returns."""
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": self._path,
            "raw_path": self._path.encode(),
            "query_string": self._query.encode(),
            "root_path": "",
            "headers": self._headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }
        await self._app(scope, self._to_app.get, self._from_app.put)


@asynccontextmanager
async def websocket(app: Any, path: str) -> AsyncIterator[ASGIWebSocket]:
    """An accepted WebSocket to `app`, disconnected when the block ends.

    Closing on the way out is what makes a failing test leave no app task
    behind — the router's teardown, including its detach, runs either way.
    """
    connection = ASGIWebSocket(app, path)
    await connection.connect()
    try:
        yield connection
    finally:
        await connection.close()


async def request(
    app: Any,
    method: str,
    path: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Response:
    """Make one HTTP request against an ASGI app and read the whole response.

    Enough for the session REST endpoints, which take no bodies: a request is
    a method and a path, and the response is small enough to collect in one
    go.
    """
    split = urlsplit(path)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": split.path,
        "raw_path": split.path.encode(),
        "query_string": split.query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }

    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": b"", "more_body": False})
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await asyncio.wait_for(app(scope, incoming.get, send), timeout_s)

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return Response(
        status=start["status"],
        headers={key.decode(): value.decode() for key, value in start.get("headers", [])},
        body=body,
    )
