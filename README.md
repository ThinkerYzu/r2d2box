# r2d2box

A chat box for AI agents, as a library: one server-side agent host and one
front-end chat panel, so an app that wants to embed a live Claude agent doesn't
have to build either.

The host app supplies what is genuinely its own — the MCP servers, the system
prompt, the tool allowlist — and r2d2box supplies the rest: the
[agent-proxy](../agent-proxy/) subprocess and its turn correlation, the
WebSocket that carries the message stream, the transcript store, and the
JavaScript that renders messages, tool calls, and the prompt input into a `div`
the page provides.

**Status:** Phase 1 of 5 — the process layer. `AgentConfig` becomes an
agent-proxy command line, and one agent-proxy process is spawned, read and shut
down correctly. Nothing above it exists yet: no sessions, no store, no router,
no front-end.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                             # the suite; needs no `claude`
R2D2BOX_RUN_LIVE=1 .venv/bin/pytest -m live  # one real turn; needs agent-proxy and claude
```

The suite runs a real subprocess — `tests/scripted_proxy.py`, replaying a
JSON-lines script — rather than mocking the pipe, so the read-buffer limit and
the shutdown path are the ones production uses.

## Documentation

Requirements, architecture, and the plan live in the docforge, not here:

| Document | What it covers |
|---|---|
| [SPEC.md](../../proj_docs/r2d2box/SPEC.md) | problem, requirements, constraints, decisions |
| [DESIGN.md](../../proj_docs/r2d2box/DESIGN.md) | architecture and the host-facing API |
| [IMPLEMENTATION-GUIDE.md](../../proj_docs/r2d2box/IMPLEMENTATION-GUIDE.md) | file layout, build, phase plan |
| [HANDOFF.md](../../proj_docs/r2d2box/HANDOFF.md) | current status and next actions |

Reference material for users of the library will live in this repo once there
is something to document.
