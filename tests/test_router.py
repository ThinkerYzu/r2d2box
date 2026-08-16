"""The transport: one WebSocket per client, many clients per conversation.

The router is the thinnest layer in the library, and these tests are mostly
about what it must *not* do — not displace a second client, not lose a turn
when the first one leaves, not wait on a browser while holding the session
lock. The conversation itself is `test_session.py`'s subject.

Everything runs against `FakeHost`, so no subprocess is involved, and against
the real FastAPI app through `asgi_client.py`, so the routing, the `WebSocket`
object and the close codes are the production ones.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from r2d2box import AgentConfig, MemoryTranscriptStore, R2D2Box

from asgi_client import request, websocket
from fake_proxy import FakeHost, FakeProxy, wait_until
from r2d2box.router import _ClientConnection

WS = "/chat/ws"


@pytest.fixture
async def mounted():
    """A factory for `(app, box)` with a `FakeHost` behind the router.

    The router is mounted under `/chat`, as a host application mounts it, so
    every path in these tests is the one a browser would use. Both the box and
    its host are closed at teardown.
    """
    built = []

    def make(**fields) -> tuple[FastAPI, R2D2Box]:
        host = fields.pop("host", None) or FakeHost(
            store=fields.pop("store", None) or MemoryTranscriptStore()
        )
        box = R2D2Box(host=host, **fields)
        app = FastAPI()
        app.include_router(box.router, prefix="/chat")
        built.append(box)
        return app, box

    yield make
    for box in built:
        await box.aclose()


async def attach(connection, topic: str = "bug-1", session: str | None = "s1") -> dict:
    """Attach a client and return the `attached` message it gets back."""
    command = {"type": "attach", "topic": topic}
    if session is not None:
        command["session"] = session
    await connection.send_json(command)
    return await connection.receive_json()


async def run_one_turn(
    host: FakeHost,
    topic: str = "bug-1",
    session: str = "s1",
    *,
    turn_id: str = "t-1",
    text: str = "the host is r2d2",
) -> FakeProxy:
    """Play out the turn a submitted prompt started, on the fake proxy behind it.

    The submit is handled on the connection's own task, so the proxy does not
    exist the instant the frame is sent; waiting for it is what keeps the test
    about the router rather than about scheduling.
    """
    await wait_until(
        lambda: (topic, session) in host.proxies, what="a process for the session"
    )
    proxy = host.proxies[(topic, session)]
    await wait_until(lambda: proxy.submits, what="the submit reaching agent-proxy")
    await proxy.run_turn(turn_id, text=text)
    await proxy.drain()
    return proxy


# ---- the two cases the implementation guide names as Phase 3's validation ----


async def test_two_clients_on_one_session_both_see_the_whole_turn(mounted):
    """DESIGN Decision 2 over the wire: a second tab is a view, not a displacement."""
    app, box = mounted()

    async with websocket(app, WS) as first, websocket(app, WS) as second:
        assert (await attach(first))["type"] == "attached"
        assert (await attach(second))["type"] == "attached"

        await first.send_json({"type": "submit", "text": "what host is this?"})
        await run_one_turn(box.host)

        for connection in (first, second):
            turn = [await connection.receive_json() for _ in range(3)]
            assert [message["type"] for message in turn] == [
                "turn_start", "text", "turn_end",
            ]
            assert {message["turn"]["id"] for message in turn} == {"t-1"}

        # Neither socket was closed on the other's account — the 4001
        # displacement agent-desktop-env does today has no counterpart here.
        await first.send_json({"type": "status_query"})
        await second.send_json({"type": "status_query"})
        assert (await first.receive_json())["type"] == "status"
        assert (await second.receive_json())["type"] == "status"


async def test_a_client_that_leaves_mid_turn_finds_the_turn_finished_on_its_return(mounted):
    """DESIGN Decision 8: the turn outlives the client, and the transcript proves it."""
    app, box = mounted()

    async with websocket(app, WS) as leaving:
        await attach(leaving)
        await leaving.send_json({"type": "submit", "text": "what host is this?"})
        await wait_until(
            lambda: ("bug-1", "s1") in box.host.proxies, what="a process for the session"
        )
        proxy = box.host.proxies[("bug-1", "s1")]
        await wait_until(lambda: proxy.submits, what="the submit reaching agent-proxy")
        await proxy.emit({"type": "turn_start", "turn": {"id": "t-1", "kind": "user"}})
        assert (await leaving.receive_json())["type"] == "turn_start"

    session = await box.host.session("bug-1", "s1")
    await wait_until(lambda: session.subscriber_count == 0, what="the client detaching")

    # The rest of the turn happens with nobody listening at all.
    await proxy.emit({"type": "text", "turn": {"id": "t-1", "kind": "user"}, "text": "r2d2"})
    await proxy.emit({
        "type": "turn_end",
        "turn": {"id": "t-1", "kind": "user"},
        "basis": "marker:turn_duration",
        "outcome": "success",
    })
    await proxy.drain()

    async with websocket(app, WS) as returning:
        attached = await attach(returning)

    assert attached["turn_active"] is False
    assert [turn["user"] for turn in attached["turns"]] == ["what host is this?"]
    assert [event["type"] for event in attached["turns"][0]["events"]] == [
        "turn_start", "text", "turn_end",
    ]


# ---- attaching ---------------------------------------------------------------


async def test_attach_answers_with_the_transcript_and_the_live_state(mounted):
    """The `attached` message is DESIGN's late-joiner contract, delivered in one shot."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        attached = await attach(connection)

    assert attached["type"] == "attached"
    assert attached["topic"] == "bug-1"
    assert attached["session"] == "s1"
    assert attached["turns"] == []
    assert attached["turn_active"] is False
    assert attached["task_ids"] == []
    assert attached["process_alive"] is False
    # Nothing has been broadcast yet, and `attached` does not number itself
    # into the stream: the next message this client sees will be seq 1.
    assert attached["seq"] == 0


