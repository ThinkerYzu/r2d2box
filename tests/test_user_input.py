"""Structured input lifecycle, identity, fan-out and answer command correlation."""
import asyncio

import pytest
from fastapi import FastAPI

from r2d2box import AgentConfig, R2D2Box, build_argv
from asgi_client import websocket
from fake_proxy import FakeHost, RecordingSubscriber, wait_until


def question(raw_id="i-1", *, blocking=True):
    return {
        "id": raw_id, "blocking": blocking, "expires_at": "2026-11-02T00:00:00Z",
        "questions": [{
            "id": "q-1", "header": "Color", "question": "Which color?",
            "options": [{"id": "o-1", "label": "Blue", "description": "The sky"}],
            "multi_select": False, "allow_text": True, "secret": False,
        }],
    }


async def emit_question(proxy, raw_id="i-1", *, blocking=True):
    await proxy.emit({
        "type": "input_request", "turn": {"id": "t-1", "kind": "user"},
        "request": question(raw_id, blocking=blocking),
    })
    await proxy.drain()


@pytest.mark.parametrize("agent", [None, "codex"])
def test_question_bridge_is_always_enabled(agent):
    assert "--user-input" in build_argv(AgentConfig(agent=agent))
    argv = build_argv(AgentConfig(agent=agent, extra_args=["--", "hello"]))
    assert argv.index("--user-input") < argv.index("--")


async def test_answer_does_not_create_turn_or_record_secrets_and_broadcasts_state(started_session):
    session, proxy, turn_id = await started_session()
    peers = [RecordingSubscriber(), RecordingSubscriber()]
    for peer in peers:
        await session.attach(peer)
    await emit_question(proxy)
    request = session.input_requests[0]
    assert request["id"] != "i-1"
    assert request["turn"]["id"] == turn_id
    snapshot = await session.snapshot()
    assert snapshot["input_requests"] == (await session.status())["input_requests"]
    snapshot["input_requests"][0]["questions"].clear()
    assert session.input_requests[0]["questions"]
    await session.answer(request["id"], {"q-1": {"text": "private reply"}})
    await proxy.drain()
    answer = proxy.submits[-1]
    assert answer["type"] == "answer" and answer["request"] == "i-1"
    assert answer["answers"] == {"q-1": {"text": "private reply"}}
    assert session.input_requests[0]["state"] == "answering"
    assert [t.id for t in session.open_turns] == [turn_id]
    assert all(peer.messages[-1]["type"] == "input_answering" for peer in peers)
    assert "private reply" not in repr(await session.snapshot())
    assert "private reply" not in repr(peers[0].messages)
    with pytest.raises(ValueError, match="already being answered"):
        await session.answer(request["id"], dismiss=True)
    await proxy.emit({"type": "input_resolved", "request": "i-1", "reason": "answered",
                      "turn": {"id": "t-1", "kind": "user"}})
    await proxy.drain()
    assert not session.input_requests
    assert peers[0].messages[-1]["request"] == request["id"]


async def test_request_ids_do_not_survive_process_restarts(started_session):
    session, proxy, _ = await started_session()
    await emit_question(proxy)
    old_id = session.input_requests[0]["id"]
    await session.stop_process()
    assert not session.input_requests
    await session.submit("another turn")
    proxy = session._proxy
    await emit_question(proxy)
    assert session.input_requests[0]["id"] != old_id
    with pytest.raises(ValueError, match="no longer available"):
        await session.answer(old_id, dismiss=True)
    await session.answer(session.input_requests[0]["id"], dismiss=True)
    assert proxy.submits[-1]["dismiss"] is True
    assert "answers" not in proxy.submits[-1]


async def test_nonblocking_request_after_eot_remains_answerable_without_reopening_turn(started_session):
    session, proxy, _ = await started_session()
    await emit_question(proxy, blocking=False)
    own_id = session.input_requests[0]["id"]
    await proxy.end_turn("t-1")
    await proxy.drain()
    assert not session.open_turns
    assert session.active
    await session.answer(own_id, dismiss=True)
    await proxy.emit({"type": "input_resolved", "request": "i-1", "reason": "dismissed",
                      "turn": {"id": "t-1", "kind": "user"}})
    await proxy.drain()
    assert not session.open_turns
    assert not session.active
    assert len((await session.snapshot())["turns"]) == 1


