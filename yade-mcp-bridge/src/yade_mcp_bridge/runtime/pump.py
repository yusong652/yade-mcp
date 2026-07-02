# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Pump strategies that drive the execute_code queue.

A pump ticks ``CodeExecutor.run_next`` to drain execute_code requests one
at a time. Long-running tasks bypass it — they run on their own daemon
thread (see execution/script_runner.py) — so a task can't starve the pump.

``startup.start()`` picks one per runtime mode; both start non-blocking:

* Qt timer (gui): ticks on the Qt event loop, so work runs on the main
  thread between GUI events (mandatory for Qt thread affinity).
* Background daemon thread (console): a polling loop that spawns its own
  thread and returns, leaving the YADE prompt free for input.
"""

import threading
import time

# Keep a global reference to avoid Qt timer garbage collection.
_qt_pump_timer = None

# Pump tick cadence in milliseconds: how often run_next() is invoked.
_TICK_INTERVAL_MS = 20


def start_qt_pump(executor, logger):
    """Drive the execute_code pump from the Qt event loop. Returns True on success."""
    global _qt_pump_timer

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
    if _qt_pump_timer is not None:
        try:
            _qt_pump_timer.stop()
        except RuntimeError:
            pass

    def _pump_tick():
        try:
            executor.run_next()
        except Exception as e:  # pump must not crash the event loop
            logger.error(f"execute_code pump tick failed: {e}")

    timer = QtCore.QTimer()
    timer.setInterval(_TICK_INTERVAL_MS)
    timer.timeout.connect(_pump_tick)
    timer.start()

    _qt_pump_timer = timer
    return True


def start_background_pump(executor, logger):
    """Start a daemon thread that polls the executor queue."""
    pump_thread = threading.Thread(
        target=_background_pump_loop,
        args=(executor, logger),
        daemon=True,
        name="mcp-exec-pump",
    )
    pump_thread.start()
    return True


def _background_pump_loop(executor, logger):
    """Poll the execute_code queue in a loop. Runs in a background daemon thread."""
    sleep_s = _TICK_INTERVAL_MS / 1000.0
    while True:
        try:
            executor.run_next()
        except Exception as e:  # pump must not crash
            logger.error(f"execute_code pump tick failed: {e}")
        time.sleep(sleep_s)
