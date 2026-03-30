"""YADE Python API Query Tool - Keyword search for API documentation."""

from typing import Any

from fastmcp import FastMCP

from yade_mcp.contracts import build_docs_data, build_ok
from yade_mcp.knowledge.search import APISearch
from yade_mcp.utils import PythonAPISearchQuery, SearchLimit


def register(mcp: FastMCP) -> None:
    """Register yade_query_api tool."""

    @mcp.tool()
    def yade_query_api(
        query: PythonAPISearchQuery,
        limit: SearchLimit = 10,
    ) -> dict[str, Any]:
        """Search YADE API documentation by keywords (like grep).

        Returns matching class/function names with descriptions ranked by relevance.
        Use yade_browse_api for full documentation of a specific class.

        When to use:
        - You have keywords but don't know the exact class name
        - Examples: "friction material", "gravity engine", "contact force",
          "triaxial stress", "sphere create", "hertz mindlin"

        Related tools:
        - yade_browse_api: Get full documentation for a known class path
        """
        matches = APISearch.search(query, top_k=limit)

        payload = build_docs_data(
            source="python_api",
            action="query",
            entries=matches,
            summary={"count": len(matches), "query": query},
        )

        return build_ok(payload)
