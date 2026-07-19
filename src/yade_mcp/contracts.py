"""Unified tool response envelope contracts with response size enforcement."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# Serialized response cap: 2^17 chars ≈ 32k English tokens, a tokenizer-free
# proxy sized so the largest docs listing and max-pagination task output fit.
MAX_RESPONSE_CHARS = 131_072

# Stripped from docs entries in this order (most verbose first) when a
# response exceeds the cap; names are always preserved.
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
    """Recursively truncate long string values to fit within budget."""
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
    """Shrink an over-cap envelope progressively, preserving as much signal as possible."""
    serialized = json.dumps(envelope, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return envelope

    # Step 1: truncate long string fields (output, doc, description text).
    per_field_budget = max_chars // 2
    envelope["data"] = _truncate_strings_in_data(envelope.get("data"), per_field_budget)
    serialized = json.dumps(envelope, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return envelope

    # Step 2: progressive field stripping on docs-shaped entries.
    entries = _entries_of(envelope.get("data"))
    if entries is not None:
        for field in _STRIPPABLE_ENTRY_FIELDS:
            _strip_entry_field(entries, field)
            serialized = json.dumps(envelope, ensure_ascii=False)
            if len(serialized) <= max_chars:
                return envelope

        # Step 3: name-only fallback; `entries` stays an empty list so the
        # DocsData contract is preserved.
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
    """Build, validate, and size-enforce a success envelope."""
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
