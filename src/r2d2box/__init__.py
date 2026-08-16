"""r2d2box — an embeddable chat box for AI agents.

The public surface grows one build phase at a time. Phase 1 is the process
layer: how a host describes an agent, and one agent-proxy subprocess driven
correctly. `R2D2Box`, the stores and the router arrive in phases 2 and 3.
"""

from .config import DEFAULT_PROXY_BIN, AgentConfig, build_argv
from .proxy import STREAM_LIMIT, AgentProxy, ProxyStartError

__all__ = [
    "AgentConfig",
    "AgentProxy",
    "DEFAULT_PROXY_BIN",
    "ProxyStartError",
    "STREAM_LIMIT",
    "build_argv",
]
