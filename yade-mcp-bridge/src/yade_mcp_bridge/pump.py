# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Task pump strategies - how queued main-thread work gets executed.

Two interchangeable pumps drive ``MainThreadExecutor.process_task``:

* Qt timer (gui mode): ticks on the Qt event loop, so tasks run on the
  main thread between GUI events without blocking it.
* Background daemon thread (console mode): a plain polling loop.

``bootstrap.start()`` picks one based on the runtime mode.
"""

import time

# Keep a global reference to avoid Qt timer garbage collection.
_qt_task_timer = None

# Pump tick cadence in milliseconds: how often process_task() is invoked.
# 20ms balances execute_code responsiveness against polling overhead and is
# the floor on REPL latency. A pump implementation detail, not a tunable.
_TICK_INTERVAL_MS = 20


def start_qt_pump(main_executor, logger):
    """Try to attach task processing to Qt event loop. Returns True on success."""
    global _qt_task_timer

    try:
        from PyQt5 import QtCore
    except ImportError:
        return False

    app = QtCore.QCoreApplication.instance()
    if app is None:
        return False

    # Stop previous timer if start() is called multiple times.
    if _qt_task_timer is not None:
        try:
            _qt_task_timer.stop()
        except RuntimeError:
            pass

    def _process_tick():
        try:
            main_executor.process_task()
        except Exception as e:  # task pump must not crash event loop
            logger.error(f"Task pump tick failed: {e}")

    timer = QtCore.QTimer()
    timer.setInterval(_TICK_INTERVAL_MS)
    timer.timeout.connect(_process_tick)
    timer.start()

    _qt_task_timer = timer
    return True


def run_background_pump(main_executor, logger):
    """Poll task queue in a loop. Runs in a background daemon thread."""
    sleep_s = _TICK_INTERVAL_MS / 1000.0
    while True:
        try:
            main_executor.process_task()
        except Exception as e:  # task pump must not crash
            logger.error(f"Task pump tick failed: {e}")
        time.sleep(sleep_s)
