"""An in-process stand-in for `AgentProxy`, so `session.py` needs no subprocess.

`proxy.py` is already tested against a real process over real pipes by
`scripted_proxy.py`; repeating that below the session layer would only make
these tests slow and their failures ambiguous. `FakeProxy` implements the same
surface with a queue, and adds what a subprocess cannot offer: a test can hand
it one message at a time and assert on what the session did with each.

    proxy = FakeProxy()
    await proxy.emit({"type": "text", "turn": {"id": "t-1", "kind": "user"}, ...})
    await proxy.end_stream()          # EOF: the session's process has gone

By default a `submit` is acknowledged automatically with a freshly minted turn
id, since almost every test wants that; `auto_ack=False` leaves the ack to the
test, which is how a rejected submit and a lost ack are exercised.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class FakeProxy:
    """A message stream a test writes by hand, with `AgentProxy`'s surface.

    `session_id`, `alive`, `returncode`, `submit`, `request_status`,
    `messages()` and `close()` all behave as the real thing's do — a session
    cannot tell the two apart. What is extra is `emit`, `end_stream`, and the
    `submits` list recording everything written to it.
    """

    def __init__(
        self,
        session_id: str = "fake-session-0001",
        *,
        auto_ack: bool = True,
        outstanding: dict[str, int] | None = None,
    ) -> None:
        self.session_id = session_id
        self.submits: list[dict[str, Any]] = []
        self.closed = False
        self.auto_ack = auto_ack

        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._seq = 1  # `ready` was seq 1 and is consumed before a session sees us
        self._turns = 0
        # What `drain` compares. `_handled` only advances once the session's
        # handler for a message has returned, which is not the same as the
        # message having left the queue.
        self._emitted = 0
        self._handled = 0
        self._outstanding = dict(outstanding or {"user": 0, "unowned": 0, "background": 0})
        self._alive = True
        self._consumed = False

    # ---- the AgentProxy surface ---------------------------------------------

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def returncode(self) -> int | None:
        return None if self._alive else 0

    async def submit(self, text: str, ref: str) -> None:
        if not self._alive:
            raise ConnectionError("agent-proxy is not running")
        self.submits.append({"type": "submit", "text": text, "ref": ref})
        if self.auto_ack:
            await self.ack(ref)

    async def request_status(self, ref: str) -> None:
        if not self._alive:
            raise ConnectionError("agent-proxy is not running")
        self.submits.append({"type": "status", "ref": ref})

    def messages(self) -> AsyncIterator[dict[str, Any]]:
        if self._consumed:
            raise RuntimeError("FakeProxy.messages() is single-consumer")
        self._consumed = True
        return self._iter_messages()

    async def _iter_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            message = await self._queue.get()
            if message is None:
                return
            yield message
            # Reached only when the consumer asks for the next message, so by
            # here the session has finished with this one. That is what makes
            # `drain` a fact rather than a guess about scheduling.
            self._handled += 1

    async def close(self) -> None:
        self._alive = False
        await self._queue.put(None)

    # ---- what a test drives it with ------------------------------------------

    async def emit(self, message: dict[str, Any]) -> dict[str, Any]:
        """Queue one message for the session to read, filling in the envelope.

        `seq` and `outstanding` are stamped when the message lacks them, the
        way agent-proxy stamps every line. Returns the message as queued, so a
        test can assert against exactly what was sent.
        """
        stamped = dict(message)
        self._seq += 1
        self._emitted += 1
        stamped.setdefault("seq", self._seq)
        stamped.setdefault("outstanding", dict(self._outstanding))
        await self._queue.put(stamped)
        return stamped

    async def ack(self, ref: str, *, turn_id: str | None = None) -> str:
        """Acknowledge a submit, minting a turn id unless the test names one.

        Returns the turn id, which is what every later message of that turn
        carries.
        """
        if turn_id is None:
            self._turns += 1
            turn_id = f"t-{self._turns}"
        await self.emit(
            {"type": "ack", "turn": {"id": turn_id, "kind": "user"}, "ref": ref}
        )
        return turn_id

    async def reject(self, ref: str, error: str = "text must be a non-empty string") -> None:
        """Refuse a submit the way agent-proxy does: an `error` with its ref, no ack."""
        await self.emit({"type": "error", "error": error, "ref": ref})

    async def run_turn(
        self,
        turn_id: str,
        *,
        text: str = "done",
        kind: str = "user",
        outcome: str = "success",
    ) -> None:
        """Emit an ordinary turn's messages: a start, one line of prose, an end."""
        turn = {"id": turn_id, "kind": kind}
        await self.emit({"type": "turn_start", "turn": turn})
        await self.emit({"type": "text", "turn": turn, "text": text})
        await self.emit({
            "type": "turn_end",
            "turn": turn,
            "basis": "marker:turn_duration",
            "outcome": outcome,
            "usage": {},
        })

    async def end_stream(self) -> None:
        """Close the stream as a dead process would: EOF, with no further messages."""
        self._alive = False
        await self._queue.put(None)

    async def drain(self, timeout_s: float = 5.0) -> None:
        """Wait until the session has finished handling everything emitted so far.

        The session reads on its own task, so a test that emits and then
        asserts is racing it. Awaiting this first makes the assertion about
        what the session did rather than about how fast it did it. Raises
        rather than returning early if the session never catches up, so a
        stalled read pump fails its test instead of making it flaky.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while self._handled < self._emitted:
            if loop.time() > deadline:
                raise AssertionError(
                    f"the session handled {self._handled} of {self._emitted} messages"
                )
            await asyncio.sleep(0)


async def wait_until(predicate, *, timeout_s: float = 5.0, what: str = "condition") -> None:
    """Give the event loop room until `predicate()` holds, or fail the test.

    For the states no message announces — a read pump noticing EOF, a process
    finishing its exit — where the only alternative is a fixed sleep that is
    either slow or occasionally too short.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"{what} never became true within {timeout_s}s")
        await asyncio.sleep(0.001)


