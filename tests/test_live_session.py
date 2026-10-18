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

# Mirrors agent-proxy's own live fixture for the same scenario, so this test
# and the one that added `command`/`output` there watch the same command run.
BACKGROUND_TASK_PROMPT = (
    "Use the Bash tool with run_in_background set to true to start exactly "
    "this command: sleep 2; echo finished. Do not wait for it to finish. "
    "Then reply with exactly: STARTED"
)

# A foreground tool the agent will sit in for a while, so a cancel lands
# mid-tool — agent-proxy's own live fixture for the same scenario. `sleep`
# itself is blocked, hence python.
LONG_TOOL_PROMPT = (
    "Use the Bash tool, NOT in background mode, to run exactly this command: "
    'python3 -c "import time; time.sleep(40); print(1)"   '
    "Then reply with exactly: DONE"
)


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

    Every activity edge is recorded on `host.activity`, with whether a process
    was running when it arrived. Against the fakes a spawn is instant, so this
    is the only place the claim that the signal precedes the process can be
    checked at all.
    """
    workdir = tmp_path / "agent-cwd"
    workdir.mkdir()

    def agent_config(topic: str, name: str) -> AgentConfig:
        return AgentConfig(
            cwd=workdir,
            append_system_prompt="Answer in as few words as possible.",
            extra_args=["--model", "haiku"],
        )

    edges: list[tuple[str, str, bool, bool]] = []

    def on_activity(topic: str, name: str, active: bool) -> None:
        session = next(s for s in host.live_sessions(topic) if s.name == name)
        edges.append((topic, name, active, session.process_alive))

    host = AgentHost(
        agent_config,
        on_activity=on_activity,
        store=FileTranscriptStore(tmp_path / "transcripts"),
    )
    host.activity = edges
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
    first_id = session.agent_session_id
    assert first_id

    # An eviction, not a crash: the process goes and the conversation stays.
    await session.stop_process()
    assert session.process_alive is False

    await run_turn(session, "What word did I ask you to remember? Reply with just the word.")

    assert session.agent_session_id == first_id, "the second process resumed a different session"
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


async def test_the_activity_signal_brackets_a_real_turn(live_host):
    """Up before there is a process, down once the agent has actually stopped."""
    session = await live_host.session("live-topic", "s1")
    assert not session.active

    await run_turn(session, "Reply with exactly: PONG")

    assert not session.active
    assert live_host.activity == [
        # No process yet: this edge is the submit, not the turn. A signal that
        # waited for the ack would have shown nothing for the whole spawn.
        ("live-topic", "s1", True, False),
        ("live-topic", "s1", False, True),
    ]


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


async def test_a_background_tasks_command_and_output_reach_the_client(live_host):
    """`task_start.command` and `task_end.output`, forwarded from a real agent-proxy.

    `_forward` copies a message's fields rather than re-encoding them, so
    nothing here had to change for agent-proxy's own newest task fields to
    reach a client — this exists to prove that promise still holds against a
    real process, not just the fixtures, which is the only way `_forward`'s
    generality is actually exercised for a field this project never named.
    """
    session = await live_host.session("live-topic", "s1")
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)

    await run_turn(session, BACKGROUND_TASK_PROMPT)

    deadline = asyncio.get_running_loop().time() + TURN_TIMEOUT_S
    while "task_end" not in subscriber.types() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)

    start = subscriber.of_type("task_start")[0]
    assert "sleep" in (start.get("command") or ""), start

    end = subscriber.of_type("task_end")[0]
    assert "finished" in (end.get("output") or ""), end


async def wait_for(subscriber: RecordingSubscriber, predicate, what: str) -> None:
    """Give a live turn time to produce the message `predicate` recognises."""
    deadline = asyncio.get_running_loop().time() + TURN_TIMEOUT_S
    while not any(predicate(m) for m in subscriber.messages):
        assert asyncio.get_running_loop().time() < deadline, f"never saw {what}"
        await asyncio.sleep(0.2)


async def test_a_real_turn_can_be_taken_back_and_the_conversation_goes_on(live_host):
    """A cancel mid-tool ends the turn as cancelled, and the next prompt is its own turn.

    The second half is the part only a real process can check: agent-proxy
    clears the prompt the inner claude may have handed back to its composer,
    and a turn typed on top of a leftover would be submitted glued to it.
    """
    session = await live_host.session("live-topic", "s1")
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)

    turn_id = await session.submit(LONG_TOOL_PROMPT)
    await wait_for(
        subscriber,
        lambda m: m.get("type") == "tool_use" and m.get("tool") == "Bash",
        "the Bash tool starting",
    )
    await asyncio.sleep(2)
    await session.cancel(turn_id)

    stopping = subscriber.of_type("turn_cancel")
    assert [m["turn"]["id"] for m in stopping] == [turn_id]
    assert "ack" not in subscriber.types()

    await wait_for(subscriber, lambda m: m.get("type") == "turn_end", "the turn ending")
    end = subscriber.of_type("turn_end")[0]
    assert end["turn"]["id"] == turn_id
    assert end["outcome"] == "cancelled", end
    assert end["basis"].startswith(("marker:interrupted", "cancel:")), end["basis"]
    assert not session.turn_active

    await run_turn(session, "Reply with exactly: AFTER")

    turns = (await session.snapshot())["turns"]
    assert [turn["outcome"] for turn in turns] == ["cancelled", "success"]
    replies = "".join(
        event.get("text", "") for event in turns[1]["events"] if event.get("type") == "text"
    )
    assert "AFTER" in replies.upper()
