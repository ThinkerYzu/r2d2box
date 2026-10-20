# r2d2box v0.2.0

This release adds Codex as a second agent and a Stop button for queued or
running turns. It also fixes idle eviction and composer behavior when agents
leave background work running.

## Compatibility and installation

- Install **agent-proxy v0.2.0 or newer** separately. It supplies Codex,
  cancellation, and the default `--autocompact 1m` launch setting that prevents
  Claude's optional context-window dialog from blocking queued prompts.
- With agent-proxy v0.2.0's default launch settings, Claude Code **2.1.221 or
  newer** is required. Explicit autocompact overrides are preserved.
- Codex requires the Codex CLI signed in with ChatGPT. Configure it with
  `AgentConfig(agent="codex")`. `allowed_tools` and `mcp_config` are refused
  for Codex; see [API.md](https://github.com/ThinkerYzu/r2d2box/blob/v0.2.0/API.md#running-codex) for accepted configuration.
- The session attribute `claude_session_id` is renamed to `agent_session_id`;
  `Session.agent` identifies the backend that owns it. A backend change starts
  a fresh agent conversation while preserving the visible transcript.
- Python 3.11 or newer remains required. The browser assets ship in the wheel.

```bash
pip install 'git+https://github.com/ThinkerYzu/agent-proxy@v0.2.0'
pip install 'git+https://github.com/ThinkerYzu/r2d2box@v0.2.0'
```

## New capabilities

- `AgentConfig.agent` selects Claude or Codex behind the same conversation
  stream, router, transcript store and chat box.
- `Session.cancel(turn_id)` and the WebSocket `cancel` command withdraw a
  queued turn or request interruption of a running user turn. `turn_cancel`
  broadcasts the stopping state to every attached client; `turn_end` reports
  the final outcome, including `cancelled`.
- Send and Stop share one button slot. Stop appears while a user turn can be
  cancelled; otherwise Send shows the composer's current availability.
- Background tasks display their commands and output in collapsible blocks,
  open while running and folded when finished. Codex tool headers summarize
  file changes, MCP calls and searches.

## Fixes

- Idle eviction spares sessions with outstanding background commands,
  subagents or monitors under the configured caps. Monitors have no deadline
  by default.
- A persistent monitor no longer disables the composer or causes indefinite
  status polling. Task kinds are included in attach and status snapshots.
- Queued turns can be stopped before they start, and cancellation state is
  shared consistently across tabs.

## Validation

Validated locally on 2026-10-20 against agent-proxy v0.2.0:

- 260 default tests passed, including 58 front-end tests.
- All 10 live tests passed with Claude Code 2.1.304 and Codex CLI 0.163.0.

The release workflow checks the tag against package metadata, runs both test
suites, builds the wheel and source distribution, and verifies all four browser
assets are present before publishing.
