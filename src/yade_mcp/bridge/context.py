"""Bridge context manager — collects runtime context from bridge.

Two pieces live here:

* ``fetch_bridge_context`` — pulls the current runtime signals (console
  history today; system stats, GUI presence, warnings tomorrow) from
  the bridge and returns a single dict.
* ``with_context`` — decorator applied at the tool boundary that awaits
  the tool's envelope (success OR error) and attaches ``_context`` in
  one centralised place. Keeping this outside of ``contracts.py`` means
  the contract layer stays pure/sync and both ok/error branches get
  identical context treatment without duplicated logic.
"""

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from yade_mcp.bridge.client import get_bridge_client
from yade_mcp.settings import is_debug_mode

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


def with_context(func: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Decorator that injects ``_context`` into a tool's envelope.

    Wraps an async MCP tool handler. After the handler returns its
    envelope (from ``build_ok`` or ``build_error``), fetches current
    bridge context and attaches it as ``_context``. Applies uniformly
    to both success and error branches — that symmetry is the whole
    point of centralising here instead of inside the contract builders.

    Failures to fetch context are swallowed: the tool's own result is
    always returned, context is best-effort.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        envelope = await func(*args, **kwargs)
        # Production mode: strip `error.details` before the response
        # leaves the MCP boundary. `details` is our diagnostic tier
        # (traceback, exception type, internal paths) — useful during
        # development, noise/risk in deployed setups.
        if (
            not is_debug_mode()
            and isinstance(envelope, dict)
            and envelope.get("ok") is False
        ):
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