async def test_attach_without_a_session_creates_one_and_names_it(mounted):
    """A client that only knows its topic never has to call the REST endpoint first."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        attached = await attach(connection, session=None)

    assert attached["type"] == "attached"
    assert attached["session"]
    assert [info.session for info in await box.host.list_sessions("bug-1")] == [
        attached["session"]
    ]


async def test_attaching_again_switches_sessions_in_place(mounted):
    """The JS API's `box.attach(topic, session)`: the old session stops arriving."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        await attach(connection, session="s1")
        await attach(connection, session="s2")

        first = await box.host.session("bug-1", "s1")
        assert first.subscriber_count == 0
        assert (await box.host.session("bug-1", "s2")).subscriber_count == 1

        # A message on the session it left must not reach it.
        await connection.send_json({"type": "status_query"})
        assert (await connection.receive_json())["session"] == "s2"


async def test_detach_stops_the_stream_and_leaves_the_socket_open(mounted):
    """Unsubscribing is not disconnecting — the client can attach again on the same socket."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        await attach(connection)
        await connection.send_json({"type": "detach"})
        detached = await connection.receive_json()

        assert detached == {"type": "detached", "topic": "bug-1", "session": "s1"}
        assert (await box.host.session("bug-1", "s1")).subscriber_count == 0

        assert (await attach(connection))["type"] == "attached"


# ---- submitting --------------------------------------------------------------


async def test_a_submit_is_acknowledged_by_the_turn_start_every_client_sees(mounted):
    """No private reply to the submitter: the turn id arrives the way it does for everyone."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        await attach(connection)
        await connection.send_json({"type": "submit", "text": "what host is this?"})
        await run_one_turn(box.host)

        first = await connection.receive_json()

    assert first["type"] == "turn_start"
    assert first["turn"]["id"] == "t-1"


async def test_a_submits_context_reaches_the_hosts_build_prompt_hook(mounted):
    """DESIGN Decision 6: the client's ride-along JSON is the hook's fourth argument."""
    seen = []

    def build_prompt(topic, session, text, context):
        seen.append((topic, session, text, context))
        return f"[{context['file']}] {text}"

    app, box = mounted(host=FakeHost(build_prompt=build_prompt))

    async with websocket(app, WS) as connection:
        await attach(connection)
        await connection.send_json({
            "type": "submit",
            "text": "what does this do?",
            "context": {"file": "DESIGN.md"},
        })
        proxy = await run_one_turn(box.host)

    assert seen == [("bug-1", "s1", "what does this do?", {"file": "DESIGN.md"})]
    assert proxy.submits[0]["text"] == "[DESIGN.md] what does this do?"


