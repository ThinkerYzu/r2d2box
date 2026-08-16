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

**Status:** Phase 2 of 5 — the server half works, without HTTP. `AgentHost`
runs conversations under a topic key, each with one agent-proxy process behind
it, resumes a conversation whose process has died, and evicts idle ones. What
is missing is a way for a browser to reach it: no router, no WebSocket, no
front-end.

```python
from pathlib import Path

from r2d2box import AgentConfig, AgentHost, FileTranscriptStore

host = AgentHost(
    lambda topic, session: AgentConfig(append_system_prompt=f"about {topic}"),
    store=FileTranscriptStore(Path.home() / ".myapp/chat"),
)

session = await host.session("bug-1992198", "s1")
await session.attach(my_subscriber)     # anything with `async def send(message)`
await session.submit("why does it crash?")
```

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                             # 132 tests, ~0.7s; needs no `claude`
R2D2BOX_RUN_LIVE=1 .venv/bin/pytest -m live  # 3 real conversations; needs agent-proxy and claude
```

Nothing in the default suite needs `claude`, and each layer is tested without
the one below it. `tests/scripted_proxy.py` is a real subprocess replaying a
JSON-lines script, so the read-buffer limit and the shutdown path are the ones
production uses; `tests/fake_proxy.py` stands in for that subprocess so the
conversation layer can be driven one message at a time; and
`AgentHost.start_proxy` is the override point that runs a whole host with no
`agent-proxy` installed.

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
