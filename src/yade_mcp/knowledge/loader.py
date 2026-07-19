"""YADE API documentation loader — cached JSON docs organised as a YADE-native class tree."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

# Class docs live flat at {category}/{class_name}.json; the tree structure
# (YADE's class hierarchy) is metadata in index.json, not nested directories.
DOCS_ROOT = Path(__file__).parent / "resources" / "python_api_docs"

# Maximum description length shown in browse listings (full docstrings
# come from load_class, not list_node_contents).
_LISTING_DESCRIPTION_CAP = 120


class APILoader:
    """Loads and caches YADE Python API documentation."""

    @staticmethod
    @lru_cache(maxsize=1)
    def load_index() -> dict[str, Any]:
        """Load the master index file with category descriptions and trees."""
        index_path = DOCS_ROOT / "index.json"
        if not index_path.exists():
            return {"categories": {}}
        with open(index_path, encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            return result

    # ------------------------------------------------------------------
    # Category-level access
    # ------------------------------------------------------------------

    @staticmethod
    def list_categories() -> list[dict[str, Any]]:
        """List top-level categories with descriptions, class counts, and top tree nodes."""
        index = APILoader.load_index()
        categories = index.get("categories", {})
        result = []
        for name, info in categories.items():
            tree = info.get("tree") or {}
            entry: dict[str, Any] = {
                "name": name,
                "description": info.get("description", ""),
                "class_count": len(info.get("classes", [])),
            }
            top_nodes = list(tree.get("children", {}).keys())
            direct_leaves = list(tree.get("direct_classes", []))
            if top_nodes:
                entry["top_level_nodes"] = top_nodes
            if direct_leaves:
                entry["direct_leaves"] = len(direct_leaves)
            result.append(entry)
        return result

    # ------------------------------------------------------------------
    # Tree navigation
    # ------------------------------------------------------------------

    @staticmethod
    def get_tree_node(category: str, tree_path: list[str]) -> dict[str, Any] | None:
        """Walk the category tree to ``tree_path`` (empty = category root); None if missing."""
        index = APILoader.load_index()
        categories = index.get("categories", {})
        cat_info = categories.get(category)
        if cat_info is None:
            return None
        node = cat_info.get("tree") or {"direct_classes": [], "children": {}}
        for segment in tree_path:
            children = node.get("children") or {}
            if segment not in children:
                return None
            node = children[segment]
        return node

    @staticmethod
    def _count_descendants(node: dict[str, Any]) -> int:
        """Total classes reachable from this node (direct + recursive)."""
        total = len(node.get("direct_classes", []))
        for child in (node.get("children") or {}).values():
            total += APILoader._count_descendants(child)
        return total

    @staticmethod
    def list_node_contents(category: str, tree_path: list[str]) -> dict[str, Any] | None:
        """Return a tree node's contents: ``self_class`` plus a flat ``entries`` list of children and leaves."""
        node = APILoader.get_tree_node(category, tree_path)
        if node is None:
            return None

        # Every internal tree node is itself a documented class; surface its
        # own docs as self_class (None at the category root).
        self_class: dict[str, Any] | None = None
        if tree_path:
            self_class = APILoader.build_class_entry(category, tree_path[-1])

        # Merge sub-nodes and direct leaves into one unified list.
        entries: list[dict[str, Any]] = []
        children = node.get("children") or {}
        seen: set[str] = set()

        for child_name in children:
            child_node = children[child_name]
            entry = APILoader.build_class_entry(category, child_name)
            if entry is None:
                # Node name is not a documented class (e.g. scraper artifact).
                entry = {
                    "name": child_name,
                    "description": "",
                    "has_docs": False,
                }
            entry["descendant_count"] = APILoader._count_descendants(child_node)
            entry["has_children"] = True
            entries.append(entry)
            seen.add(child_name)

        for cls_name in node.get("direct_classes", []):
            if cls_name in seen:
                continue
            entry = APILoader.build_class_entry(category, cls_name)
            if entry is not None:
                entries.append(entry)

        return {
            "self_class": self_class,
            "entries": entries,
        }

    # ------------------------------------------------------------------
    # Class-level access
    # ------------------------------------------------------------------

    @staticmethod
    def load_class(category: str, class_name: str) -> dict[str, Any] | None:
        """Load the full class documentation JSON, or None if undocumented."""
        doc_path = DOCS_ROOT / category / f"{class_name}.json"
        if not doc_path.exists():
            return None
        with open(doc_path, encoding="utf-8") as f:
            result: dict[str, Any] = json.load(f)
            return result

    @staticmethod
    def build_class_entry(category: str, class_name: str) -> dict[str, Any] | None:
        """Build a compact browse-listing entry: name plus cheap metadata (description, parent, counts)."""
        doc = APILoader.load_class(category, class_name)
        entry: dict[str, Any] = {
            "name": class_name,
            "description": (doc.get("description", "") or "")[:_LISTING_DESCRIPTION_CAP] if doc else "",
            "has_docs": doc is not None,
        }
        if doc:
            raw_parent = doc.get("parent") or ""
            # "instance" marks the Boost.Python wrapper base — semantically
            # meaningless to agents, normalise to absent.
            if raw_parent and raw_parent != "instance":
                entry["parent"] = raw_parent
            attrs = doc.get("attributes")
            if isinstance(attrs, list):
                entry["attribute_count"] = len(attrs)
            methods = doc.get("methods")
            if isinstance(methods, list):
                entry["method_count"] = len(methods)
        return entry

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _find_class_path_segments(
        node: dict[str, Any],
        class_name: str,
        prefix: list[str] | None = None,
    ) -> list[str] | None:
        """Return path segments from ``node`` down to ``class_name``, or None."""
        current = prefix or []

        if class_name in (node.get("direct_classes") or []):
            return current + [class_name]

        for child_name, child_node in (node.get("children") or {}).items():
            if child_name == class_name:
                return current + [child_name]
            found = APILoader._find_class_path_segments(
                child_node,
                class_name,
                current + [child_name],
            )
            if found is not None:
                return found

        return None

    @staticmethod
    def build_browse_path(category: str, class_name: str) -> str | None:
        """Return a valid ``yade_browse_api`` dotted path for a documented class."""
        cat_info = APILoader.load_index().get("categories", {}).get(category)
        if cat_info is None:
            return None

        tree = cat_info.get("tree") or {"direct_classes": [], "children": {}}
        segments = APILoader._find_class_path_segments(tree, class_name)
        if segments is None:
            return None
        return ".".join([category, *segments])

    @staticmethod
    def resolve_path(path: str) -> dict[str, Any]:
        """Parse a dotted path into a target with level root | tree_node | class | error."""
        if not path:
            return {"level": "root"}

        segments = path.split(".")
        category = segments[0]
        index = APILoader.load_index()
        categories = index.get("categories", {})

        if category not in categories:
            return {
                "level": "error",
                "error": f"Category not found: {category}",
                "available": list(categories.keys()),
            }

        # Walk remaining segments through the tree. No class-name fallback:
        # a leaf is only reachable via its full parent chain.
        rest = segments[1:]
        node = categories[category].get("tree") or {"direct_classes": [], "children": {}}
        walked: list[str] = []

        for i, segment in enumerate(rest):
            children = node.get("children") or {}
            direct_classes = node.get("direct_classes") or []

            if segment in children:
                node = children[segment]
                walked.append(segment)
                continue

            if segment in direct_classes:
                # Must be the last segment — classes are leaves.
                if i != len(rest) - 1:
                    return {
                        "level": "error",
                        "error": f"'{segment}' is a leaf class; cannot descend further",
                        "category": category,
                        "tree_path": walked,
                    }
                return {
                    "level": "class",
                    "category": category,
                    "tree_path": walked,
                    "class_name": segment,
                }

            # Not found at this node — produce a helpful error.
            return {
                "level": "error",
                "error": f"Path segment not found: {segment}",
                "category": category,
                "tree_path": walked,
                "available_children": list(children.keys()),
                "available_classes": list(direct_classes),
            }

        # Consumed all segments and landed on a tree node.
        return {
            "level": "tree_node",
            "category": category,
            "tree_path": walked,
        }

    # ------------------------------------------------------------------
    # Testing helper
    # ------------------------------------------------------------------

    @staticmethod
    def clear_cache() -> None:
        """Clear cached index (tests/dev only)."""
        APILoader.load_index.cache_clear()
