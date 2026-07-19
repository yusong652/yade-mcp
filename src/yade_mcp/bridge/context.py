"""Runtime context collection from the bridge and injection into tool responses."""

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from yade_mcp.bridge.client import get_bridge_client
from yade_mcp.settings import is_debug_mode

logger = logging.getLogger("yade-mcp.context")


async def fetch_bridge_context() -> dict[str, Any] | None:
    """Fetch runtime context from the bridge; None if unavailable or nothing new."""
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


def with_context(func: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Decorator that attaches bridge ``_context`` to a tool's envelope (success or error)."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        envelope = await func(*args, **kwargs)
        # Production mode: strip the diagnostic `error.details` tier before
        # the response leaves the MCP boundary.
        if not is_debug_mode() and isinstance(envelope, dict) and envelope.get("ok") is False:
            error = envelope.get("error")
            if isinstance(error, dict):
                error.pop("details", None)
        try:
            context = await fetch_bridge_context()
            if context and isinstance(envelope, dict):
                envelope["_context"] = context
        except Exception:
            logger.debug("Context injection skipped", exc_info=True)
        return envelope

    return wrapper


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
