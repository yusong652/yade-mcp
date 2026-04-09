"""Tests for response envelope contracts."""

import json

import pytest

from yade_mcp.contracts import (
    MAX_RESPONSE_CHARS,
    build_docs_data,
    build_error,
    build_ok,
)


class TestBuildOk:
    @pytest.mark.asyncio
    async def test_basic(self):
        result = await build_ok({"key": "value"})
        assert result["ok"] is True
        assert result["data"]["key"] == "value"
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_none_data(self):
        result = await build_ok(None)
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_enforces_size_limit(self):
        big_data = {"output": "x" * (MAX_RESPONSE_CHARS * 2)}
        result = await build_ok(big_data)
        serialized = json.dumps(result, ensure_ascii=False)
        assert len(result["data"]["output"]) < MAX_RESPONSE_CHARS * 2


class TestBuildError:
    def test_basic(self):
        result = build_error("not_found", "Resource not found")
        assert result["ok"] is False
        assert result["error"]["code"] == "not_found"
        assert result["error"]["message"] == "Resource not found"

    def test_with_details(self):
        result = build_error("err", "msg", {"task_id": "t1"})
        assert result["error"]["details"]["task_id"] == "t1"

    def test_with_data(self):
        result = build_error("err", "msg", data={"partial": True})
        assert result["data"]["partial"] is True


class TestBuildDocsData:
    def test_basic(self):
        result = build_docs_data(
            source="python_api",
            action="browse",
            entries=[{"name": "Sphere"}],
        )
        assert result["source"] == "python_api"
        assert result["action"] == "browse"
        assert len(result["entries"]) == 1

    def test_with_summary(self):
        result = build_docs_data(
            source="reference",
            action="query",
            entries=[],
            summary={"total": 0},
        )
        assert result["summary"]["total"] == 0


class TestSizeEnforcement:
    @pytest.mark.asyncio
    async def test_small_response_unchanged(self):
        result = await build_ok({"msg": "hello"})
        assert result["data"]["msg"] == "hello"

    @pytest.mark.asyncio
    async def test_large_string_truncated(self):
        result = await build_ok({"output": "line\n" * 5000})
        assert "truncated" in result["data"]["output"].lower()

    @pytest.mark.asyncio
    async def test_extremely_large_response(self):
        huge = {"field_" + str(i): "x" * 5000 for i in range(100)}
        result = await build_ok(huge)
        serialized = json.dumps(result, ensure_ascii=False)
        assert "_truncated" in serialized or len(serialized) < MAX_RESPONSE_CHARS * 3
