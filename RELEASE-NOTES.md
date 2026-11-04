# r2d2box v0.3.0

This release adds a standalone session server, structured agent questions and
native permission approval forms. It also makes r2d2box installable from PyPI
with pip or uv, including its agent-proxy dependency.

## Install and run

```bash
uvx r2d2box@0.3.0
# Or install a persistent command:
uv tool install r2d2box==0.3.0
# Embed the library in a project:
uv add r2d2box==0.3.0
# pip is also supported:
python -m pip install r2d2box==0.3.0
```

Open http://127.0.0.1:8790/ for the standalone server. Python 3.11 or newer is
required. `thinkeryzu-agent-proxy>=0.3.1` is installed automatically. Install
and sign in to Claude Code (2.1.316 or newer) or Codex separately.

## Changes since v0.2.0

- The `r2d2box` command and `python -m r2d2box` serve a packaged session page,
  persistent transcripts, REST and WebSocket endpoints. Options configure the
  bind address, agent, working directory, transcript store and proxy arguments.
- Questions are always enabled. Choice and text forms share answer state across
  tabs and reconnects. Remove any former `AgentConfig(user_input=...)` argument.
- Permission bypass remains enabled by default. Set
  `AgentConfig(bypass_permissions=False)` or use `--no-bypass-permissions` for
  native approvals. Forms display operation details and choices without
  preselecting a decision. Dismissing an approval denies it.
- Question and approval forms scroll in the conversation, with wrapping for
  long content. Chat links use the panel's light blue user accent.
- Proxy spawning prefers the dependency in the current Python environment,
  allowing uv tool launchers to work without environment activation.
  Explicit `proxy_bin` overrides still take precedence.
- Development and CI use a committed uv lockfile. The release workflow validates
  wheel, source and uv tool installations before publishing the same artifacts
  to PyPI and GitHub using Trusted Publishing.

## Validation

Release checks cover the default Python and front-end suites, distribution
metadata, packaged page/assets, clean wheel/source installs and uv tool
installation. Real agent integration remains opt-in through `R2D2BOX_RUN_LIVE=1`.
