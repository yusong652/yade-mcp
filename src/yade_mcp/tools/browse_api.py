"""YADE Python API browse tool — walks the YADE-native class tree."""

from typing import Any

from fastmcp import FastMCP
from pydantic import Field

from yade_mcp.contracts import build_docs_data, build_error, build_ok
from yade_mcp.knowledge.loader import APILoader


def register(mcp: FastMCP) -> None:
    """Register yade_browse_api tool."""

    @mcp.tool()
    async def yade_browse_api(
        path: str | None = Field(
            None,
            description=(
                "Dot-separated path into YADE's class hierarchy. "
                "Navigation is tree-driven (no class-name shortcuts).\n"
                "- None or '': list top-level categories (engine / functor / "
                "material / shape / ...)\n"
                "- 'engine': list direct sub-trees of Engine (Dispatcher, "
                "GlobalEngine, PartialEngine, ...)\n"
                "- 'engine.GlobalEngine': list GlobalEngine's sub-trees "
                "(BoundaryController, Collider, PeriodicEngine, ...) plus its "
                "direct leaf classes (NewtonIntegrator, InteractionLoop, ...)\n"
                "- 'engine.GlobalEngine.NewtonIntegrator': full docs for "
                "NewtonIntegrator\n"
                "- 'functor.LawFunctor.Law2_ScGeom_FrictPhys_CundallStrack': "
                "full docs for a contact law functor\n"
                "Every leaf is reached via its parent-class chain."
            ),
        ),
    ) -> dict[str, Any]:
        """Browse YADE's Python API as a YADE-native class tree.

        The tree is rooted in YADE's real inheritance hierarchy: paths
        mirror the class's __mro__ up to its category root. No
        shortcuts — always drill through parents.
        """
        normalized = (path or "").strip()
        resolved = APILoader.resolve_path(normalized)
        level = resolved["level"]

        if level == "error":
            details = {
                k: v
                for k, v in resolved.items()
                if k in ("category", "tree_path", "available_children", "available_classes", "available")
            }
            return build_error(
                code="invalid_path",
                message=resolved["error"],
                details=details or None,
            )

        if level == "root":
            return await _browse_root()

        if level == "tree_node":
            return await _browse_tree_node(resolved["category"], resolved["tree_path"])

        if level == "class":
            return await _browse_class(
                resolved["category"],
                resolved["tree_path"],
                resolved["class_name"],
            )

        return build_error("unknown_level", f"Unknown path level: {level}")


async def _browse_root() -> dict[str, Any]:
    """List every top-level category."""
    categories = APILoader.list_categories()
    entries = [{"entry_type": "category", **cat} for cat in categories]
    return await build_ok(
        build_docs_data(
            source="python_api",
            action="browse",
            entries=entries,
            summary={
                "count": len(entries),
                "hint": "Browse a category (e.g. 'engine') to drill into its tree",
            },
        ),
        inject_context=False,
    )


async def _browse_tree_node(category: str, tree_path: list[str]) -> dict[str, Any]:
    """List a tree node's contents.

    The response merges:
      * ``self_class`` — the node's own class docs (when applicable),
        surfaced as the first entry with ``entry_type: "self"``.
      * child entries — each either a pure leaf class or a sub-tree
        node (marked with ``has_children: true`` + ``descendant_count``).
    """
    contents = APILoader.list_node_contents(category, tree_path)
    if contents is None:
        return build_error(
            "tree_node_not_found",
            f"Tree node not found: {category}{'.' + '.'.join(tree_path) if tree_path else ''}",
            details={"category": category, "tree_path": tree_path},
        )

    display_path = category + ("." + ".".join(tree_path) if tree_path else "")
    entries: list[dict[str, Any]] = []

    # Self class as the first entry, so the browse response is "here is the
    # node itself, and here is what hangs under it".
    self_class = contents.get("self_class")
    if self_class is not None:
        self_entry: dict[str, Any] = {
            "entry_type": "self",
            "name": self_class["name"],
            "path": display_path,
            "description": self_class.get("description", ""),
            "has_docs": self_class.get("has_docs", False),
        }
        for field in ("parent", "attribute_count", "method_count"):
            if field in self_class:
                self_entry[field] = self_class[field]
        entries.append(self_entry)

    children_count = 0
    leaf_count = 0
    for cls in contents["entries"]:
        is_subtree = cls.get("has_children", False)
        if is_subtree:
            children_count += 1
        else:
            leaf_count += 1
        entry: dict[str, Any] = {
            "entry_type": "tree_node" if is_subtree else "class",
            "name": cls["name"],
            "path": f"{display_path}.{cls['name']}",
            "description": cls.get("description", ""),
            "has_docs": cls.get("has_docs", False),
        }
        for field in ("parent", "attribute_count", "method_count"):
            if field in cls:
                entry[field] = cls[field]
        if is_subtree:
            entry["descendant_count"] = cls["descendant_count"]
        entries.append(entry)

    return await build_ok(
        build_docs_data(
            source="python_api",
            action="browse",
            entries=entries,
            summary={
                "count": len(entries),
                "category": category,
                "tree_path": tree_path,
                "display_path": display_path,
                "sub_node_count": children_count,
                "leaf_count": leaf_count,
                "hint": "Drill into a tree_node to see its subtree, or into a class for full docs",
            },
        ),
        inject_context=False,
    )


async def _browse_class(category: str, tree_path: list[str], class_name: str) -> dict[str, Any]:
    """Return the full class documentation JSON."""
    doc = APILoader.load_class(category, class_name)
    if not doc:
        return build_error(
            "class_not_found",
            f"No documentation for '{class_name}' in {category}",
            details={
                "category": category,
                "tree_path": tree_path,
                "class_name": class_name,
            },
        )

    display_path = category + ("." + ".".join(tree_path) if tree_path else "")
    display_path = f"{display_path}.{class_name}"
    return await build_ok(
        build_docs_data(
            source="python_api",
            action="browse",
            entries=[
                {
                    "entry_type": "class_doc",
                    "path": display_path,
                    "doc": doc,
                }
            ],
            summary={"count": 1, "class": class_name, "display_path": display_path},
        ),
        inject_context=False,
    )
