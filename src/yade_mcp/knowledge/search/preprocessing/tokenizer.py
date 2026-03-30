"""Text tokenization for YADE search system.

Handles CamelCase, underscores, dots, and YADE-specific naming conventions
like Ig2_Sphere_Sphere_ScGeom, Law2_ScGeom_FrictPhys_CundallStrack.
"""

import re

from yade_mcp.knowledge.search.preprocessing.stopwords import is_stopword


class TextTokenizer:
    """Tokenizer for YADE technical documentation."""

    WORD_PATTERN = re.compile(r"\b[\w.-]+\b")
    NUMERIC_PATTERN = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
    TECHNICAL_SINGLE_CHARS = {"x", "y", "z", "r", "n", "t"}

    def __init__(self, remove_stopwords: bool = True, min_length: int = 2):
        self.remove_stopwords = remove_stopwords
        self.min_length = min_length

    def tokenize(self, text: str) -> list[str]:
        if not text:
            return []

        original_words = self.WORD_PATTERN.findall(text)
        text_lower = text.lower()
        lowercase_words = self.WORD_PATTERN.findall(text_lower)

        tokens: list[str] = []
        for original_word, word in zip(original_words, lowercase_words):
            if "." in word:
                if self.NUMERIC_PATTERN.match(word):
                    if self._is_valid(word):
                        tokens.append(word)
                else:
                    for orig_part, part in zip(original_word.split("."), word.split(".")):
                        self._split_and_add(orig_part, part, tokens)
            elif "_" in word:
                for orig_part, part in zip(original_word.split("_"), word.split("_")):
                    self._split_and_add(orig_part, part, tokens)
            elif "-" in word:
                for part in word.split("-"):
                    if self._is_valid(part, technical=True):
                        tokens.append(part)
            else:
                for part in self._split_camel(original_word):
                    if self._is_valid(part):
                        tokens.append(part)

        return tokens

    def _split_and_add(self, original: str, lowered: str, tokens: list[str]) -> None:
        """Split a word part (may contain CamelCase) and add valid tokens."""
        for part in self._split_camel(original):
            if self._is_valid(part, technical=True):
                tokens.append(part)

    def _split_camel(self, word: str) -> list[str]:
        """Split CamelCase: NewtonIntegrator → ['newton', 'integrator']."""
        spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", word)
        parts = spaced.split()
        if len(parts) > 1:
            return [p.lower() for p in parts]
        return [word.lower()]

    def _is_valid(self, word: str, technical: bool = False) -> bool:
        if word.isdigit():
            return True
        if len(word) == 1 and word.lower() in self.TECHNICAL_SINGLE_CHARS:
            return True
        if len(word) < self.min_length:
            return False
        if not any(c.isalnum() for c in word):
            return False
        if self.remove_stopwords and is_stopword(word):
            return False
        return True
