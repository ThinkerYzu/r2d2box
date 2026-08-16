"""The whole stack against a real agent: a WebSocket, a mounted app, a live claude.

The last stand-in goes here. `test_router_end_to_end.py` still scripts what the
agent says; this does not, so the message order is whatever `claude` produces
and the turn ends when it stops talking.

Doubly opt-in like the rest of the live tier:

    R2D2BOX_RUN_LIVE=1 pytest -m live
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from r2d2box import AgentConfig, FileTranscriptStore, R2D2Box

from asgi_client import request, websocket
from test_live_proxy import require_agent_proxy  # noqa: F401  (a fixture, used by name)

pytestmark = pytest.mark.live

WS = "/chat/ws"
TURN_TIMEOUT_S = 240


@pytest.fixture
async def live_app(require_agent_proxy, tmp_path):  # noqa: F811  (the imported fixture)
    """A mounted `R2D2Box` running real agents in a scratch directory.

    `cwd` is under `tmp_path` so the claude sessions this mints do not land
    among the project's own transcripts.
    """
    workdir = tmp_path / "agent-cwd"
    workdir.mkdir()

    def agent_config(topic: str, session: str) -> AgentConfig:
        return AgentConfig(
            cwd=workdir,
            append_system_prompt="Answer in as few words as possible.",
            extra_args=["--model", "haiku"],
        )

    box = R2D2Box(agent_config, store=FileTranscriptStore(tmp_path / "transcripts"))
    app = FastAPI()
    app.include_router(box.router, prefix="/chat")
    yield app, box
    await box.aclose()


async def receive_turn(connection) -> list[dict]:
    """Read one live turn off a socket, from wherever it starts to its `turn_end`."""
    messages = []
    while not messages or messages[-1]["type"] != "turn_end":
        messages.append(await connection.receive_json(TURN_TIMEOUT_S))
    return messages


async def test_two_browsers_and_one_live_agent(live_app):
    """Phase 3's acceptance case with nothing scripted, plus the transcript after it."""
    app, box = live_app
    created = await request(app, "POST", "/chat/sessions/live-topic")
    name = created.json()["session"]

    async with websocket(app, WS) as first, websocket(app, WS) as second:
        for connection in (first, second):
            await connection.send_json(
                {"type": "attach", "topic": "live-topic", "session": name}
            )
            assert (await connection.receive_json())["type"] == "attached"

        await second.send_json({"type": "submit", "text": "Reply with exactly: PONG"})

        turns = [await receive_turn(connection) for connection in (first, second)]

    for messages in turns:
        assert messages[0]["type"] == "turn_start"
        assert messages[-1]["outcome"] == "success"
        assert "ack" not in [message["type"] for message in messages]
        text = "".join(
            message.get("text", "") for message in messages if message["type"] == "text"
        )
        assert "PONG" in text.upper()

    # Both clients read the same numbers off the same stream.
    assert [message["seq"] for message in turns[0]] == [
        message["seq"] for message in turns[1]
    ]

    async with websocket(app, WS) as returning:
        await returning.send_json(
            {"type": "attach", "topic": "live-topic", "session": name}
        )
        attached = await returning.receive_json()

    assert [turn["user"] for turn in attached["turns"]] == ["Reply with exactly: PONG"]
    assert attached["turn_active"] is False
