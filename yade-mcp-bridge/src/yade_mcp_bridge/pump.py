"""Task pump strategies - how queued main-thread work gets executed.

Two interchangeable pumps drive ``MainThreadExecutor.process_tasks``:

* Qt timer (gui mode): ticks on the Qt event loop, so tasks run on the
  main thread between GUI events without blocking it.
* Background daemon thread (console mode): a plain polling loop.

``bootstrap.start()`` picks one based on the runtime mode.
"""

import time

# Keep a global reference to avoid Qt timer garbage collection.
_qt_task_timer = None


def _per_tick_limit(max_tasks_per_tick):
    """Normalize the per-tick task cap: None/<=0 -> unlimited, junk -> 1."""
    if max_tasks_per_tick is None:
        return None
    try:
        value = int(max_tasks_per_tick)
    except (TypeError, ValueError):
        return 1
    return value if value > 0 else None


def start_qt_pump(main_executor, interval_ms, max_tasks_per_tick, logger):
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

    per_tick = _per_tick_limit(max_tasks_per_tick)

    def _process_tick():
        try:
            main_executor.process_tasks(max_tasks=per_tick)
        except Exception as e:  # task pump must not crash event loop
            logger.error(f"Task pump tick failed: {e}")

    timer = QtCore.QTimer()
    timer.setInterval(interval_ms)
    timer.timeout.connect(_process_tick)
    timer.start()

    _qt_task_timer = timer
    return True


def run_background_pump(main_executor, interval_ms, max_tasks_per_tick, logger):
    """Poll task queue in a loop. Runs in a background daemon thread."""
    per_tick = _per_tick_limit(max_tasks_per_tick)

    sleep_s = interval_ms / 1000.0
    while True:
        try:
            main_executor.process_tasks(max_tasks=per_tick)
        except Exception as e:  # task pump must not crash
            logger.error(f"Task pump tick failed: {e}")
        time.sleep(sleep_s)