async def test_a_refused_prompt_is_reported_to_the_client_that_sent_it(mounted):
    """agent-proxy's refusal comes back as an error on that connection, not as a turn."""
    app, box = mounted(host=FakeHost(auto_ack=False))

    async with websocket(app, WS) as connection:
        await attach(connection)
        await connection.send_json({"type": "submit", "text": "?"})

        await wait_until(
            lambda: ("bug-1", "s1") in box.host.proxies, what="a process for the session"
        )
        proxy = box.host.proxies[("bug-1", "s1")]
        await wait_until(lambda: proxy.submits, what="the submit reaching agent-proxy")
        await proxy.reject(proxy.submits[0]["ref"], "text must be a non-empty string")

        # Two errors arrive: the session broadcasts agent-proxy's own, and the
        # router adds one aimed at the client whose submit it was. Which lands
        # first is a scheduling detail — the `scope` field is what separates
        # them, and it is the only thing a client should sort them by.
        errors = [await connection.receive_json() for _ in range(2)]

    broadcast = next(message for message in errors if "scope" not in message)
    reported = next(message for message in errors if message.get("scope") == "connection")
    assert broadcast["type"] == "error"
    assert broadcast["seq"] == 1
    assert reported["type"] == "error"
    assert "text must be a non-empty string" in reported["error"]
    assert "seq" not in reported


async def test_a_failing_build_prompt_hook_does_not_take_the_connection_down(mounted):
    """A broken host hook is this client's error; the socket stays usable."""

    def build_prompt(topic, session, text, context):
        raise RuntimeError("the bug row is gone")

    app, box = mounted(host=FakeHost(build_prompt=build_prompt))

    async with websocket(app, WS) as connection:
        await attach(connection)
        await connection.send_json({"type": "submit", "text": "what host is this?"})
        reported = await connection.receive_json()

        assert reported["scope"] == "connection"
        assert "the bug row is gone" in reported["error"]

        await connection.send_json({"type": "status_query"})
        assert (await connection.receive_json())["type"] == "status"


# ---- state a client reconciles against ---------------------------------------


async def test_a_background_task_that_finishes_unwatched_is_reported_on_attach(mounted):
    """DESIGN Decision 7: the server's task set is authoritative, connected or not."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        await attach(connection)
        await connection.send_json({"type": "submit", "text": "run the build"})
        proxy = await run_one_turn(box.host)
        await proxy.emit({"type": "task_start", "task": {"id": "bash_3"}})
        await proxy.drain()

    session = await box.host.session("bug-1", "s1")
    await wait_until(lambda: session.subscriber_count == 0, what="the client detaching")
    await proxy.emit({"type": "task_end", "task": {"id": "bash_3"}})
    await proxy.drain()

    async with websocket(app, WS) as returning:
        assert (await attach(returning))["task_ids"] == []


async def test_status_query_answers_with_the_sessions_live_state(mounted):
    """The `status` message, for a client reconciling after a reconnect."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        await attach(connection)
        await connection.send_json({"type": "submit", "text": "what host is this?"})
        proxy = await run_one_turn(box.host)
        await proxy.emit({"type": "task_start", "task": {"id": "bash_3"}})
        await proxy.drain()

        _, _ = await connection.receive_until("task_start")
        await connection.send_json({"type": "status_query"})
        status = await connection.receive_json()

    assert status["type"] == "status"
    assert status["turn_active"] is False
    assert status["task_ids"] == ["bash_3"]
    assert status["process_alive"] is True


async def test_a_dead_agent_proxy_is_announced_to_every_attached_client(mounted):
    """`process_exited` is r2d2box's own message, and it reaches both tabs."""
    app, box = mounted()

    async with websocket(app, WS) as first, websocket(app, WS) as second:
        await attach(first)
        await attach(second)
        await first.send_json({"type": "submit", "text": "what host is this?"})
        proxy = await run_one_turn(box.host)
        await proxy.end_stream()

        for connection in (first, second):
            exited, _ = await connection.receive_until("process_exited")
            assert exited["returncode"] == 0
            assert exited["session"] == "s1"


# ---- commands the router has to refuse ---------------------------------------


