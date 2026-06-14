# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Task pump strategies - how queued serial-executor work gets executed.

Two interchangeable pumps drive ``SerialExecutor.run_next``:

* Qt timer (gui mode): ticks on the Qt event loop, so callables run on the
  main thread between GUI events without blocking it.
* Background daemon thread (console mode): a plain polling loop.

Both expose a ``start_*`` entry point that kicks the pump off and returns
immediately (the background pump spawns its own thread), so the caller's
thread — the YADE console in console mode — stays free for user input.
``bootstrap.start()`` picks one based on the runtime mode.
"""

import threading
import time

# Keep a global reference to avoid Qt timer garbage collection.
_qt_task_timer = None

# Pump tick cadence in milliseconds: how often run_next() is invoked.
# 20ms balances execute_code responsiveness against polling overhead and is
# the floor on REPL latency. A pump implementation detail, not a tunable.
_TICK_INTERVAL_MS = 20


def start_qt_pump(executor, logger):
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
            executor.run_next()
        except Exception as e:  # task pump must not crash event loop
            logger.error(f"Task pump tick failed: {e}")

    timer = QtCore.QTimer()
    timer.setInterval(_TICK_INTERVAL_MS)
    timer.timeout.connect(_process_tick)
    timer.start()

    _qt_task_timer = timer
    return True


def start_background_pump(executor, logger):
    """Spawn a daemon thread that polls the executor queue. Returns True.

    Returns immediately after starting the thread, mirroring
    ``start_qt_pump``'s non-blocking contract, so the caller's thread stays
    free (in console mode that is the YADE prompt accepting user input).
    """
    pump_thread = threading.Thread(
        target=_background_pump_loop,
        args=(executor, logger),
        daemon=True,
        name="mcp-task-pump",
    )
    pump_thread.start()
    return True


def _background_pump_loop(executor, logger):
    """Poll the executor queue in a loop. Runs in a background daemon thread."""
    sleep_s = _TICK_INTERVAL_MS / 1000.0
    while True:
        try:
            executor.run_next()
        except Exception as e:  # task pump must not crash
            logger.error(f"Task pump tick failed: {e}")
        time.sleep(sleep_s)
