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

**Status:** Phase 4 of 5 — both halves work. An application mounts the router
and gets a WebSocket two tabs can share, session REST for its own furniture,
and the chat box served beside them. `demo/` is a working host; what is left is
Phase 5's reference documentation.

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

The page mounts the box into an element of its own and gets the conversation
drawn into it:

```html
<link rel="stylesheet" href="/chat/static/r2d2box.css">
<div id="chat"></div>
<script src="/chat/static/vendor/marked.min.js"></script>
<script src="/chat/static/vendor/purify.min.js"></script>
<script src="/chat/static/r2d2box.js"></script>
<script>
  const box = R2D2Box.mount(document.getElementById('chat'), {
    endpoint: '/chat', topic: 'bug-1992198', session: 's1',
  });
  box.on('tool_result', (message) => {
    if (message.tool === 'mcp__myapp__save' && !message.is_error) refreshPanel();
  });
</script>
```

The two vendored scripts are optional and recommended: without them the box
shows assistant text verbatim rather than rendering it, because unsanitized
Markdown never reaches the DOM. Session pickers and close buttons are the
host's, built on the REST endpoints — see `demo/index.html`.

A conversation can start before anyone types. `opening_prompt` is called once
for a session that is new — never for one that is resuming — and its result
becomes that conversation's first turn, whether the session was created by your
code, by the REST endpoint, or by the chat box attaching with no session named:

```python
box = R2D2Box(
    agent_config,
    opening_prompt=lambda topic, session: f"Here is the bug:\n\n{summarize(topic)}",
    store=FileTranscriptStore(Path.home() / ".myapp/chat"),
)
```

A question typed while that turn is still starting queues behind it, so the
conversation always begins where you meant it to.

## Running the demo

```bash
.venv/bin/pip install 'uvicorn[standard]'
.venv/bin/python -m uvicorn demo.app:app --port 8790
# then open http://localhost:8790/
```

A whole host application in under 100 lines, and the fastest way to see two
tabs sharing one conversation.

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
.venv/bin/pytest                             # 181 tests, ~1s; needs no `claude`
node tests/js/run.js                         # the front-end's 36, on their own
R2D2BOX_RUN_LIVE=1 .venv/bin/pytest -m live  # 5 real conversations; needs agent-proxy and claude
```

Nothing in the default suite needs `claude`, and each layer is tested without
the one below it. `tests/scripted_proxy.py` is a real subprocess replaying a
JSON-lines script, so the read-buffer limit and the shutdown path are the ones
production uses; `tests/fake_proxy.py` stands in for that subprocess so the
conversation layer can be driven one message at a time; `FakeHost` there
overrides `AgentHost.start_proxy` so a whole host — and the router above it —
runs with no `agent-proxy` installed; `tests/asgi_client.py` drives the mounted
app over ASGI in the test's own event loop, so the routing and the `WebSocket`
object are the production ones with no server and no HTTP client dependency;
and `tests/js/minidom.js` is the same idea one layer higher, a DOM small enough
to run the shipped `r2d2box.js` in under plain `node` — no browser, no jsdom,
no package manager. `tests/test_chat_box.py` runs those from pytest and skips
if `node` is missing.

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
