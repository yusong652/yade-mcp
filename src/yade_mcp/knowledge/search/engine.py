"""High-level YADE API search interface using BM25."""

import json
from pathlib import Path
from typing import Any

from yade_mcp.knowledge.loader import APILoader
from yade_mcp.knowledge.search.document import SearchDocument
from yade_mcp.knowledge.search.indexing.bm25_indexer import BM25Indexer
from yade_mcp.knowledge.search.scoring.bm25_scorer import BM25Scorer

DOCS_ROOT = Path(__file__).parent.parent / "resources" / "python_api_docs"


def _load_all_documents() -> list[SearchDocument]:
    """Load all doc JSONs into SearchDocuments for indexing."""
    documents: list[SearchDocument] = []

    for json_path in DOCS_ROOT.rglob("*.json"):
        if json_path.name == "index.json":
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                doc = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        name = doc.get("name", json_path.stem)
        description = doc.get("description", "")
        category = doc.get("category", "")
        parent = doc.get("parent", "")

        # Build keywords from: name, parent, category, attribute names/descriptions, method names
        keywords: list[str] = [name, category, parent]
        for attr in doc.get("attributes", []):
            attr_name = attr.get("name", "")
            if attr_name:
                keywords.append(attr_name)
            attr_desc = attr.get("description", "")
            if attr_desc:
                keywords.append(attr_desc)
        for func in doc.get("functions", []):
            func_name = func.get("name", "")
            if func_name:
                keywords.append(func_name)
        for method in doc.get("methods", []):
            method_name = method.get("name", "")
            if method_name:
                keywords.append(method_name)

        documents.append(
            SearchDocument(
                name=name,
                description=description,
                keywords=keywords,
                category=category,
                metadata={"path": str(json_path.relative_to(DOCS_ROOT)), "parent": parent},
            )
        )

    return documents


class APISearch:
    """YADE API search using BM25 multi-field scoring.

    Builds a fresh index per search for consistency (~50ms for 350 docs).
    """

    @classmethod
    def search(
        cls,
        query: str,
        top_k: int = 10,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search YADE API documentation by keywords; returns top_k results sorted by score."""
        documents = _load_all_documents()

        if category:
            documents = [d for d in documents if d.category == category]

        indexer = BM25Indexer()
        indexer.build(documents)
        scorer = BM25Scorer(indexer)

        scored = scorer.batch_score(query, documents)
        scored.sort(key=lambda x: x[1], reverse=True)

        results: list[dict[str, Any]] = []
        for doc, score, _info in scored[:top_k]:
            category_dir = str(doc.metadata.get("path", "")).split("/", 1)[0]
            results.append(
                {
                    "name": doc.name,
                    "category": doc.category,
                    "description": doc.description[:150],
                    "score": round(score, 3),
                    "parent": doc.metadata.get("parent", ""),
                    "path": doc.metadata.get("path", ""),
                    "browse_path": APILoader.build_browse_path(category_dir, doc.name),
                }
            )

        return results
