"""YADE Python API Browse Tool - Navigate documentation by path."""

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
                "Dot-separated API path to browse. Progressive disclosure:\n"
                "- None or '': Root — list all categories\n"
                "- 'engines': List all engine classes\n"
                "- 'engines.NewtonIntegrator': Full docs for NewtonIntegrator\n"
                "- 'materials': List all material classes\n"
                "- 'materials.FrictMat': Full docs for FrictMat\n"
                "- 'interactions.geometry': List contact geometry classes\n"
                "- 'interactions.laws.Law2_ScGeom_FrictPhys_CundallStrack': Full docs\n"
                "- 'utils': Utility functions\n"
                "- 'omega': Omega (O) simulation control"
            ),
        ),
    ) -> dict[str, Any]:
        """Browse YADE Python API documentation hierarchically.

        Navigate category → class → details, like a filesystem:
        - Empty path: list categories (ls /)
        - Category: list classes (ls /engines)
        - Category.Class: full documentation (cat /engines/NewtonIntegrator)
        """
        normalized = (path or "").strip()
        resolved = APILoader.resolve_path(normalized)
        level = resolved["level"]

        if level == "error":
            return build_error(
                code="invalid_path",
                message=resolved["error"],
                details={"available": resolved.get("available", [])},
            )

        if level == "root":
            return await _browse_root()

        if level == "category":
            return await _browse_category(resolved["category"])

        if level == "subcategory":
            return await _browse_category(resolved["category"], resolved["subcategory"])

        if level == "class":
            return await _browse_class(
                resolved["category"],
                resolved["class_name"],
                resolved.get("subcategory"),
            )

        return build_error("unknown_level", f"Unknown path level: {level}")


async def _browse_root() -> dict[str, Any]:
    categories = APILoader.list_categories()
    return await build_ok(
        build_docs_data(
            source="python_api",
            action="browse",
            entries=[{"entry_type": "category", **cat} for cat in categories],
            summary={"count": len(categories), "hint": "Browse a category for its classes"},
        )
    )


async def _browse_category(category: str, subcategory: str | None = None) -> dict[str, Any]:
    classes = APILoader.list_classes(category, subcategory)
    if classes is None:
        path = f"{category}.{subcategory}" if subcategory else category
        return build_error(
            "category_not_found",
            f"Category not found: {path}",
        )

    display_path = f"{category}.{subcategory}" if subcategory else category

    entries: list[dict[str, Any]] = []
    if not subcategory:
        index = APILoader.load_index()
        cat_info = index.get("categories", {}).get(category, {})
        subcats = cat_info.get("subcategories", {})
        for subcat_name, subcat_info in subcats.items():
            entries.append(
                {
                    "entry_type": "subcategory",
                    "name": subcat_name,
                    "path": f"{category}.{subcat_name}",
                    "description": subcat_info.get("description", ""),
                }
            )

    for cls in classes:
        entries.append(
            {
                "entry_type": "class",
                "name": cls["name"],
                "path": f"{display_path}.{cls['name']}",
                "description": cls["description"],
                "has_docs": cls["has_docs"],
            }
        )

    return await build_ok(
        build_docs_data(
            source="python_api",
            action="browse",
            entries=entries,
            summary={
                "count": len(entries),
                "category": display_path,
                "hint": "Browse a class for full documentation",
            },
        )
    )


async def _browse_class(category: str, class_name: str, subcategory: str | None = None) -> dict[str, Any]:
    doc = APILoader.load_class(category, class_name, subcategory=subcategory)

    if not doc:
        classes = APILoader.list_classes(category, subcategory) or []
        available = [c["name"] for c in classes]
        path = f"{category}.{subcategory}" if subcategory else category
        return build_error(
            "class_not_found",
            f"No documentation for '{class_name}' in {path}",
            details={"available_classes": available},
        )

    display_path = f"{category}.{subcategory}.{class_name}" if subcategory else f"{category}.{class_name}"

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
            summary={"count": 1, "class": class_name},
        )
    )
