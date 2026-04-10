"""YADE MCP Server - YADE DEM simulation tools exposed over MCP."""

import argparse
import asyncio
import logging
import os
from typing import Any

from fastmcp import FastMCP

from yade_mcp import __version__
from yade_mcp.bridge import close_bridge_client
from yade_mcp.tools import (
    browse_api,
    check_task_status,
    execute_code,
    execute_task,
    interrupt_task,
    list_tasks,
    query_api,
)

mcp = FastMCP(
    "YADE MCP Server",
    instructions=(
        "YADE (Yet Another Dynamic Engine) MCP server. "
        "Provides tools for browsing YADE Python API documentation "
        "and for executing DEM simulations and managing long-running tasks "
        "through a yade-mcp-bridge WebSocket service running inside a YADE process.\n\n"
        "Architecture: this MCP server and the YADE process are separate. "
        "Documentation tools query a local knowledge base and work independently. "
        "Execution tools communicate with a live YADE process through "
        "yade-mcp-bridge — a WebSocket service the user starts inside YADE. "
        "When execution tools return a bridge connectivity error, "
        "the user needs to start the bridge, not retry.\n\n"
        "Execution tool responses may include a _context field — "
        "runtime state from the YADE process (e.g. user console history, "
        "simulation status). Use this to stay aware of what is happening "
        "in YADE beyond this session."
    ),
)

logger = logging.getLogger("yade-mcp.server")

# Register documentation tools
browse_api.register(mcp)
query_api.register(mcp)

# Register execution tools
execute_task.register(mcp)
check_task_status.register(mcp)
list_tasks.register(mcp)
interrupt_task.register(mcp)
execute_code.register(mcp)


def main() -> None:
    """Entry point for the YADE MCP server."""
    parser = argparse.ArgumentParser(
        prog="yade-mcp",
        description="YADE MCP Server - YADE DEM simulation tools exposed over MCP",
    )
    parser.add_argument("--version", "-v", action="version", version=f"yade-mcp {__version__}")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind when using http/sse transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind when using http/sse transport (default: 8000)",
    )
    parser.add_argument(
        "--bridge-url",
        default=None,
        help="Bridge WebSocket URL (default: ws://localhost:9002, or YADE_MCP_BRIDGE_URL env)",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="warning",
        help="Log level for yade-mcp (default: warning)",
    )
    args = parser.parse_args()

    if args.bridge_url:
        os.environ["YADE_MCP_BRIDGE_URL"] = args.bridge_url

    # Configure logging
    level = getattr(logging, args.log_level.upper())
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("yade-mcp").setLevel(level)
    # Keep uvicorn quiet unless user asks for debug/info
    uvicorn_level = level if level <= logging.INFO else logging.CRITICAL
    logging.getLogger("uvicorn").setLevel(uvicorn_level)
    logging.getLogger("uvicorn.error").setLevel(uvicorn_level)

    run_kwargs: dict[str, Any] = {
        "transport": args.transport,
        "show_banner": False,
        "log_level": args.log_level.upper(),
    }
    if args.transport in ("http", "sse"):
        run_kwargs["host"] = args.host
        run_kwargs["port"] = args.port

    try:
        mcp.run(**run_kwargs)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            asyncio.run(close_bridge_client())
        except Exception as exc:
            logger.debug("Bridge client cleanup skipped: %s", exc)


if __name__ == "__main__":
    main()
