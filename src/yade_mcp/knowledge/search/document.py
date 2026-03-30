"""Unified document model for YADE search system."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchDocument:
    """A searchable document with multi-field content."""

    name: str
    description: str
    keywords: list[str]
    category: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.keywords = [k.lower() for k in self.keywords]

    def matches_filters(self, filters: dict[str, Any]) -> bool:
        for key, value in filters.items():
            if key == "category" and self.category != value:
                return False
        return True
