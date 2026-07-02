# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Pump strategies that drive the execute_code queue.

A pump ticks ``CodeExecutor.runNext`` to process queued execute_code
requests one at a time. Long-running tasks bypass it — they run on their
own daemon thread (see execution/script_runner.py) — so a task can't
starve the pump.

``startup.start()`` picks one per runtime mode; both start non-blocking:

* Qt timer (gui): ticks on the Qt event loop, so work runs on the main
  thread between GUI events (mandatory for Qt thread affinity).
* Background daemon thread (console): a polling loop that spawns its own
  thread and returns, leaving the YADE prompt free for input.
"""

import threading
import time

# Keep a global reference to avoid Qt timer garbage collection.
_qtPumpTimer = None

# Pump tick cadence in milliseconds: how often runNext() is invoked.
_TICK_INTERVAL_MS = 20


def startQtPump(executor, logger):
    """Drive the execute_code pump from the Qt event loop. Returns True on success."""
    global _qtPumpTimer

    # Qt5-first matches YADE's current default GUI; PyQt6 covers Qt6 builds
    try:
        from PyQt5 import QtCore
    except ImportError:
        try:
            from PyQt6 import QtCore
        except ImportError:
            return False

    app = QtCore.QCoreApplication.instance()
    if app is None:
        return False

    # Stop previous timer if start() is called multiple times.
    if _qtPumpTimer is not None:
        try:
            _qtPumpTimer.stop()
        except RuntimeError:
            pass

    def _pumpTick():
        try:
            executor.runNext()
        except Exception as e:  # pump must not crash the event loop
            logger.error(f"execute_code pump tick failed: {e}")

    timer = QtCore.QTimer()
    timer.setInterval(_TICK_INTERVAL_MS)
    timer.timeout.connect(_pumpTick)
    timer.start()

    _qtPumpTimer = timer
    return True


def startBackgroundPump(executor, logger):
    """Start a daemon thread that polls the executor queue."""
    pumpThread = threading.Thread(
        target=_backgroundPumpLoop,
        args=(executor, logger),
        daemon=True,
        name="mcp-exec-pump",
    )
    pumpThread.start()
    return True


def _backgroundPumpLoop(executor, logger):
    """Poll the execute_code queue in a loop. Runs in a background daemon thread."""
    sleepS = _TICK_INTERVAL_MS / 1000.0
    while True:
        try:
            executor.runNext()
        except Exception as e:  # pump must not crash
            logger.error(f"execute_code pump tick failed: {e}")
        time.sleep(sleepS)
