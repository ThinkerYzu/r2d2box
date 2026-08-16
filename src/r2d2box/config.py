"""How a host describes an agent, and the agent-proxy argv that description becomes.

`AgentConfig` is what the host's `agent_config(topic, session)` callback
returns, and `build_argv` is the only place in r2d2box that knows how its
fields map onto agent-proxy's command line.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PROXY_BIN = "agent-proxy"

# Flags by which the caller would pick the conversation. The session layer owns
# that choice, so `build_argv` refuses them in `extra_args` rather than letting
# one silently win — see `_reject_session_flags`.
_SESSION_FLAGS = frozenset(
    {"--session-id", "--resume", "-r", "--continue", "-c", "--fork-session"}
)
_SESSION_FLAG_PREFIXES = ("--session-id=", "--resume=")


@dataclass(frozen=True)
class AgentConfig:
    """One agent's spawn-time configuration: everything the host chooses, and nothing else.

    Every field is optional, and every one maps to an agent-proxy argument or to
    the subprocess itself. A default-constructed `AgentConfig` is valid and runs
    a bare `agent-proxy`.

    There is deliberately no `resume` field. The claude session id belongs to the
    session, which adds `--resume` itself; a host that writes one into
    `extra_args` gets a `ValueError` from `build_argv` instead of a conversation
    that is quietly the wrong one.
    """

    cwd: Path | None = None
    append_system_prompt: str | None = None
    allowed_tools: Sequence[str] = ()
    mcp_config: dict[str, Any] | None = None
    extra_args: Sequence[str] = ()
    proxy_bin: str | None = None


def build_argv(config: AgentConfig, resume: str | None = None) -> list[str]:
    """The command line that runs one agent-proxy for `config`, resuming `resume` if given.

    `resume` is the claude session id from a previous process's `ready`, and
    comes from the session rather than from `config`. Raises `ValueError` if
    `config.extra_args` carries a session-control flag.

    Nothing here duplicates what agent-proxy adds for itself:
    `--dangerously-skip-permissions` and the `AskUserQuestion` denial are its
    job (its API.md § Invocation), and passing them again would be a second
    place to keep in step with it.
    """
    _reject_session_flags(config.extra_args)

    argv = [config.proxy_bin or DEFAULT_PROXY_BIN]
    if config.append_system_prompt:
        argv += ["--append-system-prompt", config.append_system_prompt]
    if config.allowed_tools:
        argv += ["--allowedTools", *config.allowed_tools]
    if config.mcp_config:
        # Serialized here, from a dict, so a host cannot hand us malformed JSON
        # that only fails once the subprocess is already running.
        argv += ["--mcp-config", json.dumps(config.mcp_config)]

    # `--resume` goes ahead of `extra_args`, never after it.
    #
    # agent-proxy stops parsing session flags at a `--` and treats the rest as
    # the inner claude's positional arguments. A host whose extra_args ends in
    # `--` would therefore turn an appended `--resume <id>` into prompt text,
    # and silently start a new conversation instead of continuing one.
    if resume:
        argv += ["--resume", resume]
    argv += list(config.extra_args)
    return argv


def _reject_session_flags(extra_args: Sequence[str]) -> None:
    """Raise `ValueError` if `extra_args` tries to pick the conversation.

    agent-proxy resolves session flags left to right and lets the last one win,
    so a `--resume` in `extra_args` would override the session's own — and
    r2d2box would go on recording turns against an id the agent is not running.
    The failure is invisible at spawn and shows up later as a conversation that
    lost its history, which is worth refusing outright.
    """
    for arg in extra_args:
        if arg in _SESSION_FLAGS or arg.startswith(_SESSION_FLAG_PREFIXES):
            raise ValueError(
                f"AgentConfig.extra_args must not carry the session flag {arg!r}; "
                "r2d2box owns the claude session id and passes --resume itself"
            )
