"""Phase 3's validation, with a real subprocess under the real router.

`test_router.py` proves the transport's logic against `FakeHost`. These run the
whole stack — a mounted FastAPI app, a WebSocket, a session, `build_argv` and
an actual process — so that nothing between the browser's frame and the child's
stdout is a stand-in except the agent itself.

Still no `claude` anywhere: `scripted_proxy.py` is the binary, reached through
the same `build_argv` production uses.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from r2d2box import AgentConfig, FileTranscriptStore, R2D2Box

from asgi_client import request, websocket
from conftest import FIXTURES_DIR, SCRIPTED_PROXY

WS = "/chat/ws"

A_WHOLE_TURN = ["turn_start", "text", "tool_use", "tool_result", "text", "turn_end"]


@pytest.fixture
async def scripted_app(tmp_path):
    """A factory for `(app, box)` whose sessions spawn `scripted_proxy.py`.

    Call it with a fixture file name and any `R2D2Box` keyword. The transcript
    goes to a file store under `tmp_path`, so a test can prove the conversation
    survived the process by reading it back.
    """
    built = []

    def make(fixture: str, **fields) -> tuple[FastAPI, R2D2Box]:
        script = FIXTURES_DIR / fixture
        assert script.exists(), f"no such fixture: {script}"

        def agent_config(topic: str, session: str) -> AgentConfig:
            return AgentConfig(
                proxy_bin=str(SCRIPTED_PROXY),
                append_system_prompt=f"you are talking about {topic}",
                extra_args=["--script", str(script)],
            )

        fields.setdefault("store", FileTranscriptStore(tmp_path / "transcripts"))
        box = R2D2Box(agent_config, **fields)
        app = FastAPI()
        app.include_router(box.router, prefix="/chat")
        built.append(box)
        return app, box

    yield make
    for box in built:
        await box.aclose()


async def receive_turn(connection, expected: list[str]) -> list[dict]:
    """Read until the turn ends, and assert it arrived whole and in order."""
    messages = []
    while not messages or messages[-1]["type"] != "turn_end":
        messages.append(await connection.receive_json())
    assert [message["type"] for message in messages] == expected
    return messages


async def test_two_websocket_clients_receive_one_turn_from_one_process(scripted_app):
    """The Phase 3 acceptance case: one submit, one process, two clients, one stream."""
    app, box = scripted_app("one-turn.jsonl")

    async with websocket(app, WS) as first, websocket(app, WS) as second:
        for connection in (first, second):
            await connection.send_json(
                {"type": "attach", "topic": "bug-1992198", "session": "s1"}
            )
            assert (await connection.receive_json())["type"] == "attached"

        await second.send_json({"type": "submit", "text": "what host is this?"})

        for connection in (first, second):
            messages = await receive_turn(connection, A_WHOLE_TURN)
            assert {message["turn"]["id"] for message in messages} == {"t-1"}
            assert messages[3]["content"] == "r2d2"
            # DESIGN Decision 1: agent-proxy's own numbering is preserved
            # beside r2d2box's rather than overwritten by it. r2d2box's counts
            # broadcasts and nothing else, so both clients see the same
            # unbroken run however many of them attached first.
            assert [message["seq"] for message in messages] == [1, 2, 3, 4, 5, 6]
            assert [message["proxy_seq"] for message in messages] == [3, 4, 5, 6, 7, 8]

    assert len(box.host.live_sessions("bug-1992198")) == 1


async def test_a_session_made_over_rest_carries_its_transcript_to_the_next_client(
    scripted_app,
):
    """The two halves a host's furniture uses together: POST a session, then talk in it."""
    app, box = scripted_app("one-turn.jsonl")

    created = await request(app, "POST", "/chat/sessions/bug-1992198")
    name = created.json()["session"]

    async with websocket(app, WS) as connection:
        await connection.send_json(
            {"type": "attach", "topic": "bug-1992198", "session": name}
        )
        await connection.receive_json()
        await connection.send_json({"type": "submit", "text": "what host is this?"})
        await receive_turn(connection, A_WHOLE_TURN)
        # `one-turn.jsonl` exits at the end of its turn, so the process really
        # is gone by the time the next client arrives.
        assert (await connection.receive_json())["type"] == "process_exited"

    async with websocket(app, WS) as returning:
        await returning.send_json(
            {"type": "attach", "topic": "bug-1992198", "session": name}
        )
        attached = await returning.receive_json()

    assert attached["process_alive"] is False
    assert [turn["user"] for turn in attached["turns"]] == ["what host is this?"]
    assert [event["type"] for event in attached["turns"][0]["events"]] == A_WHOLE_TURN

    listed = await request(app, "GET", "/chat/sessions/bug-1992198")
    assert [entry["session"] for entry in listed.json()["sessions"]] == [name]