async def test_refused_answer_is_local_and_can_be_retried(started_session):
    session, proxy, _ = await started_session()
    await emit_question(proxy)
    peer = RecordingSubscriber()
    await session.attach(peer)
    proxy.auto_ack = False
    own_id = session.input_requests[0]["id"]
    pending = asyncio.create_task(session.answer(own_id, {"q-1": {"choices": ["bad"]}}))
    await wait_until(lambda: proxy.submits[-1]["type"] == "answer")
    with pytest.raises(ValueError, match="already being answered"):
        await session.answer(own_id, dismiss=True)
    await proxy.reject(proxy.submits[-1]["ref"], "invalid choice")
    with pytest.raises(ValueError, match="invalid choice"):
        await pending
    assert not any(m["type"] == "error" for m in peer.messages)
    assert session.input_requests[0]["state"] == "pending"
    proxy.auto_ack = True
    await session.answer(own_id, dismiss=True)


@pytest.mark.parametrize("reason", ["expired", "cancelled", "backend_closed", "error", "dismissed"])
async def test_all_resolution_reasons_remove_live_forms(started_session, reason):
    session, proxy, _ = await started_session()
    await emit_question(proxy)
    await proxy.emit({"type": "input_resolved", "request": "i-1", "reason": reason})
    await proxy.drain()
    assert not session.input_requests


async def test_human_wait_is_not_evicted_by_turn_cap():
    host = FakeHost(idle_timeout_s=0, pending_evict_cap_s=0)
    try:
        session = await host.session("topic", "s1")
        await session.submit("ask me")
        await emit_question(host.proxies[("topic", "s1")])
        assert await host.evict_idle() == 0
        assert session.process_alive
    finally:
        await host.close()


async def test_websocket_answer_and_stale_errors_are_correlated():
    host = FakeHost()
    box = R2D2Box(host=host)
    app = FastAPI()
    app.include_router(box.router, prefix="/chat")
    try:
        async with websocket(app, "/chat/ws") as client:
            await client.send_json({"type": "attach", "topic": "topic", "session": "s1"})
            await client.receive_json()
            session = await host.session("topic", "s1")
            await session.submit("ask")
            assert (await client.receive_json())["type"] == "turn_prompt"
            proxy = host.proxies[("topic", "s1")]
            await emit_question(proxy)
            request = (await client.receive_json())["request"]
            await client.send_json({"type": "answer", "request": request["id"], "dismiss": True})
            assert (await client.receive_json())["type"] == "input_answering"
            await client.send_json({"type": "answer", "request": "old", "dismiss": True})
            error = await client.receive_json()
            assert error["scope"] == "connection" and error["request"] == "old"
            assert "seq" not in error
    finally:
        await box.aclose()


async def test_late_request_and_late_answer_ack_never_reopen_ended_turn(started_session):
    session, proxy, _ = await started_session()
    await proxy.end_turn("t-1")
    await proxy.drain()
    await emit_question(proxy, blocking=False)
    assert not session.open_turns
    request = session.input_requests[0]["id"]
    proxy.auto_ack = False
    caller = asyncio.create_task(session.answer(request, dismiss=True))
    await wait_until(lambda: proxy.submits[-1]["type"] == "answer")
    command = proxy.submits[-1]
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await proxy.emit({"type": "ack", "request": "i-1", "ref": command["ref"],
                      "turn": {"id": "t-1", "kind": "user"}})
    await proxy.drain()
    assert not session.open_turns
    assert session.input_requests[0]["state"] == "answering"


async def test_process_exit_wakes_an_answer_waiter_and_clears_forms(started_session):
    session, proxy, _ = await started_session()
    await emit_question(proxy)
    proxy.auto_ack = False
    caller = asyncio.create_task(session.answer(session.input_requests[0]["id"], dismiss=True))
    await wait_until(lambda: proxy.submits[-1]["type"] == "answer")
    await proxy.end_stream()
    with pytest.raises(ConnectionError, match="exited"):
        await caller
    await wait_until(lambda: not session.input_requests)
    assert not session.active


async def test_permission_metadata_survives_broadcast_and_reconnect_snapshot(started_session):
    session, proxy, _ = await started_session()
    payload = question()
    payload.update(kind="permission", permission={
        "agent": "codex", "method": "item/commandExecution/requestApproval",
        "operation": {"command": "printf hello", "cwd": "/work"},
        "item": {"id": "native-item"}, "scope": "operation",
    })
    payload["questions"][0].update(allow_text=False, options=[
        {"id": "deny", "label": "Deny", "description": "Do not run"},
    ])
    peer = RecordingSubscriber()
    await session.attach(peer)
    await proxy.emit({"type": "input_request", "request": payload,
                      "turn": {"id": "t-1", "kind": "user"}})
    await proxy.drain()
    broadcast = peer.messages[-1]
    assert broadcast["request"]["permission"] == payload["permission"]
    snapshot = await session.snapshot()
    live = snapshot["input_requests"][0]
    assert live["permission"] == payload["permission"]
    assert live["kind"] == "permission"
    await session.answer(live["id"], {"q-1": {"choices": ["deny"]}})
    assert proxy.submits[-1]["answers"] == {"q-1": {"choices": ["deny"]}}
