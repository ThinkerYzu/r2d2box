"""One real turn through a real agent-proxy.

The scripted-proxy tests prove the I/O path; this proves the argv. `build_argv`
is a guess about another program's command line until something runs it, and the
flags it emits are the ones a host cannot work around.

It spends real quota and takes a minute or two, so it is doubly opt-in — the
`live` marker *and* an environment variable, following agent-proxy's own
convention, because a marker alone still runs on a bare `pytest`:

    R2D2BOX_RUN_LIVE=1 pytest -m live

Worth running when agent-proxy or `claude` changes version, and before trusting
a change to `build_argv`.
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from r2d2box import AgentConfig, AgentProxy

pytestmark = pytest.mark.live

TURN_TIMEOUT_S = 240


@pytest.fixture
def require_agent_proxy():
    """Skip unless the live tier is asked for and both binaries are installed."""
    if not os.environ.get("R2D2BOX_RUN_LIVE"):
        pytest.skip("set R2D2BOX_RUN_LIVE=1 to run the live tier")
    if shutil.which("agent-proxy") is None:
        pytest.skip("agent-proxy is not on PATH")
    if shutil.which("claude") is None:
        pytest.skip("claude is not on PATH")


async def test_a_real_turn_runs_end_to_end(require_agent_proxy, tmp_path):
    """Submit a prompt to a real agent and read its turn back, matching on the turn id.

    `cwd` is a scratch directory so the session this mints does not land among
    the project's own claude transcripts.
    """
    proxy = await AgentProxy.start(
        AgentConfig(
            cwd=tmp_path,
            append_system_prompt="Answer in as few words as possible.",
            extra_args=["--model", "haiku"],
        ),
        tag="live/one-turn",
    )
    try:
        assert proxy.session_id
        await proxy.submit("Reply with exactly: PONG", ref="c-1")
        turn = await asyncio.wait_for(_read_one_turn(proxy, "c-1"), TURN_TIMEOUT_S)
    finally:
        await proxy.close()

    assert turn["id"], "the ack never arrived, so no turn id was ever claimed"
    assert turn["end"]["outcome"] == "success"
    assert "PONG" in "".join(turn["text"]).upper()


async def _read_one_turn(proxy: AgentProxy, ref: str) -> dict:
    """Read until the turn claimed by `ref` ends, returning its id, prose and end.

    Written the way agent-proxy's API.md says a client must: claim the id from
    the `ack` that echoes the ref, then match every later message on `turn.id`
    and ignore the rest. An unowned turn the agent starts on its own goes past
    on the same stream and is not this one.
    """
    turn: dict = {"id": None, "text": [], "end": None}
    async for message in proxy.messages():
        kind = message.get("type")
        if kind == "ack" and message.get("ref") == ref:
            turn["id"] = message["turn"]["id"]
            continue
        if turn["id"] is None or (message.get("turn") or {}).get("id") != turn["id"]:
            continue
        if kind == "text":
            turn["text"].append(message.get("text", ""))
        elif kind == "turn_end":
            turn["end"] = message
            return turn
    raise AssertionError("agent-proxy's stream ended before the turn did")
