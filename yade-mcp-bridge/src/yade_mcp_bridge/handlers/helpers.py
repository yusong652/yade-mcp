# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Shared helper functions for message handlers."""

from ..utils import errorResponse


def requireField(data, fieldName, requestId, responseType="result"):
    """Validate that a required field exists and is non-empty.

    On failure returns ``(None, error_envelope)`` where the envelope carries a
    machine-readable ``error.code == "missing_field"`` and the offending field
    name in ``details.field`` — a structured error rather than a free-form
    ``status``/``message`` to pattern-match.
    """
    value = data.get(fieldName, "")
    if not value:
        return None, errorResponse(
            responseType,
            requestId,
            "missing_field",
            f"{fieldName} required",
            details={"field": fieldName},
        )
    return value, None


def truncateMessage(message, maxLength=5000):
    """Truncate message if too long."""
    if len(message) <= maxLength:
        return message
    return message[:maxLength] + f"\n... (truncated from {len(message)} chars)"
