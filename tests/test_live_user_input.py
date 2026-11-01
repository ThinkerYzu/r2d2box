"""Opt-in real backend question/answer round trips through the session layer."""
import os
import shutil

import pytest

from r2d2box import AgentConfig, AgentHost
from fake_proxy import RecordingSubscriber, wait_until

pytestmark = pytest.mark.live


@pytest.mark.parametrize("agent", ["claude", "codex"])
async def test_live_structured_question_answer(agent, tmp_path):
    if os.environ.get("R2D2BOX_RUN_LIVE") != "1":
        pytest.skip("set R2D2BOX_RUN_LIVE=1 to run the live tier")
    if not shutil.which("agent-proxy") or not shutil.which(agent):
        pytest.skip("agent-proxy and the backend binary must be installed")
    tool = "AskUserQuestion" if agent == "claude" else "request_user_input"
    prompt = (
        f"Use {tool} exactly once to ask Which color? with header Color and "
        "two options Blue (blue) and Green (green). Wait for my answer. "
        "Then reply exactly INPUT_RECEIVED followed by the selected color. "
        "Do not choose for me and do not use other tools."
    )
    extra = ["--model", "haiku"] if agent == "claude" else ["-c", "model_reasoning_effort=low"]
    host = AgentHost(lambda topic, name: AgentConfig(
        agent=agent, cwd=tmp_path, user_input=True, extra_args=extra,
    ))
    try:
        session = await host.session("live-input", "s1")
        subscriber = RecordingSubscriber()
        await session.attach(subscriber)
        turn_id = await session.submit(prompt)
        await wait_until(lambda: session.input_requests, timeout_s=120, what="a structured question")
        request = session.input_requests[0]
        answers = {}
        for question in request["questions"]:
            option = next(o for o in question["options"] if o["label"].lower() == "green")
            answers[question["id"]] = {"choices": [option["id"]]}
        await session.answer(request["id"], answers)
        await wait_until(
            lambda: not session.input_requests and not session.pending_turns,
            timeout_s=120, what="answer delivery and the original turn ending",
        )
        snapshot = await session.snapshot()
        assert len(snapshot["turns"]) == 1
        assert snapshot["turns"][0]["id"] == turn_id
        assert snapshot["turns"][0]["outcome"] == "success"
        text = " ".join(m.get("text", "") for m in subscriber.messages if m["type"] == "text")
        assert "INPUT_RECEIVED" in text and "green" in text.lower()
        resolved = [m for m in subscriber.messages if m["type"] == "input_resolved"]
        assert len(resolved) == 1 and resolved[0]["reason"] == "answered"
        assert resolved[0]["request"] == request["id"]
        assert any(m["type"] == "input_answering" for m in subscriber.messages)
    finally:
        await host.close()
