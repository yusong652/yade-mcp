# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Path normalization utilities for MCP bridge server."""


def pathToLlmFormat(path):
    """Convert a path to LLM-friendly format (forward slashes)."""
    return path.replace("\\", "/")
