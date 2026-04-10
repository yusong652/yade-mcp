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
        # Output twice the cap so Step 1 (string-field truncation) must kick in.
        result = await build_ok({"output": "line\n" * (MAX_RESPONSE_CHARS // 2)})
        assert "truncated" in result["data"]["output"].lower()

    @pytest.mark.asyncio
    async def test_extremely_large_response(self):
        huge = {"field_" + str(i): "x" * 5000 for i in range(100)}
        result = await build_ok(huge)
        serialized = json.dumps(result, ensure_ascii=False)
        # Non-docs-shaped data falls through to Step 4 (last-resort fallback),
        # which keeps a `_truncated: true` marker.
        assert "_truncated" in serialized or len(serialized) < MAX_RESPONSE_CHARS * 3

    @pytest.mark.asyncio
    async def test_docs_listing_progressive_field_stripping(self):
        """Docs-shaped responses with many small entries must strip verbose
        fields (description first) rather than collapse into a blank blob."""
        # Size entries so: WITH description → exceeds cap; WITHOUT → fits.
        # Per entry ≈ 130 chars for base fields + 200 chars for description filler.
        # 700 × 330 ≈ 231k (> cap), 700 × 130 ≈ 91k (< cap).
        n = max(700, MAX_RESPONSE_CHARS // 200)
        entries = [
            {
                "entry_type": "class",
                "name": f"Class{i:06d}",
                "path": f"engines.Class{i:06d}",
                "description": "x" * 200,
                "has_docs": True,
            }
            for i in range(n)
        ]
        data = build_docs_data(
            source="python_api",
            action="browse",
            entries=entries,
            summary={"count": 500, "category": "engines"},
        )
        result = await build_ok(data, inject_context=False)

        returned = result["data"]["entries"]
        assert len(returned) == n, "all entries must be preserved"
        assert all("description" not in e for e in returned), (
            "description should be stripped first when over cap"
        )
        # Name and path survive — agents can still drill into specific classes.
        assert all(e.get("name", "").startswith("Class") for e in returned)
        assert all(e.get("path", "").startswith("engines.") for e in returned)

        serialized = json.dumps(result, ensure_ascii=False)
        assert len(serialized) <= MAX_RESPONSE_CHARS

    @pytest.mark.asyncio
    async def test_docs_listing_compact_mode_fallback(self):
        """When Step 2 (field stripping) cannot bring the response under cap,
        collapse to a names-only list in ``summary.names``. Full entries must
        overflow the cap, but the name-only form must still fit — otherwise
        we land in Step 4 (last-resort), not Step 3 (compact)."""
        # Rough per-entry sizes (JSON-serialized, including overhead):
        #   full (with description):        ~240 chars
        #   stripped to name+path only:     ~125 chars
        #   compact (name in summary.names): ~65 chars
        # Need: N × 125 > cap (Step 2 exhausts) AND N × 65 < cap (Step 3 fits).
        # N = cap // 100 sits safely in both bands across a wide cap range.
        n = MAX_RESPONSE_CHARS // 100
        entries = [
            {
                "entry_type": "class",
                "name": f"VeryLongClassNameNumber{i:06d}" * 2,
                "path": f"engines.VeryLongClassNameNumber{i:06d}",
                "description": "y" * 80,
            }
            for i in range(n)
        ]
        data = build_docs_data(
            source="python_api",
            action="browse",
            entries=entries,
            summary={"count": n, "category": "engines"},
        )
        result = await build_ok(data, inject_context=False)

        summary = result["data"].get("summary", {})
        assert summary.get("compact_mode") is True, "compact_mode flag expected"
        assert "names" in summary and isinstance(summary["names"], list)
        assert len(summary["names"]) == n, "every name preserved in compact mode"
        assert result["data"]["entries"] == [], "entries emptied in compact mode"

    @pytest.mark.asyncio
    async def test_check_task_status_style_giant_line_caught(self):
        """Defense-in-depth: a check_task_status-shaped payload where the
        `output` field is a single pathologically long line (e.g. the user
        printed a huge JSON dump on one line) must be caught by Step 1's
        string-field truncation rather than falling through to the
        last-resort blob."""
        payload = {
            "task_id": "t1",
            "task_status": "running",
            "elapsed_time": 42.0,
            "output": "A" * (MAX_RESPONSE_CHARS * 3),  # 3x cap on one line
            "pagination": {"total_lines": 1, "line_range": "1-1",
                           "has_older": False, "has_newer": False},
        }
        result = await build_ok(payload)
        output = result["data"]["output"]
        assert "truncated" in output.lower()
        # Sibling fields survived — Step 1 only touched the long string.
        assert result["data"]["task_id"] == "t1"
        assert result["data"]["pagination"]["total_lines"] == 1
        # Whole envelope now fits under the cap.
        serialized = json.dumps(result, ensure_ascii=False)
        assert len(serialized) <= MAX_RESPONSE_CHARS

    @pytest.mark.asyncio
    async def test_docs_listing_small_unchanged(self):
        """Small docs listings pass through untouched — no fields stripped."""
        entries = [
            {
                "entry_type": "class",
                "name": "Sphere",
                "path": "shapes.Sphere",
                "description": "A spherical body shape",
                "has_docs": True,
            }
        ]
        result = await build_ok(
            build_docs_data(source="python_api", action="browse", entries=entries),
            inject_context=False,
        )
        returned = result["data"]["entries"]
        assert len(returned) == 1
        assert returned[0]["description"] == "A spherical body shape"
        assert returned[0]["has_docs"] is True
