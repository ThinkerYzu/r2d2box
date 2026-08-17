# r2d2box

A chat box for AI agents, as a library: one server-side agent host and one
front-end chat panel, so an app that wants to embed a live Claude agent doesn't
have to build either.

The host app supplies what is genuinely its own — the MCP servers, the system
prompt, the tool allowlist — and r2d2box supplies the rest: the `agent-proxy`
subprocess and its turn correlation, the WebSocket that carries the message
stream, the transcript store, and the JavaScript that renders messages, tool
calls, and the prompt input into a `div` the page provides.

**Status:** complete. An application mounts the router and gets a WebSocket two
tabs can share, session REST for its own furniture, and the chat box served
beside them. `demo/` is a working host; **[API.md](API.md) is the reference** —
the Python API, the JavaScript API, the WebSocket messages, and what migrating
an existing app involves.

> **You need `agent-proxy`.** It is the only way r2d2box reaches an agent, and
> it is a separate tool that is **not yet published** — so this repository does
> not run on its own today. See [Requirements](#requirements).

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

The server side can also be told when a conversation is working, which nothing
else there reports — the turn's messages go to the browsers and the transcript.
`on_activity` fires once when a session starts working and once when it stops,
and `session.active` is the same fact read rather than pushed:

```python
box = R2D2Box(
    agent_config,
    on_activity=lambda topic, session, active: mark_working(topic, session, active),
)
```

Working means a submit in flight, a turn running, or a background command still
going — so it goes up before there is a process, and stays up after a turn that
left work behind.

## Requirements

| Needs | Why | Where from |
|---|---|---|
| Python ≥ 3.11, `fastapi` ≥ 0.110 | the router you mount | pip, as this package's dependency |
| `agent-proxy` on `$PATH` | **the only way r2d2box reaches an agent** | a separate tool, not yet published |
| `claude` on `$PATH` | what agent-proxy drives | Claude Code |
| `uvicorn[standard]` | a plain `uvicorn` serves no WebSocket | pip, for the demo or your own server |

**About agent-proxy.** r2d2box does not spawn `claude` itself, does not speak
`stream-json`, and adds nothing to the turn protocol. It sits on agent-proxy,
which drives an interactive `claude` over a pty and states turn boundaries
explicitly — every message carries a turn id, a kind, and the outstanding
user/unowned/background counts. That tool is not open source yet, so **this
repository does not run end to end on its own today.** What does work without
it is the whole test suite: 196 tests pass with neither binary installed,
because every layer is tested against a stand-in for the one below it.

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
.venv/bin/pytest                             # 196 tests, ~1s; needs no `claude`
node tests/js/run.js                         # the front-end's 39, on their own
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

**[API.md](API.md)** is the one to read if you are embedding r2d2box: the
Python API, the JavaScript API, the WebSocket messages, what happens when
things go wrong, and what migrating an existing app involves.

Beyond it, the reasoning lives in the module docstrings rather than in a
separate design document. Each one opens with what its module is for and then
names the two or three things about it that are less obvious than they look —
`session.py` on why `attach` sends under the lock, `proxy.py` on the 16 MiB
read buffer, `router.py` on why a slow client is dropped rather than buffered.
Nearly all of them exist because an earlier implementation got it wrong.

## License

MIT — see [LICENSE](LICENSE).

Two third-party files are vendored under `src/r2d2box/static/vendor/`, each
keeping its own license banner and terms:

| File | Project | License |
|---|---|---|
| `marked.min.js` | [marked](https://github.com/markedjs/marked) 15.0.12 | MIT |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) 3.2.4 | Apache-2.0 or MPL-2.0 |

They are vendored rather than fetched so the front-end needs no build step and
makes no runtime request to a CDN.
