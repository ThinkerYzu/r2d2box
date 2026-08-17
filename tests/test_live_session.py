"""A real conversation through a real agent host: two turns, resumed across a restart.

`test_live_proxy.py` proves `build_argv` by running it. This proves the thing
above it — that a turn ends when the agent stops rather than when a fixture
says so, and that `--resume` actually continues a conversation instead of
merely being accepted. Nothing below `AgentHost` is faked, so the transcript
these assertions read is one a real `claude` produced.

Doubly opt-in like the rest of the live tier:

    R2D2BOX_RUN_LIVE=1 pytest -m live
"""

from __future__ import annotations

import asyncio

import pytest

from r2d2box import AgentConfig, AgentHost, FileTranscriptStore

from fake_proxy import RecordingSubscriber
from test_live_proxy import require_agent_proxy  # noqa: F401  (a fixture, used by name)

pytestmark = pytest.mark.live

TURN_TIMEOUT_S = 240


@pytest.fixture
async def live_host(require_agent_proxy, tmp_path):  # noqa: F811  (the imported fixture)
    """An `AgentHost` running real agents in a scratch directory, closed at teardown.

    Depends on `require_agent_proxy` so every test built on it skips without
    `R2D2BOX_RUN_LIVE` — the `live` marker alone does not stop a bare `pytest`
    from running these and spending real quota.

    `cwd` is under `tmp_path` so the claude sessions this mints do not land
    among the project's own transcripts, and the store is a real
    `FileTranscriptStore` because a resume is only interesting if the history
    it resumes was written somewhere.
    """
    workdir = tmp_path / "agent-cwd"
    workdir.mkdir()

    def agent_config(topic: str, name: str) -> AgentConfig:
        return AgentConfig(
            cwd=workdir,
            append_system_prompt="Answer in as few words as possible.",
            extra_args=["--model", "haiku"],
        )

    host = AgentHost(agent_config, store=FileTranscriptStore(tmp_path / "transcripts"))
    yield host
    await host.close()


async def run_turn(session, text: str) -> None:
    """Submit one prompt and wait out the turn it starts."""
    turn_id = await session.submit(text)
    deadline = asyncio.get_running_loop().time() + TURN_TIMEOUT_S
    while session.pending_turns and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
    assert not session.pending_turns, f"turn {turn_id} never ended"


async def test_a_real_conversation_survives_losing_its_process(live_host):
    """Two turns with a restart between them, and the agent still knows the first."""
    session = await live_host.session("live-topic", "s1")
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)

    await run_turn(session, "Remember the word PELICAN. Reply with exactly: OK")
    first_id = session.claude_session_id
    assert first_id

    # An eviction, not a crash: the process goes and the conversation stays.
    await session.stop_process()
    assert session.process_alive is False

    await run_turn(session, "What word did I ask you to remember? Reply with just the word.")

    assert session.claude_session_id == first_id, "the second process resumed a different session"
    snapshot = await session.snapshot()
    assert len(snapshot["turns"]) == 2
    assert snapshot["turns"][0]["outcome"] == "success"
    # Against the real thing, which numbers turns per process and hands the
    # second one `t-1` again: a conversation numbers its turns once.
    assert [turn["id"] for turn in snapshot["turns"]] == ["t-1", "t-2"]

    replies = [
        event.get("text", "")
        for event in snapshot["turns"][1]["events"]
        if event.get("type") == "text"
    ]
    assert "PELICAN" in "".join(replies).upper(), (
        "the resumed agent did not remember the first turn"
    )


async def test_both_attached_clients_see_a_real_turn(live_host):
    """against a live agent, where the message order is not scripted."""
    session = await live_host.session("live-topic", "s1")
    first, second = RecordingSubscriber(), RecordingSubscriber()
    await session.attach(first)
    await session.attach(second)

    await run_turn(session, "Reply with exactly: PONG")

    for subscriber in (first, second):
        assert subscriber.types()[0] == "attached"
        assert "turn_end" in subscriber.types()
        assert "ack" not in subscriber.types()
        text = "".join(m.get("text", "") for m in subscriber.of_type("text"))
        assert "PONG" in text.upper()

    # `seq` numbers the broadcasts and nothing else, so two clients that
    # attached at different moments still read the same numbers off the turn.
    assert [m["seq"] for m in first.messages[1:]] == [m["seq"] for m in second.messages[1:]]
