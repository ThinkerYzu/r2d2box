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

**Status:** Phase 3 of 5 — the server half is complete. An application mounts
the router and gets a WebSocket that two tabs can share, session REST for its
own furniture, and a URL for the front-end. What is missing is that front-end:
today's only client is a test that speaks the protocol by hand.

```python
from pathlib import Path

from fastapi import FastAPI
from r2d2box import AgentConfig, FileTranscriptStore, R2D2Box

box = R2D2Box(
    lambda topic, session: AgentConfig(append_system_prompt=f"about {topic}"),
    store=FileTranscriptStore(Path.home() / ".myapp/chat"),
)

app = FastAPI(lifespan=box.lifespan)
app.include_router(box.router, prefix="/chat")
```

That gives the application a WebSocket at `/chat/ws`, session endpoints under
`/chat/sessions`, and the front-end at `/chat/static`. A client attaches to a
`(topic, session)` pair and submits prompts; every client attached to the same
pair sees the same stream.

`box.host` is the `AgentHost` underneath, for a caller that also wants an agent
outside a request — a cron job, a CLI:

```python
session = await box.host.session("bug-1992198", "s1")
await session.attach(my_subscriber)     # anything with `async def send(message)`
await session.submit("why does it crash?")
```

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                             # 166 tests, ~0.9s; needs no `claude`
R2D2BOX_RUN_LIVE=1 .venv/bin/pytest -m live  # 4 real conversations; needs agent-proxy and claude
```

Nothing in the default suite needs `claude`, and each layer is tested without
the one below it. `tests/scripted_proxy.py` is a real subprocess replaying a
JSON-lines script, so the read-buffer limit and the shutdown path are the ones
production uses; `tests/fake_proxy.py` stands in for that subprocess so the
conversation layer can be driven one message at a time; and
`FakeHost` there overrides `AgentHost.start_proxy` so a whole host — and the
router above it — runs with no `agent-proxy` installed; and
`tests/asgi_client.py` drives the mounted app over ASGI in the test's own event
loop, so the routing and the `WebSocket` object are the production ones with no
server and no HTTP client dependency.

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
