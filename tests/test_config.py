"""`AgentConfig` to argv: the whole of what a host can ask agent-proxy for."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from r2d2box import AgentConfig, agent_of, build_argv


def test_empty_config_runs_a_bare_proxy():
    assert build_argv(AgentConfig()) == ["agent-proxy"]


def test_proxy_bin_replaces_the_default():
    assert build_argv(AgentConfig(proxy_bin="/opt/bin/agent-proxy")) == [
        "/opt/bin/agent-proxy"
    ]


def test_every_field_maps_to_its_flag():
    config = AgentConfig(
        cwd=Path("/home/dev/project"),
        append_system_prompt="You are reviewing bug 1992198.",
        allowed_tools=["mcp__notes__worklog_append", "mcp__notes__item_metadata"],
        mcp_config={"mcpServers": {"notes": {"command": "python3"}}},
        extra_args=["--model", "opus"],
    )
    assert build_argv(config) == [
        "agent-proxy",
        "--append-system-prompt", "You are reviewing bug 1992198.",
        "--allowedTools", "mcp__notes__worklog_append", "mcp__notes__item_metadata",
        "--mcp-config", json.dumps({"mcpServers": {"notes": {"command": "python3"}}}),
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


def test_agent_is_the_first_argument():
    assert build_argv(AgentConfig(agent="codex", extra_args=["--model", "gpt-5"]),
                      resume="thread-1") == [
        "agent-proxy", "--agent", "codex", "--resume", "thread-1", "--model", "gpt-5",
    ]


def test_no_agent_passes_no_flag():
    """`None` is claude, and runs on an agent-proxy that predates `--agent`."""
    assert build_argv(AgentConfig()) == ["agent-proxy"]
    assert build_argv(AgentConfig(agent="claude")) == ["agent-proxy", "--agent", "claude"]


def test_agent_of_names_the_default():
    assert agent_of(AgentConfig()) == "claude"
    assert agent_of(AgentConfig(agent="codex")) == "codex"


@pytest.mark.parametrize(
    "fields",
    [{"allowed_tools": ["mcp__notes__read"]},
     {"mcp_config": {"mcpServers": {"notes": {"command": "python3"}}}}],
)
def test_codex_refuses_what_only_claude_has(fields):
    """agent-proxy would refuse these too, but only once the process was running."""
    with pytest.raises(ValueError, match="no codex equivalent"):
        build_argv(AgentConfig(agent="codex", **fields))


def test_codex_keeps_the_system_prompt():
    """agent-proxy maps it to the thread's developer instructions."""
    assert build_argv(AgentConfig(agent="codex", append_system_prompt="Be brief.")) == [
        "agent-proxy", "--agent", "codex", "--append-system-prompt", "Be brief.",
    ]


def test_dash_c_is_a_config_override_under_codex():
    """Under claude `-c` is `--continue`; under codex it is `-c key=value`."""
    argv = build_argv(AgentConfig(agent="codex", extra_args=["-c", "model_reasoning_effort=low"]))
    assert argv[-2:] == ["-c", "model_reasoning_effort=low"]
    with pytest.raises(ValueError, match="session flag"):
        build_argv(AgentConfig(extra_args=["-c", "model_reasoning_effort=low"]))


@pytest.mark.parametrize(
    "flag", ["--session-id", "--resume", "-r", "--continue", "--fork-session", "--resume=abc"]
)
def test_codex_still_refuses_the_session_flags(flag):
    with pytest.raises(ValueError, match="session flag"):
        build_argv(AgentConfig(agent="codex", extra_args=[flag, "x"]))


@pytest.mark.parametrize("args", [["--agent", "codex"], ["--agent=codex"]])
def test_agent_in_extra_args_is_refused(args):
    """The session records the agent next to its id, so only `agent` may choose it."""
    with pytest.raises(ValueError, match="AgentConfig.agent"):
        build_argv(AgentConfig(extra_args=args))
