"""The registry against real subprocesses rather than a stand-in.

`test_host.py` proves the registry's logic with a fake in place of the process.
These are the cases that a fake cannot show: that a config really becomes an
argv, that a respawn really carries `--resume`, and that a terminated process
really lets go.

Still no `claude` anywhere — `scripted_proxy.py` is the binary, reached through
the same `build_argv` production uses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from r2d2box import AgentConfig, AgentHost, FileTranscriptStore

from conftest import FIXTURES_DIR, SCRIPTED_PROXY


class ScriptedHost(AgentHost):
    """An `AgentHost` that spawns `scripted_proxy.py` replaying one fixture.

    `argvs()` gives the full command line of every process started so far,
    which is how a resume is checked from the outside — the flag has to be on
    the real argv, not merely in a variable the session was holding.
    """

    def __init__(self, fixture: Path, argv_out: Path, **fields) -> None:
        def agent_config(topic: str, name: str) -> AgentConfig:
            return AgentConfig(
                proxy_bin=str(SCRIPTED_PROXY),
                append_system_prompt=f"you are talking about {topic}",
                extra_args=["--script", str(fixture), "--argv-out", str(argv_out)],
            )

        super().__init__(agent_config, **fields)
        self._argv_out = argv_out

    def argvs(self) -> list[list[str]]:
        if not self._argv_out.is_file():
            return []
        text = self._argv_out.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
async def scripted_host(tmp_path):
    """A factory for a `ScriptedHost`, guaranteed closed at teardown.

    Call it with a fixture file name and any `AgentHost` keyword. Closing the
    host is what stops the subprocesses, so a test that fails partway through
    still leaves none behind.
    """
    built = []

    def make(fixture: str, **fields) -> ScriptedHost:
        script = FIXTURES_DIR / fixture
        assert script.exists(), f"no such fixture: {script}"
        host = ScriptedHost(script, tmp_path / "argv.jsonl", **fields)
        built.append(host)
        return host

    yield make
    for host in built:
        await host.close()


async def run_turn(host: AgentHost, topic: str, name: str, text: str) -> list[dict]:
    """Submit one prompt and return the turn's events once it has finished."""
    session = await host.session(topic, name)
    turn_id = await session.submit(text)
    await _wait_for_turn_end(session, turn_id)
    stored = await host.store.read_turns(topic, name)
    return [turn.events for turn in stored if turn.id == turn_id][-1]


async def _wait_for_turn_end(session, turn_id: str, timeout_s: float = 5.0) -> None:
    """Block until `turn_id` is no longer running, or fail the test.

    The scripted proxy writes a whole turn as fast as the pipe takes it, but
    the session reads it on its own task, so a test that asserts immediately
    after `submit` is racing the reader.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while session.pending_turns and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert not session.pending_turns, f"turn {turn_id} never ended"


async def _wait_for_process_gone(session, timeout_s: float = 5.0) -> None:
    """Block until the session has finished with a process that exited on its own.

    A script that ends by exiting writes its `turn_end` first, so
    `_wait_for_turn_end` returns while the process is still on its way out.
    Everything that follows — the EOF, the reap, `process_exited` — happens on
    the read pump's task, and asserting on the process without waiting for it
    is a coin toss.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while session.process_alive and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert session.process_alive is False, "the process never went away"


async def test_two_sessions_under_one_topic_hold_independent_conversations(scripted_host):
    """Each session gets its own process, and neither transcript knows the other."""
    host = scripted_host("one-turn.jsonl")

    first = await run_turn(host, "bug-1992198", "s1", "what host is this?")
    second = await run_turn(host, "bug-1992198", "s2", "and the kernel?")

    assert [event["type"] for event in first] == [
        "turn_start", "text", "tool_use", "tool_result", "text", "turn_end",
    ]
    assert [event["type"] for event in second] == [event["type"] for event in first]

    s1 = await (await host.session("bug-1992198", "s1")).snapshot()
    s2 = await (await host.session("bug-1992198", "s2")).snapshot()
    assert [turn["user"] for turn in s1["turns"]] == ["what host is this?"]
    assert [turn["user"] for turn in s2["turns"]] == ["and the kernel?"]
    assert len(host.argvs()) == 2


async def test_a_killed_process_resumes_with_its_history(scripted_host, tmp_path):
    """The transcript is on disk and the second process is told which conversation it is."""
    host = scripted_host(
        "one-turn.jsonl", store=FileTranscriptStore(tmp_path / "transcripts")
    )

    # `one-turn.jsonl` exits at the end of the turn, so the process is gone
    # before the second prompt — the crash case, without having to kill anything.
    await run_turn(host, "bug-1", "s1", "first question")
    session = await host.session("bug-1", "s1")
    await _wait_for_process_gone(session)
    assert session.claude_session_id == "scripted-session-0001"

    await run_turn(host, "bug-1", "s1", "second question")

    argvs = host.argvs()
    assert len(argvs) == 2
    assert "--resume" not in argvs[0]
    assert argvs[1][argvs[1].index("--resume") + 1] == "scripted-session-0001"

    snapshot = await session.snapshot()
    assert [turn["user"] for turn in snapshot["turns"]] == [
        "first question", "second question",
    ]


async def test_the_transcript_outlives_the_whole_host(scripted_host, tmp_path):
    """A restarted host reads the conversation back from the store, as after a deploy."""
    root = tmp_path / "transcripts"
    first_host = scripted_host("one-turn.jsonl", store=FileTranscriptStore(root))
    await run_turn(first_host, "bug-1", "s1", "before the restart")
    await first_host.close()

    second_host = scripted_host("one-turn.jsonl", store=FileTranscriptStore(root))
    revived = await second_host.session("bug-1", "s1")
    snapshot = await revived.snapshot()

    assert [turn["user"] for turn in snapshot["turns"]] == ["before the restart"]
    assert snapshot["process_alive"] is False


async def test_an_evicted_sessions_next_turn_works(scripted_host):
    """A real process is terminated mid-life, and the client never learns it happened."""
    host = scripted_host("turn-then-wait.jsonl", idle_timeout_s=60)

    await run_turn(host, "bug-1", "s1", "first question")
    session = await host.session("bug-1", "s1")
    assert session.process_alive is True

    session.last_active -= 120
    assert await host.evict_idle() == 1
    assert session.process_alive is False

    await run_turn(host, "bug-1", "s1", "second question")

    assert session.process_alive is True
    argvs = host.argvs()
    assert argvs[1][argvs[1].index("--resume") + 1] == "scripted-session-0001"
    assert [turn["user"] for turn in (await session.snapshot())["turns"]] == [
        "first question", "second question",
    ]


async def test_the_hosts_config_callback_reaches_the_real_command_line(scripted_host):
    """what the callback returned is what ran."""
    host = scripted_host("one-turn.jsonl")
    await run_turn(host, "bug-1992198", "s1", "hello")

    argv = host.argvs()[0]
    assert argv[argv.index("--append-system-prompt") + 1] == (
        "you are talking about bug-1992198"
    )
