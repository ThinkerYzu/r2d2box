"""One agent-proxy subprocess: spawn it, read its stream, write its commands.

This module knows about pipes, buffer limits and JSON decoding, and nothing
about turns — that is `session.py`'s half of the seam described in the
implementation guide under Key Principle. Everything here can be exercised
against a scripted subprocess with no `claude` installed.

Two details live here that bzdash and agent-desktop-env each had to find for
themselves: the read buffer has to be 16 MiB, and overrunning it costs one
message rather than the session.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from .config import AgentConfig, build_argv

_log = logging.getLogger(__name__)

# One message routinely exceeds asyncio's 64 KiB default line buffer.
#
# A `tool_result` carrying a large file read or command output is a single
# line, and past the limit `readline()` raises instead of returning. Both apps
# this library replaces lost their reader that way.
STREAM_LIMIT = 16 * 1024 * 1024  # 16 MiB

# How long a terminated agent-proxy gets to exit before it is killed.
_TERMINATE_TIMEOUT_S = 5
# How long the stderr drain gets to finish after the process is gone.
_STDERR_DRAIN_TIMEOUT_S = 1


class ProxyStartError(RuntimeError):
    """agent-proxy could not be started, or did not open with a usable `ready`."""


class AgentProxy:
    """A running agent-proxy process, with its session id already read.

    Construct with `await AgentProxy.start(config)`; the constructor itself is
    for tests that supply their own process. The instance is live from that
    moment until `close()`, and `session_id` is the claude session id a later
    process passes back as `build_argv`'s `resume`.

    One consumer only: `messages()` reads the single stdout pipe, so a second
    concurrent iteration raises. Writes are serialized by an internal lock, so
    concurrent `submit` calls cannot interleave on stdin.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        session_id: str,
        *,
        stderr_task: asyncio.Task[None] | None = None,
        tag: str = "",
    ) -> None:
        self._process = process
        self._session_id = session_id
        self._stderr_task = stderr_task
        self._tag = tag
        self._write_lock = asyncio.Lock()
        self._consumed = False
        # Set while the tail of an over-long line is still arriving; see
        # `messages`.
        self._recovering = False

    @classmethod
    async def start(
        cls,
        config: AgentConfig,
        resume: str | None = None,
        *,
        tag: str = "",
    ) -> AgentProxy:
        """Spawn one agent-proxy for `config` and read its `ready`, or raise `ProxyStartError`.

        Returns only once the session id is known, so no layer above needs a
        first-message special case. `resume` is a claude session id from an
        earlier process; `tag` is a caller-chosen label for log lines, normally
        `"topic/session"`.

        On any failure — a missing binary, a proxy that rejects its arguments,
        a stream that ends before `ready` — the process is cleaned up before
        the exception leaves, so a failed start leaks nothing.
        """
        argv = build_argv(config, resume)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
                cwd=config.cwd,
            )
        except FileNotFoundError as exc:
            raise ProxyStartError(
                f"agent-proxy binary {argv[0]!r} not found on PATH"
            ) from exc

        stderr_task: asyncio.Task[None] | None = None
        if process.stderr is not None:
            # Drained from the start. An unread stderr pipe fills and blocks the
            # child, and spawn-time failures (bad argv, an MCP server that will
            # not start) are written there and nowhere else.
            stderr_task = asyncio.create_task(_drain_stderr(process.stderr, tag))

        try:
            session_id = await cls._read_ready(process)
        except BaseException:
            await _shut_down(process, stderr_task)
            raise
        return cls(process, session_id, stderr_task=stderr_task, tag=tag)

    @staticmethod
    async def _read_ready(process: asyncio.subprocess.Process) -> str:
        """Consume agent-proxy's mandatory first line and return its session id.

        `ready` is always `seq` 1 and its `session_id` is never null (agent-proxy
        API.md § Sessions). The one documented exception is `--session-id`
        combined with `--resume` or `--continue`, which produces a lone `error`
        instead — `build_argv` refuses to construct that, so reaching it here
        means agent-proxy changed and the message says so.
        """
        assert process.stdout is not None
        raw = await process.stdout.readline()
        if not raw:
            raise ProxyStartError("agent-proxy closed stdout before emitting ready")
        try:
            message = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ProxyStartError(
                f"agent-proxy's first line was not JSON: {raw[:200]!r}"
            ) from exc
        if message.get("type") == "error":
            raise ProxyStartError(f"agent-proxy refused to start: {message.get('error')}")
        if message.get("type") != "ready":
            raise ProxyStartError(
                f"agent-proxy's first message was {message.get('type')!r}, not ready"
            )
        session_id = message.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ProxyStartError(f"agent-proxy's ready carried no session id: {message!r}")
        return session_id

    @property
    def session_id(self) -> str:
        """The claude session id, for resuming this conversation in a later process."""
        return self._session_id

    @property
    def alive(self) -> bool:
        """True while the process is still running."""
        return self._process.returncode is None

    @property
    def returncode(self) -> int | None:
        """The process's exit status, or None while it is still running."""
        return self._process.returncode

    async def submit(self, text: str, ref: str) -> None:
        """Submit a prompt, tagged with `ref` so its `ack` can be recognized.

        `ref` is opaque to agent-proxy and echoed on the `ack` that carries the
        new turn's id. It is required here, not optional as on the wire: without
        it the only way back to a turn id is counting acks, which goes silently
        off by one the first time a submit is rejected (agent-proxy API.md
        § Correlating a command with its answer).
        """
        await self.send({"type": "submit", "text": text, "ref": ref})

    async def request_status(self, ref: str) -> None:
        """Ask for a `state` message describing every unfinished turn and task.

        The reply arrives on `messages()` like anything else, tagged with `ref`,
        so two queries in flight cannot be confused for one another.
        """
        await self.send({"type": "status", "ref": ref})

    async def send(self, command: dict[str, Any]) -> None:
        """Write one command line to agent-proxy's stdin.

        Raises `ConnectionError` once the process is gone, which is how a caller
        learns its agent died between messages.
        """
        stdin = self._process.stdin
        if stdin is None or not self.alive:
            raise ConnectionError("agent-proxy is not running")
        line = (json.dumps(command) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                stdin.write(line)
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ConnectionError("agent-proxy closed its stdin") from exc

    def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield each decoded message from agent-proxy's stdout until it closes.

        Ends on EOF; never raises for a malformed or over-long line. Two of the
        messages it yields are r2d2box's own rather than agent-proxy's, and are
        the only ones without a `seq` or an `outstanding`:

        - `{"type": "error", "error": ...}` for a line past `STREAM_LIMIT`
        - `{"type": "raw", "text": ...}` for a line that is not JSON

        An over-long line is not fatal. `readline()` discards what it buffered
        and raises `ValueError` (asyncio re-raises `LimitOverrunError` as one),
        but the pipe itself is unharmed, so the recovery is to report the loss
        and keep reading. The rest of that line is still on its way and arrives
        as one or more junk lines; `_recovering` swallows them, so a caller sees
        one `error` and then the next real message rather than a burst of
        `raw` fragments.
        """
        # Claimed here rather than inside the generator, whose body would not
        # run until the first `__anext__` — by which time a second caller could
        # already be interleaved with the first, each taking half the stream.
        if self._consumed:
            raise RuntimeError("AgentProxy.messages() is single-consumer")
        self._consumed = True
        return self._iter_messages()

    async def _iter_messages(self) -> AsyncIterator[dict[str, Any]]:
        """The read loop behind `messages`.

        `messages` makes the single-consumer claim before calling this, so the
        loop itself assumes it is the only reader of stdout.
        """
        stdout = self._process.stdout
        assert stdout is not None

        while True:
            try:
                raw = await stdout.readline()
            except ValueError as exc:
                if not self._recovering:
                    self._recovering = True
                    _log.warning(
                        "agent-proxy[%s] stdout line exceeded %d bytes: %s",
                        self._tag, STREAM_LIMIT, exc,
                    )
                    yield {
                        "type": "error",
                        "error": (
                            f"agent stream line exceeded the {STREAM_LIMIT}-byte "
                            "buffer — line dropped, stream continuing"
                        ),
                    }
                continue
            if not raw:
                return
            text = raw.decode("utf-8", errors="replace")
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                if self._recovering:
                    _log.debug("agent-proxy[%s] discarded %d bytes of a dropped line",
                               self._tag, len(raw))
                    continue
                stripped = text.rstrip()
                if not stripped:
                    continue
                _log.warning("agent-proxy[%s] emitted a non-JSON line: %.200s",
                             self._tag, stripped)
                yield {"type": "raw", "text": stripped}
                continue
            self._recovering = False
            yield message

    async def close(self) -> None:
        """Stop the process and wait for it to go, killing it if it will not.

        Safe to call more than once, and safe on a process that has already
        exited. There is no cancellation in the protocol, so a turn in flight is
        lost rather than finished — the caller decides whether that is
        acceptable before calling.
        """
        await _shut_down(self._process, self._stderr_task)
        self._stderr_task = None


async def _shut_down(
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task[None] | None,
) -> None:
    """Terminate `process`, escalating to a kill, then let its stderr drain finish.

    Shared by `close` and by `start`'s failure path, so a spawn that dies before
    `ready` cleans up exactly as thoroughly as an ordinary shutdown.
    """
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        else:
            try:
                await asyncio.wait_for(process.wait(), timeout=_TERMINATE_TIMEOUT_S)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
    if stderr_task is not None:
        try:
            await asyncio.wait_for(stderr_task, timeout=_STDERR_DRAIN_TIMEOUT_S)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            stderr_task.cancel()


async def _drain_stderr(stream: asyncio.StreamReader, tag: str) -> None:
    """Forward agent-proxy's stderr into this module's logger until the stream ends.

    Each line is tagged with the caller's `topic/session` label, so several
    agents logging at once can still be told apart. Logged at warning:
    agent-proxy is quiet on stderr in normal operation, so anything arriving
    here is worth seeing by default.
    """
    while True:
        try:
            raw = await stream.readline()
        except asyncio.CancelledError:
            return
        except ValueError as exc:
            _log.warning("agent-proxy[%s] stderr line dropped (buffer overrun): %s",
                         tag, exc)
            continue
        except (BrokenPipeError, ConnectionResetError):
            return
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            _log.warning("agent-proxy[%s]: %s", tag, line)