@pytest.mark.parametrize(
    "command, expected",
    [
        ({"type": "submit", "text": "hello"}, "attach to a session"),
        ({"type": "status_query"}, "attach to a session"),
        ({"type": "attach"}, "non-empty `topic`"),
        ({"type": "attach", "topic": "bug-1", "session": 7}, "non-empty string"),
        ({"type": "wat"}, "unknown command type"),
    ],
)
async def test_a_command_the_router_cannot_run_comes_back_as_an_error(
    mounted, command, expected
):
    """Every frame is answered — a silent drop reads as a hung server from the browser."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        await connection.send_json(command)
        reported = await connection.receive_json()

    assert reported["type"] == "error"
    assert reported["scope"] == "connection"
    assert expected in reported["error"]


async def test_an_empty_submit_is_refused_before_it_reaches_the_agent(mounted):
    """Whitespace is not a prompt, and agent-proxy would only reject it later anyway."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        await attach(connection)
        await connection.send_json({"type": "submit", "text": "   "})
        reported = await connection.receive_json()

    assert "non-empty `text`" in reported["error"]
    assert box.host.spawns == []


async def test_a_frame_that_is_not_a_json_object_is_answered_rather_than_dropped(mounted):
    """Malformed input is a front-end bug, and it should look like one."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        await connection._to_app.put({"type": "websocket.receive", "text": "not json"})
        first = await connection.receive_json()
        await connection._to_app.put({"type": "websocket.receive", "text": "[1, 2]"})
        second = await connection.receive_json()

    assert first["error"] == second["error"] == "each frame must be a JSON object"


# ---- the queue that keeps one slow client from stalling a session ------------


class BlockedSocket:
    """A `WebSocket` stand-in whose sends never complete until it is released.

    The overflow path cannot be reached through `asgi_client.py`, which always
    accepts what the app writes — a client too slow to read is exactly the
    thing a cooperative test client cannot be. This is the browser that has
    stopped reading.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.released = asyncio.Event()
        self._incoming: asyncio.Queue = asyncio.Queue()

    async def send_json(self, message: dict) -> None:
        await self.released.wait()
        self.sent.append(message)

    async def receive(self) -> dict:
        return await self._incoming.get()

    async def close(self) -> None:
        pass


async def test_attaching_a_client_that_is_not_reading_does_not_wait_for_it(mounted):
    """The rule the whole transport hangs on: `send` queues, it never awaits the peer.

    `Session.attach` sends the `attached` message while holding the session
    lock, so a connection that waited on its socket here would hold the lock —
    and with it the read pump — for as long as one browser stayed silent.
    """
    app, box = mounted()
    session = await box.host.session("bug-1", "s1")
    connection = _ClientConnection(BlockedSocket(), box.host)

    await asyncio.wait_for(session.attach(connection), 1.0)

    assert session.subscriber_count == 1


async def test_a_client_that_lets_its_queue_fill_is_dropped_with_a_reason(mounted):
    """One browser that stopped reading costs itself its subscription, not the session."""
    app, box = mounted()
    session = await box.host.session("bug-1", "s1")
    socket = BlockedSocket()
    connection = _ClientConnection(socket, box.host, queue_limit=4)
    runner = asyncio.create_task(connection.run())
    await session.attach(connection)

    for number in range(10):
        await session._broadcast_locked({"type": "text", "text": f"line {number}"})

    assert session.subscriber_count == 0
    socket.released.set()
    await asyncio.wait_for(runner, 5.0)

    # Everything still queued when the client fell behind is discarded, and
    # the reason it is being disconnected takes its place. The `attached` the
    # writer had already taken off the queue is past saving — that is the one
    # message the socket was mid-send on.
    assert [message["type"] for message in socket.sent] == ["attached", "error"]
    assert socket.sent[-1]["fatal"] is True
    assert "reattach" in socket.sent[-1]["error"]


# ---- the REST endpoints the host's own furniture is built on ------------------


async def test_the_session_endpoints_create_list_and_delete(mounted):
    """DESIGN Decision 3's three endpoints, each one `AgentHost` call."""
    app, box = mounted()

    created = await request(app, "POST", "/chat/sessions/bug-1")
    assert created.status == 201
    name = created.json()["session"]

    listed = await request(app, "GET", "/chat/sessions/bug-1")
    assert listed.status == 200
    assert [entry["session"] for entry in listed.json()["sessions"]] == [name]

    deleted = await request(app, "DELETE", f"/chat/sessions/bug-1/{name}")
    assert deleted.json() == {"topic": "bug-1", "session": name, "existed": True}
    assert (await request(app, "GET", "/chat/sessions/bug-1")).json()["sessions"] == []


