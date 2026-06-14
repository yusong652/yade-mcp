# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Console history query handler for MCP bridge."""

import logging

logger = logging.getLogger("MCP-Bridge")


def handle_console_history(ctx, data):
    """Handle console_history message.

    Two modes:
    - consume (default): return undelivered entries, advance cursor
    - query: return entries since a given ID (read-only)
    """
    request_id = data.get("request_id", "unknown")
    limit = data.get("limit", 20)

    if ctx.console_history is None:
        return {
            "type": "console_history_result",
            "request_id": request_id,
            "status": "error",
            "message": "Console history not available",
            "data": None,
        }

    since_id = data.get("since_id")
    if since_id is not None:
        # Read-only query, does not advance cursor
        result = ctx.console_history.query(since_id=since_id, limit=limit)
    else:
        # Consume: return new entries and advance cursor
        result = ctx.console_history.consume(limit=limit)

    return {
        "type": "console_history_result",
        "request_id": request_id,
        "status": "success",
        "message": "OK",
        "data": result,
    }
