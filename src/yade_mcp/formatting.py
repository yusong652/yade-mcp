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
    """Round an elapsed-seconds value to 2 decimals for agent-facing output.

    The bridge reports full ``time.time()``-delta precision (16 digits);
    the agent only needs centisecond resolution, so the MCP layer trims
    the noise tail. Missing or non-numeric values pass through as None.
    """
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
    """Build a unified error envelope for operation failures.

    ``**extras`` go straight into ``details`` as-is. Use this for
    diagnostic fields that don't warrant a first-class parameter —
    ``exception_type``, ``traceback``, ``log_file``, etc. Values of
    ``None`` are dropped so callers can forward dict lookups without
    guarding each one.

    ``data`` surfaces execution artefacts even on failure — e.g. the
    stdout captured before a crash. It lives at the envelope's top
    level alongside ``error`` (not inside ``details``) so it survives
    the prod-mode details strip: captured output is an execution
    product, not debug metadata.
    """
    details: dict[str, Any] = {}
    if reason:
        details["reason"] = reason
    if task_id:
        details["task_id"] = task_id
    if action:
        details["action"] = action
    for key, value in extras.items():
        if value is not None:
            details[key] = value
    return build_error(code, message, details or None, data=data)