async def test_deleting_a_session_clears_what_it_said(mounted):
    """`DELETE` means the conversation is over, not merely idle: the transcript goes too."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        await attach(connection)
        await connection.send_json({"type": "submit", "text": "what host is this?"})
        await run_one_turn(box.host)

    await request(app, "DELETE", "/chat/sessions/bug-1/s1")

    assert await box.host.store.read_turns("bug-1", "s1") == []
    async with websocket(app, WS) as returning:
        assert (await attach(returning))["turns"] == []


async def test_listing_a_topic_nobody_has_used_is_empty_rather_than_an_error(mounted):
    """A session picker asks about a bug before anyone has said anything about it."""
    app, box = mounted()

    listed = await request(app, "GET", "/chat/sessions/bug-9999")

    assert listed.status == 200
    assert listed.json() == {"topic": "bug-9999", "sessions": []}


# ---- mounting ----------------------------------------------------------------


async def test_the_static_mount_serves_the_front_end(mounted, tmp_path):
    """Phase 4's assets get a URL that does not change: `{prefix}/static`."""
    (tmp_path / "r2d2box.js").write_text("export const version = 3;\n", encoding="utf-8")
    app, box = mounted(static_dir=tmp_path)

    served = await request(app, "GET", "/chat/static/r2d2box.js")

    assert served.status == 200
    assert served.body == b"export const version = 3;\n"


async def test_a_missing_static_directory_is_a_warning_rather_than_a_failure(
    mounted, tmp_path, caplog
):
    """The server half is useful on its own, and a host may want only the WebSocket."""
    app, box = mounted(static_dir=tmp_path / "nowhere")

    async with websocket(app, WS) as connection:
        assert (await attach(connection))["type"] == "attached"
    assert (await request(app, "GET", "/chat/static/r2d2box.js")).status == 404


async def test_a_box_needs_exactly_one_of_a_config_callback_and_a_host():
    """Two ways to build the same registry, and no way to ask for both."""
    with pytest.raises(ValueError, match="either an agent_config"):
        R2D2Box()
    with pytest.raises(ValueError, match="either an agent_config"):
        R2D2Box(lambda topic, name: AgentConfig(), host=FakeHost())


async def test_the_lifespan_runs_the_sweeper_and_closes_the_host(mounted):
    """What a host wires into `FastAPI(lifespan=...)`, tested without a server."""
    app, box = mounted()
    box.host.idle_timeout_s = 0

    async with box.lifespan(app):
        session = await box.host.session("bug-1", "s1")
        assert box.host._sweeper is not None

    assert box.host._sweeper is None
    assert box.host.live_sessions() == []
    with pytest.raises(ConnectionError):
        await session.submit("anyone there?")


async def test_a_first_websocket_starts_the_sweeper_a_host_forgot_to_wire(mounted):
    """Forgetting the lifespan should cost cleanup at shutdown, not eviction entirely."""
    app, box = mounted()
    assert box.host._sweeper is None

    async with websocket(app, WS) as connection:
        await attach(connection)
        assert box.host._sweeper is not None


async def test_a_closed_socket_leaves_no_subscriber_behind(mounted):
    """The exit path detaches whether the client asked to or not."""
    app, box = mounted()

    async with websocket(app, WS) as connection:
        await attach(connection)
        session = await box.host.session("bug-1", "s1")
        assert session.subscriber_count == 1

    await wait_until(lambda: session.subscriber_count == 0, what="the client detaching")


async def test_the_box_can_be_mounted_at_the_application_root(mounted):
    """A demo host with nothing else on the port mounts with no prefix at all."""
    host = FakeHost()
    box = R2D2Box(host=host)
    app = FastAPI()
    app.include_router(box.router)
    try:
        async with websocket(app, "/ws") as connection:
            assert (await attach(connection))["type"] == "attached"
        assert (await request(app, "GET", "/sessions/bug-1")).status == 200
    finally:
        await box.aclose()
