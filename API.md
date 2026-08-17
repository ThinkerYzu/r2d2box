# r2d2box API

Reference for an application embedding r2d2box. It covers the Python API, the
JavaScript API, the WebSocket messages between them, and what an app with its
own agent chat has to replace to migrate onto this one.

Where something here reads as arbitrary, the module docstring for it carries
the reasoning — most of this library's rules exist because an earlier
implementation got them wrong.

**Contents:** [Install](#install) · [Quick start](#quick-start) ·
[Python API](#python-api) · [JavaScript API](#javascript-api) ·
[WebSocket protocol](#websocket-protocol) · [Session REST](#session-rest) ·
[Failure behavior](#failure-behavior) · [Migrating an existing app](#migrating-an-existing-app)

---

## Install

```bash
pip install -e /path/to/r2d2box
```

Two things have to be on the machine that r2d2box does not install:

| Needs | Why | Where from |
|---|---|---|
| `agent-proxy` on `$PATH` | the only way r2d2box reaches an agent | a separate tool, not yet published |
| `claude` on `$PATH` | what agent-proxy drives | Claude Code |

**agent-proxy is not open source yet**, so r2d2box does not run end to end
without it. Everything below describes the API as built and tested; the test
suite itself needs neither binary.

The browser half ships **inside** the Python package and is served by the router
you mount. There is no build step, no copy step, and nothing is fetched from a
CDN at runtime — `marked` and DOMPurify are vendored beside the box.

A WebSocket-capable server is also required: a plain `uvicorn` install serves no
WebSocket. Use `pip install 'uvicorn[standard]'`.

## Quick start

The server side is one object and one `include_router`:

```python
from pathlib import Path

from fastapi import FastAPI
from r2d2box import AgentConfig, FileTranscriptStore, R2D2Box

def agent_config(topic: str, session: str) -> AgentConfig:
    return AgentConfig(append_system_prompt=f"You are helping with {topic}.")

box = R2D2Box(agent_config, store=FileTranscriptStore(Path.home() / ".myapp/chat"))

app = FastAPI(lifespan=box.lifespan)
app.include_router(box.router, prefix="/chat")
```

That prefix now carries three things:

| Path | What |
|---|---|
| `/chat/ws` | the WebSocket the box talks to |
| `/chat/sessions/…` | session REST, for your own furniture |
| `/chat/static/…` | the box, its stylesheet, and the two vendored scripts |

The page side is four files and one `mount`:

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
</script>
```

`demo/app.py` and `demo/index.html` are a complete host application built this
way, in under 100 lines. Run it with:

```bash
python -m uvicorn demo.app:app --port 8790
```

### Topics and sessions

A **topic** is your key string for the thing being talked about. Under it sit
**sessions**, each one conversation with one agent-proxy process behind it. You
decide what a topic means; r2d2box only uses it to scope session names and
transcripts.

| An app that | A topic is | Sessions under it |
|---|---|---|
| assists on one bug at a time | one bug (`bug-1992198`) | the conversations held about that bug |
| sits beside a document viewer | one project | the entries in its session picker |

Every client attached to the same `(topic, session)` sees the same stream. Two
browser tabs on one conversation are two views of it, not two conversations —
and a turn started in either blocks the composer in both.

---

## Python API

### `R2D2Box`

```python
R2D2Box(
    agent_config,           # or host=<a prebuilt AgentHost>
    *,
    build_prompt=None,
    opening_prompt=None,
    on_activity=None,
    store=None,
    idle_timeout_s=1200,
    pending_evict_cap_s=14400,
    sweep_interval_s=60,
    static_dir=None,
    client_queue_limit=512,
)
```

| Parameter | Default | Purpose |
|---|---|---|
| `agent_config` | required | callback returning the `AgentConfig` for one session |
| `host` | — | a prebuilt `AgentHost` instead of `agent_config`; exactly one of the two |
| `build_prompt` | identity | callback turning typed text plus context into the real prompt |
| `opening_prompt` | none | callback giving a *new* conversation its first turn |
| `on_activity` | none | callback told when a session starts working and when it stops |
| `store` | `MemoryTranscriptStore()` | where finished turns go |
| `idle_timeout_s` | 1200 | stop the agent behind a session idle this long |
| `pending_evict_cap_s` | 14400 | stop it even with a turn in flight past this |
| `sweep_interval_s` | 60 | how often the idle sweep runs |
| `static_dir` | the packaged one | serve a modified front-end instead |
| `client_queue_limit` | 512 | messages one client may fall behind by before it is dropped |

Four members matter to a host:

| Member | Does |
|---|---|
| `box.router` | what you `include_router`, under any prefix or none |
| `box.host` | the `AgentHost`, for driving agents outside a request |
| `box.lifespan` | `FastAPI(lifespan=box.lifespan)` — sweeper up, host closed |
| `await box.aclose()` | stop every session and every agent-proxy; safe twice |

Wire the lifespan, or call `aclose` from whatever shutdown hook you already
have. Nothing else terminates the agent-proxy processes.

### The three callbacks that shape a turn

These three decide what the agent is and what it is asked. The fourth,
`on_activity`, runs the other way and is [below](#knowing-when-a-session-is-working).

All may be sync or async — return a coroutine and it is awaited, so a callback
that queries a database does not have to block the event loop.

**`agent_config(topic, session) -> AgentConfig`** is called at every spawn, not
once at mount. That includes the respawn after an idle eviction or a crash, so
a system prompt built from live data is never stale:

```python
async def agent_config(topic: str, session: str) -> AgentConfig:
    bug_id = int(topic.removeprefix("bug-"))
    return AgentConfig(
        cwd=Path("/home/dev/project"),
        append_system_prompt=await summarize_bug(bug_id),
        allowed_tools=["mcp__notes__worklog_append", "mcp__notes__item_metadata"],
        mcp_config={"mcpServers": {"notes": {
            "command": sys.executable,
            "args": ["-m", "myapp.mcp_server", "--bug-id", str(bug_id)],
        }}},
    )
```

**`build_prompt(topic, session, text, context) -> str`** turns what the person
typed into what the agent is asked. `context` is the arbitrary JSON the box
attached with `setContext`:

```python
def build_prompt(topic, session, text, context):
    if not context:
        return text
    return f"The reader has selected this from {context['file']}:\n\n{context['selectedText']}\n\n{text}"
```

The transcript records `text`, not the assembled prompt — what is shown is what
was asked. A hook that raises fails the submit rather than sending the bare
text, because half a prompt gets a confident answer to a question nobody asked.
Returning anything but a non-empty string is a `ValueError`.

**`opening_prompt(topic, session) -> str | None`** is called once for a session
that is new, and never for one that is resuming. Its result becomes that
conversation's first turn:

```python
async def opening_prompt(topic: str, session: str) -> str | None:
    if not topic.startswith("bug-"):
        return None                      # this conversation starts empty
    return f"Here is the bug you are helping with:\n\n{await summarize(topic)}"
```

Three things about it are worth knowing:

- It covers every path that creates a session — your own code, the REST
  endpoint, and the chat box attaching with no session named. That last one is
  the case you cannot get in front of yourself, because you never learn the
  session exists.
- It runs in the background, so attaching does not wait out a process spawn. A
  question typed the instant the panel appears queues *behind* the opening turn
  rather than racing ahead of it.
- It does not go through `build_prompt`. The opening is your own words, not a
  person's question.

### Knowing when a session is working

Nothing else on the server side tells you a conversation is busy. The turn's
messages go to the attached browsers and to the transcript; if your application
wants to show a busy light on a list of conversations, throttle something while
an agent is running, or write "in progress" to a row of its own, it needs the
same fact.

There are two ways to ask, and they answer the same question:

**`on_activity(topic, session, active)`** is called once each time a session
crosses between working and idle — never twice for the same state, and never at
all for a session that stays quiet. It may be sync or async.

```python
async def on_activity(topic: str, session: str, active: bool) -> None:
    await db.execute(
        "UPDATE conversations SET working = ? WHERE topic = ? AND session = ?",
        (active, topic, session),
    )

box = R2D2Box(agent_config, on_activity=on_activity)
```

**`session.active`** is the same value read rather than pushed, for an
application that would rather ask at a moment of its own choosing:

```python
busy = [s.name for s in box.host.live_sessions("bug-1992198") if s.active]
```

A session is **working** when any of three things is true:

| Working because | From | Until |
|---|---|---|
| a submit is in flight | the moment `submit` is called | agent-proxy acknowledges it, or it fails |
| a turn has started and not ended | its `ack` | its `turn_end` |
| a background command is running | `task_start` | `task_end` |

The first is why the signal goes up before there is a turn to point at:
assembling the prompt and spawning a process are the slow part of a submit, and
a signal that waited for the ack would show nothing during the seconds a reader
is most likely to be watching. The third is why this is broader than
`turn_active` — a `run_in_background` command outliving its turn frees the
composer but is still work.

A live agent-proxy process is **not** working, and neither is an attached
browser. A conversation nobody has spoken to in an hour is idle however many
tabs are open on it.

Three things about the callback are worth knowing before you write one:

- **It is level-triggered, not a log of edges.** What you are told is true at
  the moment you are told it. A session that goes busy and idle again while
  your callback is still running collapses to nothing rather than arriving
  backwards.
- **A callback that raises costs its own notification and nothing else.** The
  failure is logged and the conversation carries on — your status hook cannot
  end a turn.
- **It is awaited, so a slow one delays that session's own message stream** —
  the same way a slow `build_prompt` delays its submit. It does not hold the
  session lock, so attaching, `status` and every other session are unaffected.
- **Do not drive the session from inside it.** Reading state is fine; calling
  `submit`, `close` or `stop_process` waits on a lock the notification is
  holding.

The signal is the server's alone. Nothing about it goes on the wire, and a
browser learns the same thing from `turn_active` and `task_ids`.

### `AgentConfig`

Every field is optional, and a default-constructed `AgentConfig` runs a bare
`agent-proxy`.

| Field | Becomes |
|---|---|
| `cwd` | the subprocess working directory |
| `append_system_prompt` | `--append-system-prompt` |
| `allowed_tools` | `--allowedTools` |
| `mcp_config` | `--mcp-config`, with the dict serialized to JSON |
| `extra_args` | appended verbatim |
| `proxy_bin` | the binary to run; defaults to `agent-proxy` on `$PATH` |

There is no `resume` field. The session owns the claude session id and adds
`--resume` itself, ahead of `extra_args`. Putting a session flag in
`extra_args` — `--resume`, `--session-id`, `-r`, `--continue`, `-c`,
`--fork-session`, or their `=` forms — raises `ValueError` at spawn rather than
silently running the wrong conversation.

Do not pass `--dangerously-skip-permissions` or the `AskUserQuestion` denial:
agent-proxy adds both itself.

### Transcript stores

`store=` takes anything implementing `TranscriptStore`. Two ship:

| Store | Keeps | Use for |
|---|---|---|
| `MemoryTranscriptStore()` | a dict, lost on restart | tests, and a host that wants nothing on disk |
| `FileTranscriptStore(root)` | one JSON-lines file per session, one directory per topic | everything else |

`FileTranscriptStore` slugs both keys into the path, so a topic may be any
string — `Bug 1992198: crash on resize` included — without deciding where the
file lands or how far up the tree it goes. Two processes sharing a root append
to the same files without coordinating.

The interface is four async methods:

```python
class TranscriptStore(ABC):
    async def append_turn(self, topic: str, session: str, turn: Turn) -> None: ...
    async def read_turns(self, topic: str, session: str) -> list[Turn]: ...
    async def list_sessions(self, topic: str) -> list[SessionInfo]: ...
    async def clear(self, topic: str, session: str) -> None: ...
```

A store holds *completed* turns; the turn in flight lives in the session until
its `turn_end`. Reading a session that was never written is an empty list, not
an error. A `Turn` carries `id`, `kind`, `user` (the text typed, or `None` for a
turn nobody submitted), `by_host` (the text is your application's own words —
an opening prompt — rather than a person's question), `events`, `started_at`,
`ended_at` and `outcome`; its `to_dict()` is exactly what the `attached` message
sends.

`list_sessions` returns a `SessionInfo` per session: `session`, `last_active`,
`turns` and `preview`, as `GET /sessions/{topic}` passes them on. The last two
have defaults, so a store of your own may return the first two alone and list
every conversation as empty and unlabelled. `preview_of(turns)` is the shipped
stores' own helper, exported so yours can produce the same string.

If your session picker needs more than that — titles, tags — keep that metadata
yourself alongside the store rather than subclassing one; r2d2box does not model
it.

### Below the router

`box.host` is an `AgentHost`, usable with no HTTP at all — a cron job, a CLI, a
test:

```python
session = await box.host.session("bug-1992198", "s1")
await session.attach(my_subscriber)      # anything with `async def send(message)`
turn_id = await session.submit("why does it crash?")
```

`AgentHost`:

| Member | Does |
|---|---|
| `await session(topic, name)` | the live session, created if this is the first ask |
| `await create_session(topic)` | a new session with a name nothing else is using |
| `await list_sessions(topic)` | `SessionInfo` for every session, stored or live, newest first |
| `await close_session(topic, name, clear=False)` | stop its agent and drop it; `clear` discards the transcript too |
| `live_sessions(topic=None)` | the sessions being held, process or not |
| `await evict_idle()` | run one idle sweep by hand; returns how many were evicted |
| `start_sweeper(interval_s)` / `await stop_sweeper()` | the sweep loop |
| `await close()` | stop everything; also `async with` |
| `.store` | the store it was given |

`Session`:

| Member | Does |
|---|---|
| `await submit(text, context=None)` | assemble the prompt, spawn if needed, return the turn id |
| `await attach(subscriber)` / `await detach(subscriber)` | subscribe and unsubscribe |
| `await snapshot()` / `await status()` | the `attached` and `status` payloads |
| `await report_error(text)` | broadcast a session-wide error of your own |
| `active` | whether this session has any work in flight |
| `turn_active`, `pending_turns`, `open_turns`, `task_ids`, `process_alive`, `subscriber_count` | live state, derived rather than latched |
| `await stop_process()` / `await close()` / `await clear()` | evict the agent / end the session / discard the transcript |

Two properties of `submit` shape everything above it. It returns when
agent-proxy **acknowledges** the prompt — before the turn has been typed and
long before it has run — so a turn id in hand means the turn will happen, not
that it has. And the turn's messages arrive on the subscriber stream like
everything else, never as a return value.

A `Subscriber` is anything with `async def send(message)`. Its `send` is awaited
**under the session lock**, so an implementation must queue rather than wait on
its peer: a subscriber that blocks holds up the read pump for every other client
on that session. Raising from `send` unsubscribes you.

`submit` raises `SubmitRejected` if agent-proxy refused the prompt,
`ConnectionError` if the process died or never answered within 30s,
`ProxyStartError` if no process could be started, and whatever `build_prompt`
raised, unchanged.

### Idle eviction

A session idle past `idle_timeout_s` has its agent-proxy stopped. The session
itself survives — it keeps the claude session id — so the next `submit` spawns
a replacement with `--resume` and continues the conversation. Nobody attached
sees anything but a `process_exited`.

A session with a turn still running is spared until `pending_evict_cap_s`,
because a turn keeps running with nobody listening and nothing refreshes its
idle clock while it does. The cap stops that exemption becoming permanent.

---

## JavaScript API

### Mounting

```js
const box = R2D2Box.mount(document.getElementById('chat'), {
  endpoint: '/chat',
  topic: 'bug-1992198',
  session: 's1',
});
```

The element becomes the box's root; anything already in it is cleared.

| Option | Default | Purpose |
|---|---|---|
| `endpoint` | `''` | the prefix the router was mounted under; the box appends `/ws` |
| `topic` | required | the conversation's key |
| `session` | none | leave it out to be given a new one, named in the `attached` event |
| `attach` | `true` | `false` builds the box without opening a socket |
| `describeContext` | a built-in | turns a context object into the badge's one-line label |
| `marked`, `DOMPurify`, `WebSocket` | the page's globals | overrides, for a test or an unusual host |

An absolute `ws://` or `wss://` `endpoint` is used as given, which is what a
cross-origin host passes. Otherwise the scheme and host come from the page.

### Methods

| Call | Does |
|---|---|
| `box.setContext(value)` | attach arbitrary JSON to the next submit, and show a badge for it |
| `box.on(type, handler)` / `box.off(type, handler)` | subscribe to a message type |
| `box.attach(topic, session)` | switch conversations in place; omit `session` for a new one |
| `box.destroy()` | close the socket, stop the timers, empty the element |

`setContext` is for the text a reader has selected in a document, and anything
else the next question needs as background. Whatever you pass reaches
`build_prompt` as its fourth argument, and is cleared once a submit carries
it:

```js
box.setContext({ file: 'DESIGN.md', startLine: 12, selectedText: '…' });
```

`destroy` touches only the browser. A turn in flight runs to completion
server-side and is waiting in the transcript for whoever attaches next.

### Events

`on` accepts any server→client message type, plus `connected` and
`disconnected`. Handlers get the message exactly as delivered — there is nothing
to unwrap:

```js
box.on('tool_result', (message) => {
  if (message.tool === 'mcp__notes__worklog_append' && !message.is_error) {
    refreshWorklog();                        // the host's own side of the tool
  }
});

box.on('attached', (message) => {
  history.replaceState(null, '', `?session=${message.session}`);
});
```

A handler that throws is logged to the console; the rest still run, and the box
keeps drawing.

### Theming

Every class the box emits is prefixed `r2d2-`, and every color, font and gap is
a custom property on `.r2d2-box`. Retheme by setting them on your own mount
element:

```css
#chat {
  --r2d2-bg: #ffffff;
  --r2d2-fg: #222222;
  --r2d2-surface: #f3f3f3;
  --r2d2-user-bg: #e8f0fe;
  --r2d2-font-size: 14px;
}
```

All twenty-two are declared in one block at the top of `static/r2d2box.css`:
six neutrals (`--r2d2-bg`, `--r2d2-fg`, `--r2d2-muted`, `--r2d2-border`,
`--r2d2-surface`, `--r2d2-sunken`), eleven accents for the user, agent,
background turn, thinking, tool, error and button states, and five for
typography and spacing (`--r2d2-font`, `--r2d2-mono`, `--r2d2-font-size`,
`--r2d2-gap`, `--r2d2-radius`).

There is no shadow root, so a page rule can reach inside the box. That is
deliberate: it is also what lets a DOM-driven UI test see the panel's internals.
The structure those tests can rely on:

| Selector | Is |
|---|---|
| `.r2d2-messages` | the scrolling list |
| `.r2d2-message[data-turn="…"]` | one turn's block; `.r2d2-message-user` for the question |
| `.r2d2-content` | the rendered prose |
| `.r2d2-tool` | one tool call, with `-running`, `-ok` or `-error` |
| `.r2d2-thinking` | a collapsed thinking block |
| `.r2d2-fold` | the container older blocks fold into |
| `.r2d2-status` | the working indicator at the foot of the list |
| `.r2d2-input`, `.r2d2-send` | the composer |

### Rendering, and what the box does not draw

Assistant text goes through `marked` then DOMPurify before it reaches
`innerHTML`. If either vendored script is missing the box shows the text
verbatim instead — unsanitized Markdown never reaches the DOM. That is the only
reason the two `<script>` tags are recommended rather than required.

The box draws the message list, the tool and thinking blocks, the status
indicator and the prompt input. It does **not** draw session pickers, close
buttons, titles or panel headers. Those differ too much between applications to
be worth standardizing, and each is one REST call plus one `box.attach` —
`demo/index.html` builds all of them in about forty lines.

---

## WebSocket protocol

Only relevant if you are writing your own renderer. The shipped box speaks all
of this already.

One connection per box, at `{prefix}/ws`, attached to at most one session at a
time.

### Client → server

| Type | Payload | Answered by |
|---|---|---|
| `attach` | `topic`, optional `session` | `attached`. With no `session`, one is created and named in the answer |
| `submit` | `text`, optional `context` | nothing directly — the turn arrives as broadcasts |
| `status_query` | — | `status` |
| `detach` | — | `detached` |

A second `attach` switches sessions in place. Anything else — a frame that is
not a JSON object, an unknown type, a `submit` before any `attach` — comes back
as a connection-scoped `error` rather than being dropped, because a silent drop
reads as a hung server from the browser.

### Server → client

Every message carries `topic` and `session`. Messages sent to every attached
client also carry `seq`; messages aimed at one connection carry
`scope: "connection"` and no `seq`.

| Type | Source | Meaning |
|---|---|---|
| `attached` | r2d2box | the transcript plus current state, in one message |
| `status` | r2d2box | turn in flight, outstanding task ids, process alive |
| `detached` | r2d2box | the `detach` is done |
| `turn_prompt` | r2d2box | what was asked, and the turn id answering it |
| `text`, `thinking`, `tool_use`, `tool_result` | agent-proxy | turn content |
| `turn_start`, `turn_end` | agent-proxy | turn boundaries, with `turn.kind` |
| `task_start`, `task_end` | agent-proxy | background command lifecycle |
| `error` | either | a session failure, or — with `scope: "connection"` — this client's command refused |
| `process_exited` | r2d2box | the agent-proxy behind this session is gone |
| `session_closed` | r2d2box | this conversation is over; the last message a session sends |

Forwarded messages are agent-proxy's own, unchanged, with an envelope added —
see agent-proxy's own `API.md § Message types` for their fields. Three of its
fields cannot survive as they are. `ref` is r2d2box's internal correlation
token and is dropped. The other two are numbered per process, and a session
outlives its process, so both are renumbered for the conversation and the
proxy's own value is kept beside the new one:

| agent-proxy's field | becomes | its own value is kept as |
|---|---|---|
| `seq` | the session's broadcast number | `proxy_seq` |
| `turn.id` | the session's turn id | `proxy_turn_id` |

So a turn id is yours to group by: it identifies one turn for the whole life of
the conversation, and is never handed out again after that turn ends — not
after an idle eviction, an agent that died, or a server restart, all of which
put a fresh agent-proxy behind a session that is numbering from `t-1` again.
Use `proxy_turn_id` only to match a turn against the agent's own log lines.

`ack` and `ready` are not forwarded at all.

An `attached` message looks like this:

```json
{"type": "attached", "topic": "bug-1992198", "session": "s1", "seq": 41,
 "turns": [{"id": "t-3", "kind": "user", "user": "why does it crash?",
            "events": [{"type": "turn_start", "…": "…"}],
            "started_at": 1755300000.0, "ended_at": 1755300012.0, "outcome": "ok"}],
 "turn_active": true, "task_ids": ["bash_3"], "process_alive": true,
 "outstanding": {"user": 1, "unowned": 0, "background": 1}}
```

### Four rules a renderer has to get right

**A message with no `seq` is about this connection, not the conversation.** It
carries `scope: "connection"` and is how a refused command comes back. Show it
and forget it — recording it would put one tab's complaint into a transcript
every tab shares.

**`seq` counts broadcasts only, and is gapless.** `attached` and `status` carry
the last broadcast number rather than consuming one, so the next message after
an `attached` with `seq: 41` is `42`. A real gap means a lost message — and so
does a `status` whose `seq` is ahead of what you have read. Both are repaired by
re-attaching, not by patching: the `attached` that answers replaces the screen,
which is correct only because the transcript is authoritative. Do not merge it.
`seq` restarts at 1 if a `Session` object is rebuilt for the same
`(topic, session)`, so every `attached` is also a reset point.

**The composer's disabled state belongs to the session, not the tab.**
`turn_active` and `task_ids` arrive from `attached` and `status`; replace your
own with them, never accumulate. A turn another tab started disables this tab's
input, and a background task that finished while the socket was down cannot
leave it stuck.

**The question is a broadcast too.** `turn_prompt` carries what was asked to
every attached client, so draw nothing locally when you submit. A "helpful"
local render doubles every question the moment the broadcast arrives.

### One turn, across two tabs

<svg viewBox="0 0 800 400" style="max-width:100%;height:auto" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Tab A submits; the session broadcasts turn_prompt, turn_start, text and turn_end to both tab A and tab B; the finished turn is appended to the store.">
<defs>
<marker id="ap-tip" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
<path d="M 0 0 L 10 5 L 0 10 z" fill="#334155"/>
</marker>
</defs>
<rect x="0" y="0" width="800" height="400" fill="#ffffff"/>
<text x="60" y="28" font-family="sans-serif" font-size="12" font-weight="bold" fill="#334155" text-anchor="middle">tab A</text>
<text x="300" y="28" font-family="sans-serif" font-size="12" font-weight="bold" fill="#334155" text-anchor="middle">session</text>
<text x="540" y="28" font-family="sans-serif" font-size="12" font-weight="bold" fill="#334155" text-anchor="middle">agent-proxy</text>
<text x="720" y="28" font-family="sans-serif" font-size="12" font-weight="bold" fill="#334155" text-anchor="middle">tab B</text>
<line x1="60" y1="38" x2="60" y2="380" stroke="#94a3b8" stroke-width="1.5"/>
<line x1="300" y1="38" x2="300" y2="380" stroke="#94a3b8" stroke-width="1.5"/>
<line x1="540" y1="38" x2="540" y2="380" stroke="#94a3b8" stroke-width="1.5"/>
<line x1="720" y1="38" x2="720" y2="380" stroke="#94a3b8" stroke-width="1.5"/>
<line x1="60" y1="62" x2="296" y2="62" stroke="#2563eb" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<text x="178" y="56" font-family="sans-serif" font-size="11" fill="#1e3a8a" text-anchor="middle">submit "why does it crash?"</text>
<line x1="300" y1="94" x2="536" y2="94" stroke="#16a34a" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<text x="418" y="88" font-family="sans-serif" font-size="11" fill="#14532d" text-anchor="middle">the assembled prompt, with a ref</text>
<line x1="540" y1="126" x2="304" y2="126" stroke="#16a34a" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<text x="422" y="120" font-family="sans-serif" font-size="11" fill="#14532d" text-anchor="middle">ack — that ref is turn t-4</text>
<text x="300" y="152" font-family="sans-serif" font-size="10" fill="#64748b" text-anchor="middle">ack is not forwarded; the ref is internal</text>
<rect x="20" y="168" width="740" height="146" rx="5" fill="#f8fafc" stroke="#60a5fa" stroke-width="1.5" stroke-dasharray="5 3"/>
<text x="390" y="186" font-family="sans-serif" font-size="11" font-weight="bold" fill="#1e3a8a" text-anchor="middle">broadcast to every attached client, seq gapless</text>
<line x1="296" y1="212" x2="64" y2="212" stroke="#2563eb" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<line x1="304" y1="212" x2="716" y2="212" stroke="#2563eb" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<text x="180" y="206" font-family="sans-serif" font-size="11" fill="#1e3a8a" text-anchor="middle">turn_prompt (seq 42)</text>
<line x1="296" y1="244" x2="64" y2="244" stroke="#2563eb" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<line x1="304" y1="244" x2="716" y2="244" stroke="#2563eb" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<text x="180" y="238" font-family="sans-serif" font-size="11" fill="#1e3a8a" text-anchor="middle">turn_start (43)</text>
<line x1="296" y1="276" x2="64" y2="276" stroke="#2563eb" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<line x1="304" y1="276" x2="716" y2="276" stroke="#2563eb" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<text x="180" y="270" font-family="sans-serif" font-size="11" fill="#1e3a8a" text-anchor="middle">text, tool_use, tool_result (44-46)</text>
<line x1="296" y1="304" x2="64" y2="304" stroke="#2563eb" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<line x1="304" y1="304" x2="716" y2="304" stroke="#2563eb" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<text x="180" y="298" font-family="sans-serif" font-size="11" fill="#1e3a8a" text-anchor="middle">turn_end (47)</text>
<line x1="300" y1="340" x2="300" y2="366" stroke="#334155" stroke-width="1.5" marker-end="url(#ap-tip)"/>
<text x="312" y="360" font-family="sans-serif" font-size="11" fill="#334155">the finished turn goes to the store, prompt and events together</text>
</svg>

Both tabs draw the same conversation from the same messages. Tab A draws
nothing of its own: it cleared its input on submit and waited for
`turn_prompt` like everyone else.

---

## Session REST

For your own furniture, mounted under the same prefix:

| Method | Path | Returns |
|---|---|---|
| `GET` | `/sessions/{topic}` | `{"topic": …, "sessions": [{"session": …, "last_active": …, "turns": …, "preview": …}]}`, newest first |
| `POST` | `/sessions/{topic}` | `{"topic": …, "session": …}`, 201; no process starts until a submit |
| `DELETE` | `/sessions/{topic}/{session}` | `{"topic": …, "session": …, "existed": bool}` |

A `GET` gives you what a session picker needs without reading any transcripts:

| Field | Is |
|---|---|
| `last_active` | epoch seconds |
| `turns` | how many exchanges the conversation holds — turns, not the messages inside them |
| `preview` | the first thing a **person** asked, on one line and cut to 120 characters, or `null` for a conversation nobody has spoken in yet |

A turn your application submitted itself — an `opening_prompt`, or any
`Session.submit(..., assemble=False)` — is never the preview. It is your words
rather than a person's, and it would otherwise label every conversation with the
same line. Transcripts written before r2d2box recorded this distinction preview
their opening turn.

The listing covers both halves of a topic and combines them per session: a
conversation whose transcript is on disk but whose process was evicted, and one
created a moment ago whose first turn is still running. `turns` counts the
stored turns plus the running ones, and a session with nothing stored yet
previews from the turn in flight.

**What it costs.** `FileTranscriptStore` opens every transcript under the topic
to count its turns, since one turn is one line. It never parses past the
preview, so the cost is I/O rather than JSON. A topic holding many long
conversations pays for its listing; if that is your shape, back `list_sessions`
with a store that keeps the two numbers alongside the transcript.

`DELETE` ends a conversation for good — it stops the agent and clears the
transcript — and announces itself over the socket. Every attached client gets
`session_closed` before its subscription is dropped. The shipped box answers by
clearing itself and attaching to a fresh session under the same topic; to choose
the next conversation yourself, listen for `session_closed` and call `attach`
from the handler.

Both keys are path segments here. A host whose topic keys can contain a slash
should reach `AgentHost` directly instead — the WebSocket carries topics in a
JSON body and has no such limit.

---

## Failure behavior

What r2d2box does when things go wrong, so you know what not to build.

| Failure | What happens |
|---|---|
| A `tool_result` bigger than 16 MiB | one synthetic `error` message; the stream continues and the turn finishes |
| agent-proxy dies mid-turn | every open turn is stored with an error outcome, and `process_exited` is broadcast; the next submit respawns and resumes |
| A resume that fails | retried once against the same id, then a fresh conversation with a warning logged |
| The client disconnects mid-turn | the turn runs to completion and keeps appending to the transcript |
| A background task finishes with nobody connected | the server-side task set is updated anyway; the next `attached` reports it |
| A session idle past the timeout | its agent is stopped, the conversation is kept, the next submit resumes it |
| A client that stops reading | dropped after 512 queued messages, with one last message saying why |
| A socket that drops | the box reconnects on a 0.5s→10s backoff and re-attaches, and the transcript in `attached` covers what it missed |
| `marked` or DOMPurify missing | assistant text is shown verbatim; unsanitized HTML never reaches the DOM |
| `build_prompt` raises | the submit fails with that exception; no prompt is sent |

Two of these are worth planning around. A dropped socket is currently
**silent** — the box fires a `disconnected` event but draws nothing, so a server
restart looks like a conversation that has gone quiet. Draw your own indicator
from `connected`/`disconnected` if that matters. And a client dropped for
falling behind gets a connection-scoped `error` with `fatal: true` before the
socket closes; reconnecting is the recovery, and the transcript makes it whole.

---

## Migrating an existing app

r2d2box was extracted from two applications that had each built this machinery
separately, so a migration is mostly deletion. What follows is the inventory,
grouped by the part of your app it touches. Not every row will apply — the two
originals differed on transport, on how many agents they ran, and on what they
did to a prompt before sending it.

### What goes, whatever your app looks like

| Delete | Replaced by |
|---|---|
| the agent-proxy subprocess manager (spawn, `ready`, 16 MiB limit, stderr drain, terminate-then-kill) | `AgentConfig` plus the mounted router |
| the `ref` → `ack.turn.id` correlation | `Session.submit`, which returns the turn id |
| the session registry and its resume logic | topics and sessions, with `--resume` handled inside |
| the transcript file handling and its replay | a `TranscriptStore` and the `attached` message |
| the chat renderer: Markdown, tool folding, thinking blocks, scroll anchoring, Enter/Shift+Enter | `R2D2Box.mount` |

You also gain three things a hand-rolled implementation rarely has: an opening
turn for a new conversation, a transcript that survives the host process, and
fan-out — two tabs on one conversation see one stream.

### Transport

| Today | After |
|---|---|
| SSE, one request per turn | the WebSocket at `{prefix}/ws` |
| a keepalive keeping a quiet SSE connection from being reaped | **delete it** — a WebSocket is not what a browser prunes for idleness |
| chat multiplexed onto a WebSocket you already have | a second socket, r2d2box's own; keep yours for everything else |
| a handshake answering chat and non-chat questions together | only the non-chat half stays yours |

SSE-per-turn is the one that forces the most change, and the reason is
structural rather than stylistic: it cannot deliver anything *between* turns,
which is exactly how unowned turns and background-task completions arrive. An
app on SSE is usually dropping both without having decided to.

### The agent registry

| Today | After |
|---|---|
| a registry keyed by whatever you are talking about | one topic key per thing; sessions under it are the library's |
| one agent for the whole app | one topic, with sessions for whatever your picker lists |
| a process per browser tab, each resuming the same conversation id | one session, fanned out — the tabs are views |
| displacing a second client and closing its socket | fan-out; a second tab is a view, not an error |
| terminate-on-idle while keeping the session id | the idle sweeper, which does exactly this |
| a 20-minute idle timeout and a cap for turns still running | `idle_timeout_s=1200`, `pending_evict_cap_s=14400` — the defaults |

The third row is a silent bug worth checking for. Two tabs that each spawn a
process while resuming one conversation id will each write turns the other's
process never sees, and nothing surfaces the divergence.

### Configuration and prompts

| Today | After |
|---|---|
| a system prompt built per item from your own data | the same read, inside `agent_config` — and now re-read at every spawn |
| `--mcp-config` mounting your MCP server for one item | `AgentConfig.mcp_config`, built in the same callback |
| prepending selected text or other context to every prompt | `box.setContext(…)` plus `build_prompt` |
| a host-side effect after a particular tool succeeds | `box.on('tool_result', …)` |
| a server-side flag saying this conversation has an agent running | `on_activity`, or `session.active` |

### The front-end

| Today | After |
|---|---|
| `{role: …}` re-encoding of the message stream | forwarded messages, each naming the turn it belongs to — switch on `type` |
| a client-side set of outstanding background task ids | `task_ids` from `attached` and `status`; **delete the client-side set** |
| `marked.parse()` output assigned straight to `innerHTML` | sanitized with DOMPurify first, always |
| your own session picker and close button | still yours, on `GET /sessions/{topic}` and `box.attach` — `demo/index.html` shows the shape |
| an idle inhibitor or similar held for the length of a turn | still yours — drive it from `box.on('turn_start')` and `box.on('turn_end')` |

### Storage

| Today | After |
|---|---|
| server-side JSON-lines transcripts | `FileTranscriptStore`, which is the same shape |
| one JSON file per session | `FileTranscriptStore`; keep picker titles and timestamps yourself |
| a close-session button posting to your own route | `DELETE /sessions/{topic}/{session}` |

### What your users will notice

Two changes are visible rather than internal. The panel takes r2d2box's
appearance — role-labelled blocks, folded tool history, a status indicator at
the foot of the list — so a migration is a visual change even where it is not a
behavioral one. And turns the agent starts on its own appear as "Background
task" blocks; an app that drained them silently starts showing them.

---

## See also

[README.md](README.md) is the shortest path to a running box, `demo/` is a
whole host application in 89 lines, and the module docstrings carry the
reasoning for anything here that reads as arbitrary.

The one document outside this repository that matters is agent-proxy's own
`API.md`. It defines the message types r2d2box forwards, and is fixed input to
everything above.
