# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""YADE runtime coordination: the bridge's foothold inside the live process.

These modules drive and observe YADE's main-thread / simulation loop —
distinct from request handling. ``pump`` chooses the thread that runs queued
executor work; ``pyrunner`` injects the interrupt/pause tick into ``O.run()``;
``signals`` holds the cross-thread interrupt and paused-snapshot primitives
they coordinate through.
"""

from .pump import start_background_pump, start_qt_pump
from .pyrunner import install_pyrunner

__all__ = [
    "start_background_pump",
    "start_qt_pump",
    "install_pyrunner",
]
