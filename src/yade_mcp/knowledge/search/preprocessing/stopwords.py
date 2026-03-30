"""Stopwords for YADE technical documentation."""

STOPWORDS = {
    "a", "an", "the", "this", "that", "these", "those", "it", "its",
    "they", "them", "their", "what", "which", "who", "whom",
    "with", "from", "to", "for", "of", "in", "on", "at", "by", "as",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "under", "again", "further", "then", "once",
    "and", "or", "but", "nor", "so", "yet",
    "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "having", "do", "does", "did", "doing",
    "will", "would", "can", "could", "may", "might", "shall", "should", "must",
    "if", "than", "because", "while", "where", "when", "why", "how",
    "all", "both", "each", "few", "more", "most", "other", "some", "such",
    "no", "not", "only", "own", "same", "too", "very",
    "here", "there", "now", "just", "also",
}

TECHNICAL_PRESERVE = {
    "set", "get", "force", "stress", "strain", "contact", "body", "sphere",
    "wall", "facet", "engine", "material", "interaction", "run", "step",
    "reset", "save", "load", "create", "delete", "apply", "compute",
    "model", "law", "phys", "geom", "damping", "gravity", "friction",
    "cohesion", "stiffness", "velocity", "position", "radius", "mass",
    "density", "young", "poisson", "normal", "shear", "tangent",
    "periodic", "cell", "clump", "polyhedra", "triaxial", "compress",
}


def is_stopword(word: str) -> bool:
    word_lower = word.lower()
    if word_lower in TECHNICAL_PRESERVE:
        return False
    return word_lower in STOPWORDS
