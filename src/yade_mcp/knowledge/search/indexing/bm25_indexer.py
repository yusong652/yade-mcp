"""BM25 inverted index with multi-field support for YADE search."""

import math
from collections import Counter

from yade_mcp.knowledge.search.document import SearchDocument
from yade_mcp.knowledge.search.preprocessing.tokenizer import TextTokenizer


class BM25Indexer:
    """Multi-field BM25 index: name, description, keywords."""

    def __init__(self) -> None:
        self._fields: dict[str, dict[str, list[str]]] = {"name": {}, "description": {}, "keywords": {}}
        self._tf: dict[str, dict[str, dict[str, int]]] = {"name": {}, "description": {}, "keywords": {}}
        self._df: dict[str, dict[str, int]] = {"name": {}, "description": {}, "keywords": {}}
        self._avg_len: dict[str, float] = {"name": 0.0, "description": 0.0, "keywords": 0.0}
        self.doc_count: int = 0
        self.tokenizer = TextTokenizer()

    def build(self, documents: list[SearchDocument]) -> None:
        for f in ("name", "description", "keywords"):
            self._fields[f].clear()
            self._tf[f].clear()
            self._df[f].clear()

        for doc in documents:
            doc_id = doc.name
            field_texts = {
                "name": doc.name,
                "description": doc.description,
                "keywords": " ".join(doc.keywords),
            }
            for field, text in field_texts.items():
                tokens = self.tokenizer.tokenize(text)
                self._fields[field][doc_id] = tokens
                self._tf[field][doc_id] = dict(Counter(tokens))
                for term in set(tokens):
                    self._df[field][term] = self._df[field].get(term, 0) + 1

        self.doc_count = len(documents)
        for field in ("name", "description", "keywords"):
            total = sum(len(t) for t in self._fields[field].values())
            self._avg_len[field] = total / self.doc_count if self.doc_count else 0.0

    def get_idf(self, term: str, field: str) -> float:
        df = self._df[field].get(term, 0)
        if self.doc_count == 0 or df == 0:
            return 0.0
        return max(0.0, math.log((self.doc_count - df + 0.5) / (df + 0.5) + 1))

    def get_term_freq(self, doc_id: str, term: str, field: str) -> int:
        return self._tf[field].get(doc_id, {}).get(term, 0)

    def get_doc_length(self, doc_id: str, field: str) -> int:
        return len(self._fields[field].get(doc_id, []))

    def get_avg_doc_length(self, field: str) -> float:
        return self._avg_len[field]

    def get_field_tokens(self, doc_id: str, field: str) -> list[str]:
        return self._fields[field].get(doc_id, [])
