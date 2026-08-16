"""The process layer against a real subprocess: one turn, and the ways a stream goes wrong."""

from __future__ import annotations

import pytest

from r2d2box import AgentConfig, AgentProxy, ProxyStartError, build_argv
from r2d2box.proxy import STREAM_LIMIT

from conftest import SCRIPTED_PROXY


async def collect(proxy: AgentProxy) -> list[dict]:
    """Every message the proxy emits, to EOF."""
    return [message async for message in proxy.messages()]


async def test_start_reads_ready_and_keeps_the_session_id(start_scripted):
    proxy = await start_scripted("silent.jsonl")
    assert proxy.session_id == "scripted-session-0001"
    assert proxy.alive


async def test_ready_is_not_yielded_to_the_caller(start_scripted):
    """`ready` is consumed at start, so no layer above needs a first-message case."""
    proxy = await start_scripted("junk-line.jsonl")
    messages = await collect(proxy)
    assert "ready" not in [m["type"] for m in messages]


async def test_one_turn_arrives_in_order(start_scripted):
    proxy = await start_scripted("one-turn.jsonl")
    await proxy.submit("what host is this?", ref="c-1")
    messages = await collect(proxy)

    assert [m["type"] for m in messages] == [
        "ack", "turn_start", "text", "tool_use", "tool_result", "text", "turn_end",
    ]
    ack = messages[0]
    assert ack["ref"] == "c-1"
    turn_id = ack["turn"]["id"]
    assert all(m["turn"]["id"] == turn_id for m in messages)
    assert messages[-1]["outcome"] == "success"


async def test_envelope_fields_pass_through_untouched(start_scripted):
    """messages are forwarded, not re-encoded — `seq` and `turn` survive."""
    proxy = await start_scripted("one-turn.jsonl")
    await proxy.submit("what host is this?", ref="c-1")
    messages = await collect(proxy)

    # `ready` took seq 1, so the turn starts at 2 and runs without gaps.
    assert [m["seq"] for m in messages] == list(range(2, 2 + len(messages)))
    assert all("outstanding" in m for m in messages)
    tool_use = next(m for m in messages if m["type"] == "tool_use")
    assert tool_use["input"] == {"file_path": "/etc/hostname"}


async def test_oversized_line_becomes_an_error_and_the_turn_continues(start_scripted):
    """The 16 MiB failure both apps hit: the line is lost, the session is not.

    `readline()` discards its buffer and raises before the caller ever sees the
    line, so the only honest report is that one message went missing. What must
    not happen is the reader dying and the rest of the turn going unread.
    """
    proxy = await start_scripted("oversized-tool-result.jsonl")
    await proxy.submit("read the log", ref="c-1")
    messages = await collect(proxy)

    assert [m["type"] for m in messages] == [
        "ack", "turn_start", "tool_use", "error", "text", "turn_end",
    ]
    assert str(STREAM_LIMIT) in messages[3]["error"]
    assert messages[-1]["outcome"] == "success"


async def test_oversized_line_reports_once_not_per_fragment(start_scripted):
    """The tail of the dropped line arrives as junk; it must not become more messages."""
    proxy = await start_scripted("oversized-tool-result.jsonl")
    await proxy.submit("read the log", ref="c-1")
    types = [m["type"] for m in await collect(proxy)]

    assert types.count("error") == 1
    assert "raw" not in types


async def test_non_json_line_becomes_a_raw_message(start_scripted):
    proxy = await start_scripted("junk-line.jsonl")
    messages = await collect(proxy)

    assert [m["type"] for m in messages] == ["text", "raw", "text"]
    assert messages[1] == {"type": "raw", "text": "this is not json"}
    assert [m.get("text") for m in messages] == ["before", "this is not json", "after"]


async def test_iteration_ends_when_the_process_closes_stdout(start_scripted):
    """EOF ends the iterator rather than hanging or raising.

    The scripted proxy runs off the end of its script and exits; reaching the
    assertion at all is the half of this that matters.
    """
    proxy = await start_scripted("junk-line.jsonl")
    assert len(await collect(proxy)) == 3


async def test_messages_is_single_consumer(start_scripted):
    """One stdout pipe, one reader — a second iteration would silently steal messages."""
    proxy = await start_scripted("silent.jsonl")
    proxy.messages()
    with pytest.raises(RuntimeError, match="single-consumer"):
        proxy.messages()


async def test_submit_carries_its_ref(start_scripted):
    """The ref is what claims the turn id; counting acks goes off by one on a rejection."""
    proxy = await start_scripted("one-turn.jsonl")
    await proxy.submit("hello", ref="c-42")
    messages = await collect(proxy)
    assert messages[0]["ref"] == "c-42"


async def test_close_stops_the_process(start_scripted):
    proxy = await start_scripted("silent.jsonl")
    assert proxy.alive
    await proxy.close()
    assert not proxy.alive
    assert proxy.returncode is not None


async def test_close_is_idempotent(start_scripted):
    proxy = await start_scripted("silent.jsonl")
    await proxy.close()
    await proxy.close()


async def test_send_after_close_raises_connection_error(start_scripted):
    proxy = await start_scripted("silent.jsonl")
    await proxy.close()
    with pytest.raises(ConnectionError):
        await proxy.submit("too late", ref="c-1")


async def test_missing_binary_raises_proxy_start_error():
    config = AgentConfig(proxy_bin="/nonexistent/agent-proxy")
    with pytest.raises(ProxyStartError, match="not found on PATH"):
        await AgentProxy.start(config)


async def test_a_proxy_that_says_nothing_fails_to_start(tmp_path):
    """No `ready` means no session id, and nothing above can work without one."""
    script = tmp_path / "empty.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    with pytest.raises(ProxyStartError, match="before emitting ready"):
        await AgentProxy.start(AgentConfig(proxy_bin=str(script)))


async def test_a_refused_start_reports_the_proxys_own_error(tmp_path):
    """agent-proxy's one documented no-ready case: an error line, and no session."""
    script = tmp_path / "refuse.sh"
    script.write_text(
        '#!/bin/sh\necho \'{"seq":1,"type":"error","error":"bad session flags"}\'\n'
    )
    script.chmod(0o755)
    with pytest.raises(ProxyStartError, match="bad session flags"):
        await AgentProxy.start(AgentConfig(proxy_bin=str(script)))


async def test_cwd_is_the_subprocess_working_directory(tmp_path, scripted_config):
    """`cwd` decides which directory's claude sessions the agent can resume."""
    workdir = tmp_path / "here"
    workdir.mkdir()
    proxy = await AgentProxy.start(scripted_config("silent.jsonl", cwd=workdir))
    try:
        assert proxy.alive
    finally:
        await proxy.close()


async def test_the_fixture_is_reached_through_the_real_build_argv(scripted_config):
    """The scripted proxy runs on the same argv production builds, not a shortcut."""
    config = scripted_config("silent.jsonl", append_system_prompt="be brief")
    argv = build_argv(config)
    assert argv[0] == str(SCRIPTED_PROXY)
    assert argv[1:3] == ["--append-system-prompt", "be brief"]

    proxy = await AgentProxy.start(config)
    try:
        assert proxy.session_id == "scripted-session-0001"
    finally:
        await proxy.close()
