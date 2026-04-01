"""YADE API documentation loader.

Loads and caches documentation from JSON resource files.
Provides hierarchical access: categories → classes → details.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DOCS_ROOT = Path(__file__).parent / "resources" / "python_api_docs"


class APILoader:
    """Loads and caches YADE Python API documentation."""

    @staticmethod
    @lru_cache(maxsize=1)
    def load_index() -> dict[str, Any]:
        """Load the master index file."""
        index_path = DOCS_ROOT / "index.json"
        if not index_path.exists():
            return {"categories": {}}
        with open(index_path, encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            return result

    @staticmethod
    def list_categories() -> list[dict[str, Any]]:
        """List all top-level categories with descriptions."""
        index = APILoader.load_index()
        categories = index.get("categories", {})
        result = []
        for name, info in categories.items():
            entry: dict[str, Any] = {
                "name": name,
                "description": info.get("description", ""),
            }
            if "classes" in info:
                entry["class_count"] = len(info["classes"])
            if "subcategories" in info:
                entry["subcategories"] = list(info["subcategories"].keys())
            result.append(entry)
        return result

    @staticmethod
    def list_classes(category: str, subcategory: str | None = None) -> list[dict[str, Any]] | None:
        """List classes in a category (or subcategory).

        Returns None if category not found.
        """
        index = APILoader.load_index()
        categories = index.get("categories", {})

        cat_info = categories.get(category)
        if cat_info is None:
            return None

        if subcategory:
            subcats = cat_info.get("subcategories", {})
            cat_info = subcats.get(subcategory)
            if cat_info is None:
                return None

        class_names = cat_info.get("classes", [])
        result = []
        for cls_name in class_names:
            # Try to load summary from the class doc file
            doc = APILoader.load_class(category, cls_name, subcategory=subcategory)
            result.append(
                {
                    "name": cls_name,
                    "description": doc.get("description", "")[:120] if doc else "",
                    "has_docs": doc is not None,
                }
            )
        return result

    @staticmethod
    def load_class(category: str, class_name: str, subcategory: str | None = None) -> dict[str, Any] | None:
        """Load full documentation for a specific class.

        Looks for: resources/python_api_docs/{category}/[{subcategory}/]{class_name}.json
        """
        if subcategory:
            doc_path = DOCS_ROOT / category / subcategory / f"{class_name}.json"
        else:
            doc_path = DOCS_ROOT / category / f"{class_name}.json"

        if not doc_path.exists():
            return None

        with open(doc_path, encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            return result

    @staticmethod
    def resolve_path(path: str) -> dict[str, Any]:
        """Resolve a dot-separated browse path to its target.

        Returns a dict with:
          - level: "root", "category", "subcategory", "class"
          - category, subcategory, class_name (as applicable)
          - error (if path is invalid)

        Examples:
          "" → {"level": "root"}
          "engines" → {"level": "category", "category": "engines"}
          "interactions.geometry" → {"level": "subcategory", "category": "interactions", "subcategory": "geometry"}
          "engines.NewtonIntegrator" → {"level": "class", "category": "engines", "class_name": "NewtonIntegrator"}
          "interactions.laws.Law2_ScGeom_FrictPhys_CundallStrack" → {"level": "class", ...}
        """
        if not path:
            return {"level": "root"}

        parts = path.split(".", 1)
        category = parts[0]

        index = APILoader.load_index()
        categories = index.get("categories", {})

        if category not in categories:
            return {
                "level": "error",
                "error": f"Category not found: {category}",
                "available": list(categories.keys()),
            }

        cat_info = categories[category]

        if len(parts) == 1:
            return {"level": "category", "category": category}

        remainder = parts[1]
        subcats = cat_info.get("subcategories", {})

        # Check if remainder starts with a subcategory
        for subcat_name in subcats:
            if remainder == subcat_name:
                return {"level": "subcategory", "category": category, "subcategory": subcat_name}
            if remainder.startswith(subcat_name + "."):
                class_name = remainder[len(subcat_name) + 1 :]
                return {
                    "level": "class",
                    "category": category,
                    "subcategory": subcat_name,
                    "class_name": class_name,
                }

        # No subcategory match — treat remainder as class name
        return {"level": "class", "category": category, "class_name": remainder}

    @staticmethod
    def clear_cache() -> None:
        """Clear cached data (for development/testing)."""
        APILoader.load_index.cache_clear()
