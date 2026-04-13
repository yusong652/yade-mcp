"""Runtime settings read from the environment.

Keeps the dev/prod behaviour toggle in one place so its semantics
don't drift across modules. Today there's only one flag —
``YADE_MCP_DEBUG`` — but this is where more will land.
"""

import os


def is_debug_mode() -> bool:
    """Return True when the server should expose diagnostic details.

    Set ``YADE_MCP_DEBUG=0`` to run in production mode. Production mode
    strips ``error.details`` before responses leave the MCP boundary —
    traceback, exception type, internal URLs, and any other diagnostic
    bookkeeping. The LLM still sees ``code`` and ``message``, which are
    enough to decide what to do next.

    Defaults to debug mode (on) so local development is not surprising.
    Deployments should set ``YADE_MCP_DEBUG=0`` explicitly.
    """
    return os.getenv("YADE_MCP_DEBUG", "1") != "0"
