"""Tests for bridge handler helpers."""

from yade_mcp_bridge.handlers.helpers import requireField, truncateMessage


class TestRequireField:
    def test_valid_field(self):
        value, err = requireField({"code": "print(1)"}, "code", "req-1")
        assert value == "print(1)"
        assert err is None

    def test_missing_field(self):
        value, err = requireField({}, "code", "req-1")
        assert value is None
        assert err["ok"] is False
        assert err["error"]["code"] == "missing_field"
        assert err["error"]["details"]["field"] == "code"
        assert "code required" in err["error"]["message"]
        assert err["request_id"] == "req-1"

    def test_empty_string_field(self):
        value, err = requireField({"code": ""}, "code", "req-1")
        assert value is None
        assert err is not None

    def test_custom_response_type(self):
        _, err = requireField({}, "task_id", "req-1", "execute_code_result")
        assert err["type"] == "execute_code_result"


class TestTruncateMessage:
    def test_short_message_unchanged(self):
        msg = "hello"
        assert truncateMessage(msg) == msg

    def test_exact_limit_unchanged(self):
        msg = "x" * 5000
        assert truncateMessage(msg) == msg

    def test_over_limit_truncated(self):
        msg = "x" * 6000
        result = truncateMessage(msg)
        assert len(result) < len(msg)
        assert "truncated from 6000 chars" in result

    def test_custom_limit(self):
        msg = "x" * 200
        result = truncateMessage(msg, maxLength=100)
        assert "truncated from 200 chars" in result
