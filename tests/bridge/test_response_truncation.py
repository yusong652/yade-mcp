"""Tests for bridge response truncation (``YADEBridgeServer._truncate_response``)."""

from yade_mcp_bridge.server import YADEBridgeServer


def _truncate(response):
    return YADEBridgeServer._truncate_response(response)


class TestTruncateResponse:
    def test_small_response_unchanged(self):
        response = {"status": "ok", "data": {"output": "hello"}}
        result = _truncate(response)
        assert result["data"]["output"] == "hello"

    def test_large_output_truncated(self):
        big_output = "x" * 50000
        response = {"status": "ok", "data": {"output": big_output}}
        out = _truncate(response)["data"]["output"]
        assert len(out) < len(big_output)
        assert out.startswith("... [TRUNCATED: ")
        assert "earlier chars omitted" in out

    def test_keeps_tail_not_head(self):
        big_output = "head_marker\n" + "x" * 50000 + "\ntail_marker"
        out = _truncate({"data": {"output": big_output}})["data"]["output"]
        assert out.endswith("tail_marker")
        assert "head_marker" not in out

    def test_tail_aligned_to_line_boundary(self):
        big_output = "\n".join(f"line-{i:05d}" for i in range(2000))
        out = _truncate({"data": {"output": big_output}})["data"]["output"]
        kept = out.split("]\n", 1)[1]
        assert kept.startswith("line-")
        assert len(kept.split("\n", 1)[0]) == len("line-00000")

    def test_no_data_key(self):
        response = {"status": "ok", "message": "no data"}
        result = _truncate(response)
        assert result == response

    def test_no_output_key(self):
        response = {"status": "ok", "data": {"result": 42}}
        result = _truncate(response)
        assert result["data"]["result"] == 42

    def test_short_output_not_truncated(self):
        response = {"status": "ok", "data": {"output": "short"}}
        out = _truncate(response)["data"]["output"]
        assert out == "short"
        assert "[TRUNCATED" not in out
