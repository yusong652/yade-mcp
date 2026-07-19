"""Formatting and error rendering helpers for MCP tool outputs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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


def round_elapsed_seconds(value: Any) -> float | None:
    """Round an elapsed-seconds value to 2 decimals; non-numeric values become None."""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


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
    details: dict[str, Any] = {
        "bridge_url": cfg.url,
        "reason": _summarize_bridge_error(exc),
        "action": "start yade-mcp-bridge in YADE process, then retry",
    }
    if task_id:
        details["task_id"] = task_id
    return build_error("bridge_unavailable", "Bridge unavailable", details)


def build_operation_error(
    code: str,
    message: str,
    *,
    reason: str | None = None,
    task_id: str | None = None,
    action: str | None = None,
    data: Any | None = None,
    **extras: Any,
) -> dict[str, Any]:
    """Build a unified error envelope for operation failures."""
    details: dict[str, Any] = {}
    if reason:
        details["reason"] = reason
    if task_id:
        details["task_id"] = task_id
    if action:
        details["action"] = action
    # Extra diagnostic fields go into details as-is; None values are dropped
    # so callers can forward dict lookups unguarded.
    for key, value in extras.items():
        if value is not None:
            details[key] = value
    # `data` rides at the envelope top level (not inside details) so captured
    # output survives the production-mode details strip.
    return build_error(code, message, details or None, data=data)
