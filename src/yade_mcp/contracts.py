"""Unified tool response envelope contracts.

All tool business payloads should be wrapped by this module so response
shapes stay consistent across documentation and execution tools.

Includes automatic response size enforcement to prevent context window
exhaustion in LLM clients.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# Maximum serialized response size in characters. This is a defensive safety
# rail, not a precise token budget — character count is an imperfect proxy
# (English ≈ 4 chars/token, Chinese ≈ 1-2, JSON structure ≈ 5-6), but it is
# zero-cost, deterministic, and does not require a tokenizer (which would tie
# the MCP server to a specific LLM vendor).
#
# 131_072 = 2^17 chars ≈ 32k tokens in English, ~16% of a 200k context window.
# Chosen so that:
#   - The biggest existing docs listing (engines, 263 classes) fits with full
#     descriptions AND future field enrichment (baseClass, attribute counts,
#     etc.) without forcing compact-mode fallback.
#   - check_task_status at max pagination (200 lines) tolerates verbose
#     scientific print output (~640 chars/line average) without hitting Step 1
#     string truncation.
#
# Every tool goes through this cap via build_ok() — it is the project's
# bottom-line defense against runaway responses. See _enforce_size() for the
# progressive fallback strategy when a response exceeds the cap.
MAX_RESPONSE_CHARS = 131_072

# Fields stripped progressively from docs entries when responses exceed the
# cap. Order matters: most verbose first. Names/paths are always preserved.
_STRIPPABLE_ENTRY_FIELDS = ("description", "doc", "has_docs", "entry_type")


class ToolError(BaseModel):
    """Structured business error for tool payloads."""

    code: str = Field(description="Stable machine-readable error code")
    message: str = Field(description="Human-readable error summary")
    details: dict[str, Any] | None = Field(default=None, description="Optional structured error details")


class ToolEnvelope(BaseModel):
    """Unified response shape for all tool business results."""

    ok: bool = Field(description="Business-level success flag")
    data: Any | None = Field(default=None, description="Tool-specific payload")
    error: ToolError | None = Field(default=None, description="Structured error payload")

    @model_validator(mode="after")
    def _validate_coherence(self) -> ToolEnvelope:
        if self.ok and self.error is not None:
            raise ValueError("ok=true responses must not include error")
        if not self.ok and self.error is None:
            raise ValueError("ok=false responses must include error")
        return self


class DocsData(BaseModel):
    """Unified inner `data` schema for documentation tools."""

    source: Literal["python_api", "reference"]
    action: Literal["browse", "query"]
    entries: list[dict[str, Any]]
    summary: dict[str, Any] = Field(default_factory=dict)


def _truncate_strings_in_data(data: Any, budget: int) -> Any:
    """Recursively truncate long string values to fit within budget.

    Targets the most common oversized fields: 'output', 'doc', 'description'.
    """
    if isinstance(data, str):
        if len(data) > budget:
            cut = data[:budget].rsplit("\n", 1)[0]
            return cut + f"\n... (truncated, {len(data)} total chars. Use pagination or filter to see more.)"
        return data
    if isinstance(data, dict):
        return {k: _truncate_strings_in_data(v, budget) for k, v in data.items()}
    if isinstance(data, list):
        return [_truncate_strings_in_data(item, budget) for item in data]
    return data


def _entries_of(data: Any) -> list[dict[str, Any]] | None:
    """Return the ``entries`` list from a docs-shaped data dict, or None."""
    if not isinstance(data, dict):
        return None
    entries = data.get("entries")
    if isinstance(entries, list) and all(isinstance(e, dict) for e in entries):
        return entries
    return None


def _strip_entry_field(entries: list[dict[str, Any]], field: str) -> None:
    """Remove ``field`` from every entry in place (no-op if absent)."""
    for entry in entries:
        entry.pop(field, None)


def _enforce_size(envelope: dict[str, Any], max_chars: int = MAX_RESPONSE_CHARS) -> dict[str, Any]:
    """Enforce response size limit with progressive, information-preserving fallback.

    Strategy (stops as soon as the envelope fits under ``max_chars``):

    1. Return as-is if already under cap.
    2. Truncate long string fields in-place (handles bloated ``output`` / ``doc``).
    3. For docs-shaped data (``entries: list[dict]``), strip verbose per-entry
       fields one at a time in priority order: description → doc → has_docs →
       entry_type. This keeps all entries but trims their verbosity.
    4. Collapse entries to a compact name-only array under ``summary.names``
       and empty the ``entries`` list. Callers can still see *what* exists,
       just not the per-entry metadata.
    5. Last resort: surface a structured error without wiping identifying
       fields (source/action stay).

    The old behavior — wiping data to a ``{"_truncated": True}`` blob with
    zero content — is gone. Every fallback tier preserves as much signal as
    possible so agents can always recover and drill deeper.
    """
    serialized = json.dumps(envelope, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return envelope

    # Step 1: truncate long string fields (output, doc, description text)
    per_field_budget = max_chars // 2
    envelope["data"] = _truncate_strings_in_data(envelope.get("data"), per_field_budget)
    serialized = json.dumps(envelope, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return envelope

    # Step 2: progressive field stripping on docs-shaped entries
    entries = _entries_of(envelope.get("data"))
    if entries is not None:
        for field in _STRIPPABLE_ENTRY_FIELDS:
            _strip_entry_field(entries, field)
            serialized = json.dumps(envelope, ensure_ascii=False)
            if len(serialized) <= max_chars:
                return envelope

        # Step 3: compact name-only fallback. Entries become a thin list of
        # names inside summary; `entries` stays an empty list so the DocsData
        # contract is preserved.
        data = envelope["data"]
        names = [e.get("name") for e in entries if e.get("name")]
        data["entries"] = []
        summary = data.setdefault("summary", {})
        summary["names"] = names
        summary["compact_mode"] = True
        summary["compact_reason"] = (
            f"Response exceeded {max_chars}-char cap; only names are listed. Browse a specific name for full details."
        )
        serialized = json.dumps(envelope, ensure_ascii=False)
        if len(serialized) <= max_chars:
            return envelope

    # Step 4: last resort — preserve identifying fields only.
    data_obj = envelope.get("data")
    data_dict: dict[str, Any] = data_obj if isinstance(data_obj, dict) else {}
    envelope["data"] = {
        "source": data_dict.get("source"),
        "action": data_dict.get("action"),
        "_truncated": True,
        "_message": (
            f"Response exceeds cap ({len(serialized)} chars) even after compact mode. "
            "Use a more specific query or path."
        ),
        "_original_size": len(serialized),
    }
    return envelope


def build_ok(data: Any) -> dict[str, Any]:
    """Build, validate, and size-enforce a success envelope.

    Pure contract constructor — shapes the ``{ok, data}`` payload and
    enforces size limits. Runtime context (``_context``) is injected
    outside this module by the tool-layer ``with_context`` decorator;
    keeping that concern external lets contracts stay sync and I/O-free.
    """
    envelope = ToolEnvelope(ok=True, data=data).model_dump(exclude_none=True)
    return _enforce_size(envelope)


def build_error(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    data: Any | None = None,
) -> dict[str, Any]:
    """Build and validate an error envelope."""
    return ToolEnvelope(
        ok=False,
        data=data,
        error=ToolError(code=code, message=message, details=details),
    ).model_dump(exclude_none=True)


def build_docs_data(
    *,
    source: Literal["python_api", "reference"],
    action: Literal["browse", "query"],
    entries: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate documentation tool `data` payloads."""
    return DocsData(
        source=source,
        action=action,
        entries=entries,
        summary=summary or {},
    ).model_dump(exclude_none=True)
