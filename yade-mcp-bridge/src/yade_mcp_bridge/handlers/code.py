# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""execute_code message handler for the MCP bridge."""

from .helpers import requireField


def handleExecuteCode(ctx, data):
    """Run a code snippet synchronously, blocking for its result."""
    requestId = data.get("request_id", "unknown")

    code, err = requireField(data, "code", requestId, "execute_code_result")
    if err:
        return err

    timeoutMs = data.get("timeout_ms", 10000)
    return ctx.codeRunner.run(requestId, code, timeoutMs)
