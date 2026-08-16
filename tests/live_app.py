"""A mounted `R2D2Box` for a real server to run, built from the environment.

`test_live_chat_box.py` starts this under uvicorn so the chat box can be driven
over a real WebSocket. Nothing here is a stand-in — real processes, a real
`claude` — and everything the test needs to control comes in as an environment
variable, because the whole module has to be importable by name from a
subprocess.

    R2D2BOX_LIVE_STORE   where transcripts go
    R2D2BOX_LIVE_CWD     the agent's working directory
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from r2d2box import AgentConfig, FileTranscriptStore, R2D2Box

WORKDIR = Path(os.environ["R2D2BOX_LIVE_CWD"])
STORE = Path(os.environ["R2D2BOX_LIVE_STORE"])


def agent_config(topic: str, session: str) -> AgentConfig:
    """The smallest, fastest real agent that can still answer a question."""
    return AgentConfig(
        cwd=WORKDIR,
        append_system_prompt="Answer in as few words as possible.",
        extra_args=["--model", "haiku"],
    )


box = R2D2Box(agent_config, store=FileTranscriptStore(STORE))
app = FastAPI(lifespan=box.lifespan)
app.include_router(box.router, prefix="/chat")
