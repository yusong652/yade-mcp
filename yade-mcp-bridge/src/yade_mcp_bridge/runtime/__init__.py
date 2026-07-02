# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Coordination with YADE's main thread and simulation loop.

- ``pump`` chooses the thread that processes the execute_code queue
- ``pyrunner`` injects the interrupt/hold tick into ``O.run()``
- ``backgroundRun`` tracks ``O.run(wait=False)`` so tasks wait for it
- ``signals`` holds the cross-thread interrupt and sim-hold primitives
"""

from .pump import startBackgroundPump, startQtPump
from .pyrunner import installPyrunner

__all__ = [
    "startBackgroundPump",
    "startQtPump",
    "installPyrunner",
]
