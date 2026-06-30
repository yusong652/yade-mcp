# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Cross-thread signals between the bridge and YADE's running simulation.

Three jobs:

1. Task interruption — signal a running task to stop at a clean point.
2. execute_code interruption — stop a timed-out snippet at a clean point; if it
   is not driving the simulation, by injecting an exception into its thread.
3. Snapshot hold — briefly hold the simulation cycle so an execute_code read
   sees a consistent scene whose values do not span a running step.
"""

import contextlib
import logging
import threading

logger = logging.getLogger("MCP-Bridge")

# Global state for interrupt signaling
_current_task_id = None
_current_task_lock = threading.Lock()
_interrupt_requested = {}  # task_id -> bool

# Thread registry for the fallback interrupt: maps a task's task_id / an
# execute_code's request_id to its worker thread, so a Python exception can be
# injected when execution is not in a running step.
_exec_thread_ids = {}
_exec_thread_lock = threading.Lock()


def set_current_task(task_id):
    """Set the currently executing task ID."""
    global _current_task_id
    with _current_task_lock:
        _current_task_id = task_id


def clear_current_task():
    """Clear the currently executing task ID."""
    global _current_task_id
    with _current_task_lock:
        _current_task_id = None


def get_current_task():
    """Return the currently-set task/request id, or None."""
    with _current_task_lock:
        return _current_task_id


def request_interrupt(task_id):
    """Set the interrupt flag for an id (opaque: a task_id or a request_id)."""
    _interrupt_requested[task_id] = True
    logger.info(f"Interrupt requested for task: {task_id}")


def is_task_interrupt_requested(task_id):
    """Check if interruption was requested for a specific task."""
    return _interrupt_requested.get(task_id, False)


def clear_interrupt(task_id):
    """Clear interrupt flag for a task."""
    _interrupt_requested.pop(task_id, None)


def register_exec_thread(exec_id, thread_id):
    """Map a task_id / execute_code request_id to its worker thread."""
    with _exec_thread_lock:
        # Drop entries whose thread has died — defensive, in case a caller
        # ever skips its unregister.
        if _exec_thread_ids:
            alive = {t.ident for t in threading.enumerate() if t.is_alive()}
            stale = [eid for eid, tid in _exec_thread_ids.items() if tid not in alive]
            for eid in stale:
                _exec_thread_ids.pop(eid, None)
        _exec_thread_ids[exec_id] = thread_id


def unregister_exec_thread(exec_id):
    """Drop the thread record for ``exec_id``. Idempotent."""
    with _exec_thread_lock:
        _exec_thread_ids.pop(exec_id, None)


def get_exec_thread(exec_id):
    """Return the recorded thread id for ``exec_id``, or None."""
    with _exec_thread_lock:
        return _exec_thread_ids.get(exec_id)


# ---------------------------------------------------------------------------
# Sim-hold: a consistent snapshot for execute_code.
#
# While a task drives the cycle, an execute_code snippet must read the scene
# without spanning steps. The snippet raises a cross-thread signal and the
# PyRunner tick (registered in O.engines) waits on it, holding the cycle at a
# clean engine boundary until the snippet is done, then resuming.
#
# NOT O.pause(): O.running stays True; only the tick blocks, on an Event.
# ---------------------------------------------------------------------------

_hold_lock = threading.Lock()  # only one thread touches the hold state at a time
_hold_wanted = threading.Event()  # execute_code wants to hold the task
_cycle_held = threading.Event()  # task is held, scene frozen
_snippet_released = threading.Event()  # execute_code done, task resumes
_window_local = threading.local()  # marks the thread currently holding the task

_MAX_HOLD_S = 30.0  # max hold; past it the cycle resumes even if not released


def hold_if_wanted(max_hold_s=_MAX_HOLD_S):
    """Called by the PyRunner tick each step: if a hold is wanted, hold the
    task here until the snippet releases (or ``max_hold_s`` elapses)."""
    if not _hold_wanted.is_set():
        return
    _snippet_released.clear()
    _cycle_held.set()
    if not _snippet_released.wait(timeout=max_hold_s):
        logger.warning(
            "execute_code hold window exceeded %.0fs; resuming sim "
            "(snippet may be stuck in a C call while holding the window)",
            max_hold_s,
        )
    _cycle_held.clear()


def snippet_holds_sim():
    """True if the current thread (a snippet) is currently holding the task."""
    return bool(getattr(_window_local, "active", False))


@contextlib.contextmanager
def sim_hold_window(acquire_timeout_s=2.0):
    """Snippet side: hold the sim cycle for a consistent snapshot.

    Always releases the task on exit (incl. exception / async abort).
    """
    with _hold_lock:
        _cycle_held.clear()
        _snippet_released.clear()
        _hold_wanted.set()
        _window_local.active = True
        try:
            held = _cycle_held.wait(timeout=acquire_timeout_s)
            yield held
        finally:
            _window_local.active = False
            _hold_wanted.clear()
            _snippet_released.set()
