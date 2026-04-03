"""Tests for BM25 search: tokenizer, indexer, scorer."""

from yade_mcp.knowledge.search.document import SearchDocument
from yade_mcp.knowledge.search.indexing.bm25_indexer import BM25Indexer
from yade_mcp.knowledge.search.preprocessing.tokenizer import TextTokenizer
from yade_mcp.knowledge.search.scoring.bm25_scorer import BM25Scorer


def _make_doc(name, description="", keywords=None, category=None):
    return SearchDocument(
        name=name,
        description=description,
        keywords=keywords or [],
        category=category,
    )


# =========================================================================
# Tokenizer
# =========================================================================

class TestTextTokenizer:
    def setup_method(self):
        self.tok = TextTokenizer()

    def test_empty(self):
        assert self.tok.tokenize("") == []

    def test_camel_case(self):
        tokens = self.tok.tokenize("NewtonIntegrator")
        assert "newton" in tokens
        assert "integrator" in tokens

    def test_underscore_split(self):
        tokens = self.tok.tokenize("Ig2_Sphere_Sphere_ScGeom")
        assert "sphere" in tokens
        assert "ig2" in tokens

    def test_dot_split(self):
        tokens = self.tok.tokenize("O.bodies")
        assert "bodies" in tokens

    def test_stopwords_removed(self):
        tokens = self.tok.tokenize("the class is used for simulation")
        assert "the" not in tokens
        assert "is" not in tokens
        assert "simulation" in tokens

    def test_numeric(self):
        tokens = self.tok.tokenize("value 3.14 end")
        assert "3.14" in tokens

    def test_technical_single_chars(self):
        tokens = self.tok.tokenize("x y z coordinate")
        assert "x" in tokens
        assert "y" in tokens
        assert "z" in tokens

    def test_hyphenated(self):
        tokens = self.tok.tokenize("open-source")
        assert "open" in tokens
        assert "source" in tokens


# =========================================================================
# SearchDocument
# =========================================================================

class TestSearchDocument:
    def test_keywords_preserved(self):
        doc = _make_doc("Test", keywords=["Sphere", "GRAVITY"])
        assert doc.keywords == ["Sphere", "GRAVITY"]

    def test_matches_filters_category(self):
        doc = _make_doc("Test", category="engines")
        assert doc.matches_filters({"category": "engines"})
        assert not doc.matches_filters({"category": "materials"})

    def test_matches_filters_empty(self):
        doc = _make_doc("Test", category="engines")
        assert doc.matches_filters({})


# =========================================================================
# BM25 Indexer
# =========================================================================

class TestBM25Indexer:
    def _build_index(self, docs):
        indexer = BM25Indexer()
        indexer.build(docs)
        return indexer

    def test_empty_index(self):
        indexer = self._build_index([])
        assert indexer.doc_count == 0

    def test_basic_indexing(self):
        docs = [_make_doc("FrictMat", description="friction material")]
        indexer = self._build_index(docs)
        assert indexer.doc_count == 1
        assert indexer.get_doc_length("FrictMat", "description") > 0

    def test_term_freq(self):
        docs = [_make_doc("Test", description="sphere sphere sphere")]
        indexer = self._build_index(docs)
        assert indexer.get_term_freq("Test", "sphere", "description") == 3

    def test_idf(self):
        docs = [
            _make_doc("A", description="sphere gravity"),
            _make_doc("B", description="sphere material"),
            _make_doc("C", description="box container"),
        ]
        indexer = self._build_index(docs)
        idf_sphere = indexer.get_idf("sphere", "description")
        idf_gravity = indexer.get_idf("gravity", "description")
        # gravity appears in 1 doc, sphere in 2 — gravity should have higher IDF
        assert idf_gravity > idf_sphere

    def test_missing_doc(self):
        indexer = self._build_index([])
        assert indexer.get_field_tokens("nonexistent", "name") == []
        assert indexer.get_term_freq("nonexistent", "term", "name") == 0

    def test_avg_doc_length(self):
        docs = [
            _make_doc("A", description="one two three"),
            _make_doc("B", description="four five six seven eight"),
        ]
        indexer = self._build_index(docs)
        avg = indexer.get_avg_doc_length("description")
        assert avg > 0


# =========================================================================
# BM25 Scorer
# =========================================================================

class TestBM25Scorer:
    def _score_query(self, docs, query):
        indexer = BM25Indexer()
        indexer.build(docs)
        scorer = BM25Scorer(indexer)
        return scorer.batch_score(query, docs)

    def test_empty_query(self):
        docs = [_make_doc("A", description="sphere")]
        results = self._score_query(docs, "")
        assert len(results) == 0

    def test_no_match(self):
        docs = [_make_doc("A", description="sphere gravity")]
        results = self._score_query(docs, "completely unrelated xyz")
        assert len(results) == 0

    def test_exact_name_match_scores_high(self):
        docs = [
            _make_doc("FrictMat", description="friction material model"),
            _make_doc("CohFrictMat", description="cohesive friction material"),
            _make_doc("NewtonIntegrator", description="time integration"),
        ]
        results = self._score_query(docs, "FrictMat")
        assert len(results) > 0
        top_name = results[0][0].name
        # FrictMat should score highest due to exact name match
        assert "Frict" in top_name

    def test_description_match(self):
        docs = [
            _make_doc("A", description="gravity engine for particle simulation"),
            _make_doc("B", description="material properties"),
        ]
        results = self._score_query(docs, "gravity simulation")
        assert len(results) > 0
        assert results[0][0].name == "A"

    def test_keyword_match(self):
        docs = [
            _make_doc("A", keywords=["sphere", "geometry"]),
            _make_doc("B", keywords=["box", "wall"]),
        ]
        results = self._score_query(docs, "sphere")
        assert len(results) > 0
        assert results[0][0].name == "A"

    def test_score_returns_field_breakdown(self):
        docs = [_make_doc("Sphere", description="a sphere body")]
        indexer = BM25Indexer()
        indexer.build(docs)
        scorer = BM25Scorer(indexer)
        score, info = scorer.score("sphere", docs[0])
        assert "field_scores" in info
        assert "name" in info["field_scores"]
        assert "description" in info["field_scores"]
        assert "keywords" in info["field_scores"]