class FakeSpawner:
    """Stands in for a session's spawn callback, recording what each spawn was asked for.

    `resumes` is the claude session id passed to every call in order, which is
    how the resume-and-retry path is asserted; `proxies` is what each call
    returned. `errors` is consumed one entry per call — an exception raises
    from that spawn, `None` lets it succeed — so a test can make the first
    attempt fail and the next one work.
    """

    def __init__(
        self,
        *,
        session_ids: tuple[str, ...] = (),
        errors: tuple[BaseException | None, ...] = (),
    ) -> None:
        self.resumes: list[str | None] = []
        self.proxies: list[FakeProxy] = []
        self._session_ids = list(session_ids)
        self._errors = list(errors)

    async def __call__(self, resume: str | None) -> FakeProxy:
        self.resumes.append(resume)
        if self._errors:
            error = self._errors.pop(0)
            if error is not None:
                raise error
        if self._session_ids:
            session_id = self._session_ids.pop(0)
        else:
            session_id = f"fake-session-{len(self.proxies) + 1:04d}"
        proxy = FakeProxy(session_id)
        self.proxies.append(proxy)
        return proxy

    @property
    def latest(self) -> FakeProxy:
        """The proxy handed out by the most recent successful spawn."""
        return self.proxies[-1]


class RecordingSubscriber:
    """A `Subscriber` that keeps every message, standing in for a WebSocket client."""

    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[dict[str, Any]] = []
        self.fail = fail

    async def send(self, message: dict[str, Any]) -> None:
        if self.fail:
            raise ConnectionError("subscriber is gone")
        self.messages.append(message)

    def types(self) -> list[str]:
        """The `type` of every message received, in order."""
        return [message.get("type") for message in self.messages]

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        """Every message of one type."""
        return [message for message in self.messages if message.get("type") == kind]
