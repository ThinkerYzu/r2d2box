"""A real codex conversation through a real agent host: a tool call, then a resume.

The claude live tests prove the session layer against claude; this proves the
same layer against `agent-proxy --agent codex`, whose session id is a codex
thread id and whose turns end on codex's own `turn/completed` rather than on
anything read off a screen. Nothing below `AgentHost` is faked.

Doubly opt-in like the rest of the live tier, and also skipped without `codex`
on `PATH`, signed in with ChatGPT (agent-proxy refuses an API key):

    R2D2BOX_RUN_LIVE=1 pytest -m live
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

import pytest

from r2d2box import AgentConfig, AgentHost, FileTranscriptStore

pytestmark = pytest.mark.live

TURN_TIMEOUT_S = 240


@pytest.fixture
def require_codex():
    """Skip unless the live tier is asked for and an agent-proxy that drives codex is installed."""
    if not os.environ.get("R2D2BOX_RUN_LIVE"):
        pytest.skip("set R2D2BOX_RUN_LIVE=1 to run the live tier")
    if shutil.which("agent-proxy") is None:
        pytest.skip("agent-proxy is not on PATH")
    if shutil.which("codex") is None:
        pytest.skip("codex is not on PATH")
    usage = subprocess.run(
        ["agent-proxy", "--help"], capture_output=True, text=True, timeout=30
    ).stdout
    if "codex" not in usage:
        pytest.skip("this agent-proxy predates --agent codex")


@pytest.fixture
async def codex_host(require_codex, tmp_path):
    """An `AgentHost` running codex in a scratch directory, closed at teardown.

    `-c model_reasoning_effort=low` keeps the turns quick, and is also the one
    argument whose meaning `build_argv` changes under codex: under claude `-c`
    is `--continue` and is refused.
    """
    workdir = tmp_path / "agent-cwd"
    workdir.mkdir()
    (workdir / "word.txt").write_text("PELICAN\n")

    def agent_config(topic: str, name: str) -> AgentConfig:
        return AgentConfig(
            agent="codex",
            cwd=workdir,
            append_system_prompt="Answer in as few words as possible.",
            extra_args=["--sandbox", "read-only", "-c", "model_reasoning_effort=low"],
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


async def test_a_codex_conversation_survives_losing_its_process(codex_host):
    """A command, an eviction, and the resumed thread still knows what the command read."""
    session = await codex_host.session("live-codex", "s1")

    await run_turn(session, "Run `cat word.txt` in the shell, then reply with exactly: READ")
    assert session.agent == "codex"
    thread_id = session.agent_session_id
    assert thread_id

    await session.stop_process()
    assert session.process_alive is False

    await run_turn(session, "What word did word.txt contain? Reply with just the word.")
    assert session.agent_session_id == thread_id, "the second process opened a different thread"

    turns = (await session.snapshot())["turns"]
    assert [turn["outcome"] for turn in turns] == ["success", "success"]
    ends = [e for turn in turns for e in turn["events"] if e.get("type") == "turn_end"]
    assert [e["basis"] for e in ends] == ["codex:completed", "codex:completed"]
    tools = [e["tool"] for e in turns[0]["events"] if e.get("type") == "tool_use"]
    assert "commandExecution" in tools, f"the first turn ran no command: {tools}"

    reply = "".join(e.get("text", "") for e in turns[1]["events"] if e.get("type") == "text")
    assert "PELICAN" in reply.upper(), "the resumed thread did not remember the first turn"
