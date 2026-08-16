"""r2d2box — an embeddable chat box for AI agents.

The public surface grows one build phase at a time. Phase 1 is the process
layer: how a host describes an agent, and one agent-proxy subprocess driven
correctly. Phase 2 adds the conversation on top of it — `AgentHost`, `Session`
and the transcript stores. The router and the browser half arrive in phases 3
and 4.
"""

from .config import DEFAULT_PROXY_BIN, AgentConfig, build_argv
from .host import AgentHost
from .proxy import STREAM_LIMIT, AgentProxy, ProxyStartError
from .session import Session, SubmitRejected, Subscriber
from .store import (
    FileTranscriptStore,
    MemoryTranscriptStore,
    SessionInfo,
    TranscriptStore,
    Turn,
)

__all__ = [
    "AgentConfig",
    "AgentHost",
    "AgentProxy",
    "DEFAULT_PROXY_BIN",
    "FileTranscriptStore",
    "MemoryTranscriptStore",
    "ProxyStartError",
    "STREAM_LIMIT",
    "Session",
    "SessionInfo",
    "SubmitRejected",
    "Subscriber",
    "TranscriptStore",
    "Turn",
    "build_argv",
]
