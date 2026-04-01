"""BM25 scorer with multi-field support for YADE search."""

from typing import Any

from yade_mcp.knowledge.search.document import SearchDocument
from yade_mcp.knowledge.search.indexing.bm25_indexer import BM25Indexer
from yade_mcp.knowledge.search.preprocessing.tokenizer import TextTokenizer


class BM25Scorer:
    """Multi-field BM25 scoring: name(0.5) + keywords(0.3) + description(0.2)."""

    K1 = 1.5
    B = 0.75
    WEIGHT_NAME = 0.5
    WEIGHT_KEYWORDS = 0.3
    WEIGHT_DESCRIPTION = 0.2

    def __init__(self, indexer: BM25Indexer):
        self.indexer = indexer
        self.tokenizer = TextTokenizer()

    def score(self, query: str, document: SearchDocument) -> tuple[float, dict[str, Any]]:
        query_tokens = set(self.tokenizer.tokenize(query))
        if not query_tokens:
            return 0.0, {}

        doc_id = document.name
        name_score = self._score_field(query_tokens, doc_id, "name")
        desc_score = self._score_field(query_tokens, doc_id, "description")
        kw_score = self._score_field(query_tokens, doc_id, "keywords")

        total = self.WEIGHT_NAME * name_score + self.WEIGHT_DESCRIPTION * desc_score + self.WEIGHT_KEYWORDS * kw_score

        return total, {
            "field_scores": {
                "name": round(name_score, 3),
                "description": round(desc_score, 3),
                "keywords": round(kw_score, 3),
            },
        }

    def _score_field(self, query_tokens: set[str], doc_id: str, field: str) -> float:
        doc_tokens = set(self.indexer.get_field_tokens(doc_id, field))
        if not doc_tokens:
            return 0.0

        matched = query_tokens & doc_tokens
        total = 0.0
        for term in matched:
            idf = self.indexer.get_idf(term, field)
            tf = self.indexer.get_term_freq(doc_id, term, field)
            if tf == 0:
                continue
            doc_len = self.indexer.get_doc_length(doc_id, field)
            avg_len = self.indexer.get_avg_doc_length(field)
            norm = 1 - self.B + self.B * (doc_len / avg_len) if avg_len > 0 else 1.0
            saturated_tf = (tf * (self.K1 + 1)) / (tf + self.K1 * norm)
            total += idf * saturated_tf
        return total

    def batch_score(
        self, query: str, documents: list[SearchDocument]
    ) -> list[tuple[SearchDocument, float, dict[str, Any]]]:
        results = []
        for doc in documents:
            s, info = self.score(query, doc)
            if s > 0:
                results.append((doc, s, info))
        return results
