"""A standalone HTTP host for the same router applications embed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from collections.abc import Sequence

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse

from . import AgentConfig, FileTranscriptStore, R2D2Box, __version__, build_argv


def create_app(config: AgentConfig, store: Path) -> FastAPI:
    """Build the standalone host without starting a server or an agent."""
    box = R2D2Box(lambda topic, session: config, store=FileTranscriptStore(store))
    app = FastAPI(title="r2d2box", lifespan=box.lifespan)
    app.state.box = box
    app.include_router(box.router, prefix="/chat")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(Path(__file__).with_name("server.html"))

    return app


def main(argv: Sequence[str] | None = None) -> None:
    """Parse launch configuration and serve sessions until interrupted."""
    parser = argparse.ArgumentParser(description="Serve r2d2box sessions over HTTP and WebSocket.")
    parser.add_argument("--version", action="version", version=f"r2d2box {__version__}")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8790, help="HTTP port (default: 8790)")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="agent working directory (default: current directory)")
    parser.add_argument("--store", type=Path, default=Path.home() / ".local/share/r2d2box/transcripts",
                        help="transcript directory (default: ~/.local/share/r2d2box/transcripts)")
    parser.add_argument("--agent", choices=("claude", "codex"), default="claude")
    parser.add_argument("--no-bypass-permissions", action="store_true", help="enable native permission checks and approval forms")
    parser.add_argument("--append-system-prompt", help="additional system instructions")
    parser.add_argument("--mcp-config", type=Path, help="JSON MCP configuration file (Claude only)")
    parser.add_argument("--allowed-tool", action="append", default=[], help="allowed Claude tool; repeat for multiple tools")
    parser.add_argument("--proxy-bin", help="agent-proxy executable (default: agent-proxy on PATH)")
    parser.add_argument("--proxy-arg", action="append", default=[], help="extra proxy argument; use --proxy-arg=--flag for flags")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        parser.error(f"--cwd is not a directory: {cwd}")
    try:
        mcp_config = None
        if args.mcp_config is not None:
            mcp_config = json.loads(args.mcp_config.expanduser().read_text())
            if not isinstance(mcp_config, dict):
                raise ValueError("--mcp-config must contain a JSON object")
        config = AgentConfig(
            cwd=cwd, agent=args.agent,
            bypass_permissions=not args.no_bypass_permissions,
            append_system_prompt=args.append_system_prompt,
            mcp_config=mcp_config, allowed_tools=args.allowed_tool,
            proxy_bin=args.proxy_bin, extra_args=args.proxy_arg,
        )
        build_argv(config)
        store = args.store.expanduser().resolve()
        store.mkdir(parents=True, exist_ok=True)
        app = create_app(config, store)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=args.host, port=args.port, ws="websockets", workers=1)
