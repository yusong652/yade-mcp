"""Shared helper functions for message handlers."""

from ..utils import error_response


def require_field(data, field_name, request_id, response_type="result"):
    """Validate that a required field exists and is non-empty.

    On failure returns ``(None, error_envelope)`` where the envelope carries a
    machine-readable ``error.code == "missing_field"`` and the offending field
    name in ``details.field`` — so the MCP server lifts a structured error
    instead of pattern-matching a free-form ``status``/``message``.
    """
    value = data.get(field_name, "")
    if not value:
        return None, error_response(
            response_type,
            request_id,
            "missing_field",
            f"{field_name} required",
            details={"field": field_name},
        )
    return value, None


def truncate_message(message, max_length=5000):
    """Truncate message if too long."""
    if len(message) <= max_length:
        return message
    return message[:max_length] + f"\n... (truncated from {len(message)} chars)"
