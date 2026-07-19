"""Runtime settings read from the environment."""

import os


def is_debug_mode() -> bool:
    """Return True when the server should expose diagnostic error details."""
    # Defaults on so local development sees full details; deployments set
    # YADE_MCP_DEBUG=0 to strip error.details from responses.
    return os.getenv("YADE_MCP_DEBUG", "1") != "0"
