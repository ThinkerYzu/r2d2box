"""`AgentConfig` to argv: the whole of what a host can ask agent-proxy for."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from r2d2box import AgentConfig, build_argv


def test_empty_config_runs_a_bare_proxy():
    assert build_argv(AgentConfig()) == ["agent-proxy"]


def test_proxy_bin_replaces_the_default():
    assert build_argv(AgentConfig(proxy_bin="/opt/bin/agent-proxy")) == [
        "/opt/bin/agent-proxy"
    ]


def test_every_field_maps_to_its_flag():
    config = AgentConfig(
        cwd=Path("/home/thinker/progm/claudebugzilla"),
        append_system_prompt="You are reviewing bug 1992198.",
        allowed_tools=["mcp__bzdash__worklog_append", "mcp__bzdash__bug_metadata"],
        mcp_config={"mcpServers": {"bzdash": {"command": "python3"}}},
        extra_args=["--model", "opus"],
    )
    assert build_argv(config) == [
        "agent-proxy",
        "--append-system-prompt", "You are reviewing bug 1992198.",
        "--allowedTools", "mcp__bzdash__worklog_append", "mcp__bzdash__bug_metadata",
        "--mcp-config", json.dumps({"mcpServers": {"bzdash": {"command": "python3"}}}),
        "--model", "opus",
    ]


def test_cwd_is_not_an_argument():
    """`cwd` is the subprocess's working directory, not something agent-proxy parses."""
    assert build_argv(AgentConfig(cwd=Path("/tmp"))) == ["agent-proxy"]


def test_mcp_config_is_serialized_here():
    argv = build_argv(AgentConfig(mcp_config={"mcpServers": {"a": {"args": ["--x"]}}}))
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {
        "mcpServers": {"a": {"args": ["--x"]}}
    }


def test_resume_is_added_by_the_caller_not_the_config():
    argv = build_argv(AgentConfig(), resume="4f0d-abc")
    assert argv == ["agent-proxy", "--resume", "4f0d-abc"]


def test_resume_lands_before_extra_args():
    """A `--` in extra_args ends agent-proxy's flag parsing, so resume cannot follow it.

    Everything after `--` reaches the inner claude as prompt text rather than as
    options, which would turn the resume into a word the model reads and start a
    brand new conversation instead of continuing one.
    """
    argv = build_argv(AgentConfig(extra_args=["--", "hello"]), resume="4f0d-abc")
    assert argv == ["agent-proxy", "--resume", "4f0d-abc", "--", "hello"]
    assert argv.index("--resume") < argv.index("--")


def test_no_resume_means_no_flag():
    assert "--resume" not in build_argv(AgentConfig(), resume=None)
    assert "--resume" not in build_argv(AgentConfig(), resume="")


def test_empty_collections_add_nothing():
    assert build_argv(AgentConfig(allowed_tools=[], mcp_config={}, extra_args=[])) == [
        "agent-proxy"
    ]


@pytest.mark.parametrize(
    "flag",
    ["--session-id", "--resume", "-r", "--continue", "-c", "--fork-session",
     "--session-id=abc", "--resume=abc"],
)
def test_session_flags_in_extra_args_are_refused(flag):
    """The session layer owns the conversation; extra_args may not second-guess it.

    agent-proxy lets the last session flag win, so one hidden in extra_args
    would take the session somewhere r2d2box does not know about, and the
    transcript would go on being written under the id it thinks is running.
    """
    with pytest.raises(ValueError, match="session flag"):
        build_argv(AgentConfig(extra_args=[flag, "x"]), resume="4f0d-abc")


def test_session_flags_are_refused_even_without_a_resume():
    with pytest.raises(ValueError):
        build_argv(AgentConfig(extra_args=["--continue"]))
