"""Shared helper functions for message handlers."""


def require_field(data, field_name, request_id, response_type="result"):
    """Validate that a required field exists and is non-empty."""
    value = data.get(field_name, "")
    if not value:
        return None, {
            "type": response_type,
            "request_id": request_id,
            "status": "error",
            "message": "{} required".format(field_name),
            "data": None
        }
    return value, None


def truncate_message(message, max_length=5000):
    """Truncate message if too long."""
    if len(message) <= max_length:
        return message
    return message[:max_length] + "\n... (truncated from {} chars)".format(len(message))
