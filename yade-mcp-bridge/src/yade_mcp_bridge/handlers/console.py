# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Console history query handler for MCP bridge."""

import logging

logger = logging.getLogger("MCP-Bridge")


def handle_console_history(ctx, data):
    """Return the user's latest console input from the live YADE session."""
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

    result = ctx.console_history.consume(limit=limit)

    return {
        "type": "console_history_result",
        "request_id": request_id,
        "status": "success",
        "message": "OK",
        "data": result,
    }
