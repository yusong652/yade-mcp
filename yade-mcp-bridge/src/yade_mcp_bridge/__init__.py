# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""YADE MCP Bridge - HTTP + SSE bridge for YADE DEM simulation.

Runs inside YADE's Python environment and exposes simulation control
as a remote HTTP API (request/response over POST, server push over a
Server-Sent Events stream) for MCP clients.

Usage (inside YADE Python console):
    import yade_mcp_bridge
    yade_mcp_bridge.start()

Usage (batch/console mode):
    import yade_mcp_bridge
    yade_mcp_bridge.start(mode="console")
"""

from .startup import start

__version__ = "0.8.0"

__all__ = ["start"]
