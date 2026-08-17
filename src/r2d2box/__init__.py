"""r2d2box — an embeddable chat box for AI agents.

The surface is four layers, each usable without the ones above it. `config.py`
and `proxy.py` are the process layer: how a host describes an agent, and one
agent-proxy subprocess driven correctly. `session.py`, `host.py` and `store.py`
are the conversation — turns, fan-out, topics, resume, transcripts. `router.py`
is `R2D2Box`, the mountable router that puts a browser in front of all of it.
The browser half is not Python at all: it ships as `static/r2d2box.js` inside
this package and is served by that router.

A host application needs `R2D2Box` and `AgentConfig`, plus a store; everything
else here is for a caller reaching below the router on purpose.
"""

from .config import DEFAULT_PROXY_BIN, AgentConfig, build_argv
from .host import AgentHost
from .proxy import STREAM_LIMIT, AgentProxy, ProxyStartError
from .router import DEFAULT_CLIENT_QUEUE_LIMIT, R2D2Box
from .session import Session, SubmitRejected, Subscriber
from .store import (
    FileTranscriptStore,
    MemoryTranscriptStore,
    SessionInfo,
    TranscriptStore,
    Turn,
    preview_of,
)

__all__ = [
    "AgentConfig",
    "AgentHost",
    "AgentProxy",
    "DEFAULT_CLIENT_QUEUE_LIMIT",
    "DEFAULT_PROXY_BIN",
    "FileTranscriptStore",
    "MemoryTranscriptStore",
    "ProxyStartError",
    "R2D2Box",
    "STREAM_LIMIT",
    "Session",
    "SessionInfo",
    "SubmitRejected",
    "Subscriber",
    "TranscriptStore",
    "Turn",
    "build_argv",
    "preview_of",
]
