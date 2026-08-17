"""A whole host application for r2d2box, so the library can be seen working.

    .venv/bin/python -m uvicorn demo.app:app --port 8790
    # then open http://localhost:8790/

Everything here is what a real host supplies and nothing else: the agent's
configuration, the topic keys, and the page the box is mounted into. The
conversation — its process, its transcript, its fan-out and its front-end — is
the library's.

Two things worth copying into a real host. `agent_config` is a callback rather
than an object, so the system prompt is assembled at every
spawn and never goes stale; and `box.lifespan` is wired into the app, because
nothing else terminates the agent-proxy processes at shutdown.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from r2d2box import AgentConfig, FileTranscriptStore, R2D2Box

DEMO_DIR = Path(__file__).parent
TRANSCRIPTS = Path(os.environ.get("R2D2BOX_DEMO_STORE", DEMO_DIR / "transcripts"))


def agent_config(topic: str, session: str) -> AgentConfig:
    """The agent behind one conversation, resolved every time a process starts.

    A real host builds the system prompt from whatever it knows about the
    topic — reading a bug's summary out of its own database, say — and mounts
    its own MCP servers here. The demo has no data of its own, so it only tells
    the agent what it is being asked about.
    """
    return AgentConfig(
        cwd=DEMO_DIR,
        append_system_prompt=(
            f"You are the assistant in a demo chat panel, talking about {topic!r}. "
            "Keep answers to a few sentences."
        ),
    )


def opening_prompt(topic: str, session: str) -> str | None:
    """The turn a brand-new conversation starts with, before anyone types.

    Called once per new session and never for one that is resuming, whoever
    created it — including the chat box attaching with no session named, which
    is the case a host cannot otherwise get in front of. Off by default here
    because it spawns an agent the moment a session appears, which is real
    quota for a demo nobody may type into:

        R2D2BOX_DEMO_OPENING="Introduce yourself in one line." uvicorn …
    """
    return os.environ.get("R2D2BOX_DEMO_OPENING") or None


def build_prompt(topic: str, session: str, text: str, context) -> str:
    """Turn what was typed plus the box's ride-along context into the real prompt.

    The motivating case is a host that prepends the document text a reader
    has selected. The demo does the same with whatever the page put in
    `context`, which is the note typed into the box's context field.
    """
    if not context:
        return text
    return f"[the reader is looking at: {context}]\n\n{text}"


def on_activity(topic: str, session: str, active: bool) -> None:
    """Say on the console when a conversation starts working and when it stops.

    This is the server's own busy light, and nothing about it reaches the
    browser — the box already knows, from the turn it is watching. A real host
    writes it somewhere it can act on: the row it keeps for the conversation,
    so a list of them can show which are running. The demo keeps no such list,
    so it prints.
    """
    print(f"[r2d2box] {topic}/{session} is {'working' if active else 'idle'}", flush=True)


box = R2D2Box(
    agent_config,
    build_prompt=build_prompt,
    opening_prompt=opening_prompt,
    on_activity=on_activity,
    store=FileTranscriptStore(TRANSCRIPTS),
    idle_timeout_s=10 * 60,
)

app = FastAPI(title="r2d2box demo", lifespan=box.lifespan)
app.include_router(box.router, prefix="/chat")


@app.get("/")
async def index() -> FileResponse:
    """The one page: a topic picker, the host's own session furniture, and the box."""
    return FileResponse(DEMO_DIR / "index.html")
