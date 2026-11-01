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
        agent=agent, cwd=tmp_path, extra_args=extra,
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


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_live_permission_decision_controls_execution(agent, decision, tmp_path):
    if os.environ.get("R2D2BOX_RUN_LIVE") != "1":
        pytest.skip("set R2D2BOX_RUN_LIVE=1 to run the live tier")
    if not shutil.which("agent-proxy") or not shutil.which(agent):
        pytest.skip("agent-proxy and the backend binary must be installed")
    import json
    import shlex

    work = tmp_path / "work"
    work.mkdir()
    marker = tmp_path / "approved-marker"
    command = f"printf approved > {shlex.quote(str(marker))}"
    if agent == "claude":
        extra = ["--model", "haiku", "--settings", json.dumps({"permissions": {"ask": ["Bash"]}})]
        prompt = (
            f"Use Bash exactly once to run: {command} . "
            "Do not use other tools or retry if denied. After the result, reply DONE."
        )
    else:
        extra = ["-c", "model_reasoning_effort=low"]
        prompt = (
            'Execute exactly this shell command with sandbox_permissions="require_escalated" '
            'and justification="Write the permission test marker outside the workspace": '
            f"{command} . This is a test of client approval. "
            "Do not try without escalation or use other tools or retry if denied. Then reply DONE."
        )
    host = AgentHost(lambda topic, name: AgentConfig(
        agent=agent, cwd=work, bypass_permissions=False, extra_args=extra,
    ))
    try:
        session = await host.session("live-permissions", "s1")
        subscriber = RecordingSubscriber()
        await session.attach(subscriber)
        await session.submit(prompt)
        await wait_until(
            lambda: session.input_requests or not session.pending_turns,
            timeout_s=120, what="a permission request",
        )
        assert session.input_requests, "the agent ended without a permission request"
        request = session.input_requests[0]
        assert request["kind"] == "permission"
        assert request["permission"]["scope"] == "operation"
        assert not marker.exists(), "operation ran before client approval"
        answers = {}
        expected = "allow once" if decision == "allow" else "deny"
        for question in request["questions"]:
            option = next(o for o in question["options"] if o["label"].lower() == expected)
            answers[question["id"]] = {"choices": [option["id"]]}
        await session.answer(request["id"], answers)
        await wait_until(
            lambda: not session.input_requests and not session.pending_turns,
            timeout_s=120, what="permission resolution and the turn ending",
        )
        assert marker.exists() == (decision == "allow")
        if marker.exists():
            assert marker.read_text() == "approved"
        resolutions = [m for m in subscriber.messages if m["type"] == "input_resolved"]
        assert len(resolutions) == 1 and resolutions[0]["reason"] == "answered"
        assert not [m for m in subscriber.messages if m["type"] == "error"]
        assert len((await session.snapshot())["turns"]) == 1
    finally:
        await host.close()
