"""Tests for response envelope contracts."""

import json

from yade_mcp.contracts import (
    MAX_RESPONSE_CHARS,
    build_docs_data,
    build_error,
    build_ok,
)


class TestBuildOk:
    def test_basic(self):
        result = build_ok({"key": "value"})
        assert result["ok"] is True
        assert result["data"]["key"] == "value"
        assert "error" not in result

    def test_none_data(self):
        result = build_ok(None)
        assert result["ok"] is True

    def test_enforces_size_limit(self):
        big_data = {"output": "x" * (MAX_RESPONSE_CHARS * 2)}
        result = build_ok(big_data)
        serialized = json.dumps(result, ensure_ascii=False)
        assert len(serialized) <= MAX_RESPONSE_CHARS

    def test_does_not_inject_context(self):
        """Context injection must happen at the tool boundary
        (``with_context`` decorator), not inside the contract builder."""
        result = build_ok({"key": "value"})
        assert "_context" not in result


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

    def test_does_not_inject_context(self):
        result = build_error("err", "msg")
        assert "_context" not in result


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
    def test_small_response_unchanged(self):
        result = build_ok({"msg": "hello"})
        assert result["data"]["msg"] == "hello"

    def test_large_string_truncated(self):
        # Output twice the cap so Step 1 (string-field truncation) must kick in.
        result = build_ok({"output": "line\n" * (MAX_RESPONSE_CHARS // 2)})
        assert "truncated" in result["data"]["output"].lower()

    def test_extremely_large_response(self):
        huge = {"field_" + str(i): "x" * 5000 for i in range(100)}
        result = build_ok(huge)
        serialized = json.dumps(result, ensure_ascii=False)
        # Non-docs-shaped data falls through to Step 4 (last-resort fallback),
        # which keeps a `_truncated: true` marker.
        assert "_truncated" in serialized or len(serialized) < MAX_RESPONSE_CHARS * 3

    def test_docs_listing_progressive_field_stripping(self):
        """Docs-shaped responses with many small entries must strip verbose
        fields (description first) rather than collapse into a blank blob."""
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
        result = build_ok(data)

        returned = result["data"]["entries"]
        assert len(returned) == n, "all entries must be preserved"
        assert all("description" not in e for e in returned), "description should be stripped first when over cap"
        assert all(e.get("name", "").startswith("Class") for e in returned)
        assert all(e.get("path", "").startswith("engines.") for e in returned)

        serialized = json.dumps(result, ensure_ascii=False)
        assert len(serialized) <= MAX_RESPONSE_CHARS

    def test_docs_listing_compact_mode_fallback(self):
        """When Step 2 (field stripping) cannot bring the response under cap,
        collapse to a names-only list in ``summary.names``."""
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
        result = build_ok(data)

        summary = result["data"].get("summary", {})
        assert summary.get("compact_mode") is True, "compact_mode flag expected"
        assert "names" in summary and isinstance(summary["names"], list)
        assert len(summary["names"]) == n, "every name preserved in compact mode"
        assert result["data"]["entries"] == [], "entries emptied in compact mode"

    def test_check_task_status_style_giant_line_caught(self):
        """Defense-in-depth: a check_task_status-shaped payload where the
        `output` field is a single pathologically long line must be caught by
        Step 1's string-field truncation rather than falling through to the
        last-resort blob."""
        payload = {
            "task_id": "t1",
            "task_status": "running",
            "elapsed_time": 42.0,
            "output": "A" * (MAX_RESPONSE_CHARS * 3),
            "pagination": {"total_lines": 1, "line_range": "1-1", "has_older": False, "has_newer": False},
        }
        result = build_ok(payload)
        output = result["data"]["output"]
        assert "truncated" in output.lower()
        assert result["data"]["task_id"] == "t1"
        assert result["data"]["pagination"]["total_lines"] == 1
        serialized = json.dumps(result, ensure_ascii=False)
        assert len(serialized) <= MAX_RESPONSE_CHARS

    def test_docs_listing_small_unchanged(self):
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
        result = build_ok(build_docs_data(source="python_api", action="browse", entries=entries))
        returned = result["data"]["entries"]
        assert len(returned) == 1
        assert returned[0]["description"] == "A spherical body shape"
        assert returned[0]["has_docs"] is True
