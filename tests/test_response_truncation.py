"""Tests for WebSocket response truncation and error handling."""

import json
import pytest


def _truncate_response(response, max_bytes):
    """Mirror of YADEWebSocketServer._truncate_response for testing."""
    data = response.get("data", {})
    if isinstance(data, dict) and "output" in data:
        output = data["output"]
        if isinstance(output, str) and len(output) > 10000:
            data["output"] = output[:10000] + (
                f"\n\n... [TRUNCATED: output was {len(output)} chars, "
                f"exceeds WebSocket size limit. "
                f"Consider writing output to file instead of printing.]"
            )
            response["data"] = data
    return response


class TestTruncateResponse:
    """Test response truncation logic (mirrors YADEWebSocketServer._truncate_response)."""

    @staticmethod
    def _truncate(response, max_bytes=1000):
        return _truncate_response(response, max_bytes)

    def test_small_response_unchanged(self):
        response = {"status": "ok", "data": {"output": "hello"}}
        result = self._truncate(response)
        assert result["data"]["output"] == "hello"

    def test_large_output_truncated(self):
        big_output = "x" * 50000
        response = {"status": "ok", "data": {"output": big_output}}
        result = self._truncate(response)
        assert len(result["data"]["output"]) < len(big_output)
        assert "[TRUNCATED:" in result["data"]["output"]
        assert "50000 chars" in result["data"]["output"]

    def test_truncated_output_starts_with_original(self):
        big_output = "line_" + "x" * 50000
        response = {"status": "ok", "data": {"output": big_output}}
        result = self._truncate(response)
        assert result["data"]["output"].startswith("line_")

    def test_no_data_key(self):
        response = {"status": "ok", "message": "no data"}
        result = self._truncate(response)
        assert result == response

    def test_no_output_key(self):
        response = {"status": "ok", "data": {"result": 42}}
        result = self._truncate(response)
        assert result["data"]["result"] == 42

    def test_short_output_not_truncated(self):
        response = {"status": "ok", "data": {"output": "short"}}
        result = self._truncate(response)
        assert result["data"]["output"] == "short"
        assert "[TRUNCATED" not in result["data"]["output"]


class TestBridgeResponseTooLarge:
    """Test that BridgeResponseTooLarge produces correct error output."""

    def test_exception_is_connection_error(self):
        from yade_mcp.bridge.client import BridgeResponseTooLarge
        exc = BridgeResponseTooLarge("test")
        assert isinstance(exc, ConnectionError)

    def test_build_bridge_error_response_too_large(self):
        from yade_mcp.bridge.client import BridgeResponseTooLarge
        from yade_mcp.formatting import build_bridge_error

        exc = BridgeResponseTooLarge("output exceeds limit")
        result = build_bridge_error(exc, task_id="task-123")

        assert result["ok"] is False
        assert result["error"]["code"] == "response_too_large"
        assert result["error"]["message"] == "Bridge response too large"
        assert "write to file" in result["error"]["details"]["action"]
        assert result["error"]["details"]["task_id"] == "task-123"

    def test_build_bridge_error_normal_connection_error(self):
        from yade_mcp.formatting import build_bridge_error

        exc = ConnectionError("connection refused")
        result = build_bridge_error(exc, task_id="task-456")

        assert result["error"]["code"] == "bridge_unavailable"
        assert "cannot connect" in result["error"]["details"]["reason"]
