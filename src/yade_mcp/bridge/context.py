"""Bridge context manager — collects runtime context from bridge.

Gathers all relevant context (console history, and future: system
stats, warnings, etc.) for injection into every MCP tool response.
Called automatically by build_ok() in contracts.py.
"""

import logging
from typing import Any

from yade_mcp.bridge.client import get_bridge_client

logger = logging.getLogger("yade-mcp.context")


async def fetch_bridge_context() -> dict[str, Any] | None:
    """Fetch context from bridge for injection into tool responses.

    Returns a dict with context sections, or None if bridge is
    unavailable or there's nothing new.
    """
    context: dict[str, Any] = {}

    try:
        client = await get_bridge_client()
        response = await client.consume_console_history()
        data = response.get("data") or {}
        entries = data.get("entries", [])
        if entries:
            context["user_console"] = {
                "description": "Recent commands the USER typed in the YADE interactive console. Not from this LLM session.",
                "entries": [_format_entry(e) for e in entries],
            }
    except Exception as exc:
        logger.warning("Console context unavailable: %s", exc)

    # Future: add more context sources here

    return context if context else None


def _format_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Format a console entry for LLM consumption."""
    formatted: dict[str, Any] = {"input": entry.get("input", "")}
    output = entry.get("output", "")
    if output:
        formatted["output"] = output
    result = entry.get("result")
    if result is not None:
        formatted["result"] = result
    if not entry.get("success", True):
        formatted["error"] = True
    return formatted
