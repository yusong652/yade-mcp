# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""execute_code message handler for the MCP bridge."""

from .helpers import require_field


def handle_execute_code(ctx, data):
    """Run a code snippet synchronously in the YADE process."""
    request_id = data.get("request_id", "unknown")

    code, err = require_field(data, "code", request_id, "execute_code_result")
    if err:
        return err

    timeout_ms = data.get("timeout_ms", 10000)
    return ctx.code_runner.run(request_id, code, timeout_ms)
