"""Path normalization utilities for YADE bridge server."""


def path_to_llm_format(path):
    # type: (str) -> str
    """Convert a path to LLM-friendly format (forward slashes)."""
    return path.replace('\\', '/')
