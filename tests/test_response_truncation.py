"""Tests for MCP-side handling of oversized bridge responses.

Bridge-side truncation itself is covered in
``tests/bridge/test_response_truncation.py`` against the real implementation.
"""


class TestBridgeResponseTooLargeError:
    """Test that BridgeResponseTooLargeError produces correct error output."""

    def test_exception_is_connection_error(self):
        from yade_mcp.bridge.client import BridgeResponseTooLargeError
        exc = BridgeResponseTooLargeError("test")
        assert isinstance(exc, ConnectionError)

    def test_build_bridge_error_response_too_large(self):
        from yade_mcp.bridge.client import BridgeResponseTooLargeError
        from yade_mcp.formatting import build_bridge_error

        exc = BridgeResponseTooLargeError("output exceeds limit")
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
