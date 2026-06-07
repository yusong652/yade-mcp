"""Utility message handlers for YADE bridge."""


def handle_ping(ctx, data):
    """Handle ping message for connection health check."""
    request_id = data.get("request_id", "unknown")
    return {
        "type": "result",
        "request_id": request_id,
        "status": "success",
        "message": "pong",
        "data": {"runtime_mode": ctx.runtime_mode},
    }
