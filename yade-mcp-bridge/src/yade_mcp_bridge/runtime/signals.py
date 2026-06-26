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

from __future__ import annotations

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
_exec_thread_ids: dict[str, int] = {}
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


def register_exec_thread(exec_id: str, thread_id: int) -> None:
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


def unregister_exec_thread(exec_id: str) -> None:
    """Drop the thread record for ``exec_id``. Idempotent."""
    with _exec_thread_lock:
        _exec_thread_ids.pop(exec_id, None)


def get_exec_thread(exec_id: str) -> int | None:
    """Return the recorded thread id for ``exec_id``, or None."""
    with _exec_thread_lock:
        return _exec_thread_ids.get(exec_id)


# ---------------------------------------------------------------------------
# Sim-hold rendezvous: execute_code consistent-snapshot window.
#
# NOTE: this is NOT ``O.pause()``. YADE's loop keeps "running" (O.running stays
# True); the snippet just holds the PyRunner tick inside one engine slot so the
# scene stops advancing for the duration of the window, then releases it.
#
# Lets an execute_code snippet (running on the pump thread) freeze YADE's
# simulation cycle at a clean engine boundary, so it sees a CONSISTENT scene
# (no torn/mid-step reads) and can mutate without racing the cycle, then
# resume — while the snippet itself keeps running on the pump thread, so
# async-abort on timeout still works (the alternative, running the snippet
# ON the sim thread, is un-abortable: Dummy-N → boost::python → C++ FATAL).
#
# It is a two-way handshake between the snippet (pump thread) and the PyRunner
# tick (YADE's C++ sim thread, a Dummy-N boost::python thread). The tick
# NEVER receives an injected exception — it holds the cycle COOPERATIVELY on an
# Event (GIL released), so the snippet can run while it waits:
#
#   snippet: set _hold_wanted -> wait _cycle_held -> <work> -> clear + set _snippet_released
#   tick:    see _hold_wanted -> set _cycle_held -> wait _snippet_released -> continue
#
# Events (not Conditions) are used deliberately: a set() persists, so there
# is no lost-wakeup risk regardless of which side reaches its wait first.
# ---------------------------------------------------------------------------

_hold_lock = threading.Lock()  # one window at a time. The serial pump never
# contends today; kept as a defensive guard so the Event handshake can't corrupt
# if a second thread ever opens a window.
_hold_wanted = threading.Event()  # snippet -> cycle: please hold
_cycle_held = threading.Event()  # cycle -> snippet: held, scene is frozen
_snippet_released = threading.Event()  # snippet -> cycle: done, resume
_window_local = threading.local()  # marks the thread currently holding a window

# Max time the cycle will stay held waiting for the snippet to finish. Bounds
# the damage if the snippet hangs (e.g. stuck C-level I/O) while holding the
# window: the sim resumes instead of freezing forever.
_MAX_HOLD_S = 30.0


def hold_if_wanted(max_hold_s: float = _MAX_HOLD_S) -> None:
    """Cooperative brake, called by the PyRunner tick on YADE's sim thread.

    If an execute_code snippet has requested a snapshot window, hold here
    (GIL released, so the snippet can run) until it releases — or until
    ``max_hold_s`` elapses, after which we resume anyway so a hung snippet
    can't freeze the sim indefinitely. The scene is quiescent at the
    PyRunner's engine slot, so the snippet gets a consistent view.
    """
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


def snippet_holds_sim() -> bool:
    """True if the CURRENT thread is inside a sim-hold window.

    Read by the ``O.run`` hook to refuse driving the cycle from a snippet
    that is holding the cycle frozen — that would deadlock: the snippet's
    ``O.wait()`` would block on an iteration count the held cycle can
    never reach, and ``wait()`` sits in released-GIL C code where async
    abort cannot fire. The task's own ``O.run`` runs on its companion
    thread (never inside a window), so it is unaffected.
    """
    return bool(getattr(_window_local, "active", False))


@contextlib.contextmanager
def sim_hold_window(acquire_timeout_s: float = 2.0):
    """Snippet side: freeze the sim cycle for an exclusive snapshot window.

    Yields ``True`` if the cycle actually held (snapshot is consistent),
    ``False`` if it did not hold within ``acquire_timeout_s`` (a single
    step longer than the timeout, or the cycle ended meanwhile) — in which
    case the caller's reads are best-effort/concurrent, as before.

    Always releases the cycle on exit, including on exception / async
    abort, so the sim never stays frozen because a snippet raised.
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
