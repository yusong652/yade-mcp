"""Tests for formatting and error rendering helpers."""

from yade_mcp.formatting import (
    build_bridge_error,
    build_operation_error,
    format_unix_timestamp,
    is_bridge_connectivity_error,
    normalize_status,
)


class TestNormalizeStatus:
    def test_known_statuses(self):
        assert normalize_status("success") == "completed"
        assert normalize_status("error") == "failed"
        assert normalize_status("running") == "running"
        assert normalize_status("pending") == "pending"
        assert normalize_status("interrupted") == "interrupted"

    def test_unknown_status_passthrough(self):
        assert normalize_status("custom_status") == "custom_status"


class TestFormatUnixTimestamp:
    def test_none_returns_na(self):
        assert format_unix_timestamp(None) == "n/a"

    def test_valid_timestamp(self):
        result = format_unix_timestamp(0)
        assert "1970" in result

    def test_string_timestamp(self):
        result = format_unix_timestamp("1000000")
        assert "1970" in result

    def test_invalid_value(self):
        assert format_unix_timestamp("not-a-number") == "not-a-number"

    def test_float_timestamp(self):
        result = format_unix_timestamp(1700000000.0)
        assert "2023" in result


class TestIsBridgeConnectivityError:
    def test_connection_error(self):
        assert is_bridge_connectivity_error(ConnectionError("refused"))

    def test_timeout_error(self):
        assert is_bridge_connectivity_error(TimeoutError("timed out"))

    def test_os_error(self):
        assert is_bridge_connectivity_error(OSError("network unreachable"))

    def test_generic_exception(self):
        assert not is_bridge_connectivity_error(ValueError("bad value"))

    def test_connection_refused_string(self):
        assert is_bridge_connectivity_error(Exception("connection refused"))


class TestBuildBridgeError:
    def test_connection_refused(self):
        result = build_bridge_error(ConnectionError("connection refused"))
        assert result["ok"] is False
        assert result["error"]["code"] == "bridge_unavailable"
        assert "cannot connect" in result["error"]["details"]["reason"]

    def test_response_too_large(self):
        from yade_mcp.bridge.client import BridgeResponseTooLargeError
        result = build_bridge_error(BridgeResponseTooLargeError("output exceeds limit"), task_id="t1")
        assert result["error"]["code"] == "response_too_large"
        assert result["error"]["details"]["task_id"] == "t1"
        assert "write to file" in result["error"]["details"]["action"]

    def test_task_id_included(self):
        result = build_bridge_error(ConnectionError("lost"), task_id="abc")
        assert result["error"]["details"]["task_id"] == "abc"


class TestBuildOperationError:
    def test_basic(self):
        result = build_operation_error("not_found", "Task not found", task_id="t1")
        assert result["ok"] is False
        assert result["error"]["code"] == "not_found"
        assert result["error"]["details"]["task_id"] == "t1"

    def test_with_action(self):
        result = build_operation_error("err", "msg", action="retry")
        assert result["error"]["details"]["action"] == "retry"

    def test_no_details(self):
        result = build_operation_error("err", "msg")
        assert result["error"].get("details") is None
