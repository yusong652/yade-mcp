"""Formatting and error rendering helpers for MCP tool outputs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from yade_mcp.bridge.client import BridgeResponseTooLargeError
from yade_mcp.config import get_bridge_config
from yade_mcp.contracts import build_error

# =============================================================================
# Task status / output formatting
# =============================================================================

_LEGACY_STATUS_MAP = {
    "success": "completed",
    "error": "failed",
}


def normalize_status(status: str) -> str:
    """Normalize task status. Maps legacy bridge names for compatibility."""
    return _LEGACY_STATUS_MAP.get(status, status)


def format_unix_timestamp(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return str(value)
    try:
        return datetime.fromtimestamp(timestamp).isoformat(sep=" ", timespec="seconds")
    except Exception:
        return str(value)


def paginate_output(
    output: str,
    skip_newest: int,
    limit: int,
    filter_text: str | None,
) -> tuple[str, dict[str, Any]]:
    lines = output.splitlines() if output else []
    if filter_text:
        lines = [line for line in lines if filter_text in line]

    total_lines = len(lines)
    start_idx = max(0, total_lines - limit - skip_newest)
    end_idx = max(0, total_lines - skip_newest)
    selected = lines[start_idx:end_idx]

    pagination = {
        "total_lines": total_lines,
        "line_range": f"{start_idx + 1}-{end_idx}" if selected else "0-0",
        "has_older": start_idx > 0,
        "has_newer": skip_newest > 0,
    }

    return "\n".join(selected) if selected else "(no output)", pagination


# =============================================================================
# Bridge error formatting
# =============================================================================


def is_bridge_connectivity_error(exc: Exception) -> bool:
    """Best-effort detection for bridge connectivity failures."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True

    lowered = str(exc).strip().lower()
    return (
        "connect call failed" in lowered
        or "connection refused" in lowered
        or "connection lost" in lowered
        or "connection closed" in lowered
        or "bridge" in lowered
        and "unavailable" in lowered
        or "[errno 61]" in lowered
    )


def _summarize_bridge_error(exc: Exception) -> str:
    text = str(exc).strip()
    lowered = text.lower()

    if "connect call failed" in lowered or "connection refused" in lowered or "[errno 61]" in lowered:
        return "cannot connect to bridge service"
    if "timed out" in lowered:
        return "bridge request timed out"
    if "connection closed" in lowered or "connection lost" in lowered:
        return "bridge connection closed"
    if not text:
        return "unknown bridge error"
    return text.splitlines()[0]


def build_bridge_error(exc: Exception, *, task_id: str | None = None) -> dict[str, Any]:
    """Build a unified error envelope for bridge connectivity failures."""
    cfg = get_bridge_config()
    if isinstance(exc, BridgeResponseTooLargeError):
        reason = str(exc)
        action = "reduce output volume (write to file instead of printing)"
        code = "response_too_large"
        message = "Bridge response too large"
    else:
        reason = _summarize_bridge_error(exc)
        action = "start yade-mcp-bridge in YADE process, then retry"
        code = "bridge_unavailable"
        message = "YADE bridge unavailable"
    details: dict[str, Any] = {
        "bridge_url": cfg.url,
        "reason": reason,
        "action": action,
    }
    if task_id:
        details["task_id"] = task_id
    return build_error(code, message, details)


def build_operation_error(
    code: str,
    message: str,
    *,
    reason: str | None = None,
    task_id: str | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    """Build a unified error envelope for operation failures."""
    details: dict[str, Any] = {}
    if reason:
        details["reason"] = reason
    if task_id:
        details["task_id"] = task_id
    if action:
        details["action"] = action
    return build_error(code, message, details or None)
