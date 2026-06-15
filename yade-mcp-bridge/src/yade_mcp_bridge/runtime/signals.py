# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""YADE signal handling for task interruption."""

from __future__ import annotations

import contextlib
import logging
import threading

logger = logging.getLogger("MCP-Bridge")

# Global state for interrupt signaling
_current_task_id = None
_current_task_lock = threading.Lock()
_interrupt_requested = {}  # task_id -> bool

# Thread registry for execute_code cancellation. Keyed by request_id
# (or task_id — both are opaque strings here), value is the
# ``threading.get_ident()`` of the worker currently running that
# request. Populated at the top of ``_execute_code``, cleaned in its
# ``finally``. Read by the execute_code timeout handler to target
# ``PyThreadState_SetAsyncExc``.
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


def peek_current_task():
    """Return the currently-set task/request id, or None.

    Used by ``_execute_code`` to implement a save-and-restore pattern:
    when an execute_code runs *inside* another script's PyRunner tick,
    we must not clobber the outer script's ``_current_task_id`` on the
    way out.
    """
    with _current_task_lock:
        return _current_task_id


def request_interrupt(task_id):
    """Request interruption of a specific task."""
    _interrupt_requested[task_id] = True
    logger.info(f"Interrupt requested for task: {task_id}")


def is_task_interrupt_requested(task_id):
    """Check if interruption was requested for a specific task.

    Used where the caller knows its own task id (e.g. a task script).
    """
    return _interrupt_requested.get(task_id, False)


def is_current_interrupt_requested():
    """Check if interruption was requested for the currently-running task.

    Used where the caller has no handle on its own task id — notably the
    PyRunner tick, which fires generically on YADE's sim thread and
    discovers the active task via the ambient ``_current_task_id`` set by
    ``set_current_task``.
    """
    with _current_task_lock:
        task_id = _current_task_id
    return bool(task_id) and _interrupt_requested.get(task_id, False)


def clear_interrupt(task_id):
    """Clear interrupt flag for a task."""
    _interrupt_requested.pop(task_id, None)


# ---------------------------------------------------------------------------
# Fire-and-forget cycling marker (per thread).
#
# ``O.run(wait=False)`` returns before YADE's C++ sim thread has flipped
# ``O.running`` True. A task that ends with such a dispatch must drain the
# cycling before reporting success, but right after the script's ``exec``
# there is no way to tell "cycling about to start" from "no cycling at all" —
# both read ``O.running == False``. The ``O.run`` hook records, per calling
# thread, whether the last run left fire-and-forget cycling; the task drain
# reads its OWN thread's flag to decide whether to wait. Per-thread
# (``threading.local``) so an ``execute_code`` ``O.run`` on the pump thread
# never bleeds into a task's drain on its own dedicated thread.
# ---------------------------------------------------------------------------

_async_cycling = threading.local()


def mark_async_cycling(pending):
    """Record whether the calling thread's last ``O.run`` left undrained
    fire-and-forget cycling (``wait=False``)."""
    _async_cycling.pending = pending


def async_cycling_pending():
    """True if THIS thread's last ``O.run`` was a ``wait=False`` dispatch
    whose cycling has not been drained yet."""
    return getattr(_async_cycling, "pending", False)


def register_exec_thread(request_id: str, thread_id: int) -> None:
    """Record which OS thread is currently running ``request_id``.

    As a cheap leak-defense, scrubs any pre-existing entries whose
    recorded thread is no longer alive. Leaks would only occur if
    ``_execute_code`` exited through a path that skipped its
    ``finally`` — vanishingly rare, but the scrub keeps the registry
    from growing unboundedly over a long-lived bridge session.
    """
    with _exec_thread_lock:
        if _exec_thread_ids:
            alive = {t.ident for t in threading.enumerate() if t.is_alive()}
            stale = [rid for rid, tid in _exec_thread_ids.items() if tid not in alive]
            for rid in stale:
                _exec_thread_ids.pop(rid, None)
        _exec_thread_ids[request_id] = thread_id


def unregister_exec_thread(request_id: str) -> None:
    """Drop the thread record for ``request_id``. Idempotent."""
    with _exec_thread_lock:
        _exec_thread_ids.pop(request_id, None)


def get_exec_thread(request_id: str) -> int | None:
    """Return the recorded thread id for ``request_id``, or None."""
    with _exec_thread_lock:
        return _exec_thread_ids.get(request_id)


# ---------------------------------------------------------------------------
# Sim-pause rendezvous: execute_code consistent-snapshot window.
#
# Lets an execute_code snippet (running on the pump thread) freeze YADE's
# simulation cycle at a clean engine boundary, so it sees a CONSISTENT scene
# (no torn/mid-step reads) and can mutate without racing the cycle, then
# resume — while the snippet itself keeps running on the pump thread, so
# async-abort on timeout still works (the alternative, running the snippet
# ON the sim thread, is un-abortable: Dummy-N → boost::python → C++ FATAL).
#
# It is a two-way handshake between the REPL (pump thread) and the PyRunner
# tick (YADE's C++ sim thread, a Dummy-N boost::python thread). The tick
# NEVER receives an injected exception — it parks COOPERATIVELY on an Event
# (GIL released), so the REPL can run while it waits:
#
#   REPL:  set _pause_wanted -> wait _cycle_parked -> <work> -> clear + set _repl_released
#   tick:  see _pause_wanted -> set _cycle_parked -> wait _repl_released -> continue
#
# Events (not Conditions) are used deliberately: a set() persists, so there
# is no lost-wakeup risk regardless of which side reaches its wait first.
# ---------------------------------------------------------------------------

_pause_lock = threading.Lock()  # serialize: at most one window at a time
_pause_wanted = threading.Event()  # REPL -> cycle: please park
_cycle_parked = threading.Event()  # cycle -> REPL: parked, scene is frozen
_repl_released = threading.Event()  # REPL -> cycle: done, resume
_window_local = threading.local()  # marks the thread currently holding a window

# Max time the cycle will stay parked waiting for the REPL to finish. Bounds
# the damage if the REPL hangs (e.g. stuck C-level I/O) while holding the
# window: the sim resumes instead of freezing forever.
_PAUSE_MAX_HOLD_S = 30.0


def park_if_pause_wanted(max_hold_s: float = _PAUSE_MAX_HOLD_S) -> None:
    """Cooperative brake, called by the PyRunner tick on YADE's sim thread.

    If an execute_code snippet has requested a snapshot window, park here
    (GIL released, so the REPL can run) until it releases — or until
    ``max_hold_s`` elapses, after which we resume anyway so a hung REPL
    can't freeze the sim indefinitely. The scene is quiescent at the
    PyRunner's engine slot, so the REPL gets a consistent view.
    """
    if not _pause_wanted.is_set():
        return
    _repl_released.clear()
    _cycle_parked.set()
    if not _repl_released.wait(timeout=max_hold_s):
        logger.warning(
            "execute_code pause window exceeded %.0fs; resuming sim "
            "(REPL may be stuck in a C call while holding the window)",
            max_hold_s,
        )
    _cycle_parked.clear()


def repl_holds_sim() -> bool:
    """True if the CURRENT thread is inside a sim-pause window.

    Read by the ``O.run`` hook to refuse driving the cycle from a snippet
    that is holding the cycle frozen — that would deadlock: the snippet's
    ``O.wait()`` would block on an iteration count the parked cycle can
    never reach, and ``wait()`` sits in released-GIL C code where async
    abort cannot fire. The task's own ``O.run`` runs on its companion
    thread (never inside a window), so it is unaffected.
    """
    return bool(getattr(_window_local, "active", False))


@contextlib.contextmanager
def sim_paused_window(acquire_timeout_s: float = 2.0):
    """REPL side: freeze the sim cycle for an exclusive snapshot window.

    Yields ``True`` if the cycle actually parked (snapshot is consistent),
    ``False`` if it did not park within ``acquire_timeout_s`` (a single
    step longer than the timeout, or the cycle ended meanwhile) — in which
    case the caller's reads are best-effort/concurrent, as before.

    Always releases the cycle on exit, including on exception / async
    abort, so the sim never stays frozen because a snippet raised.
    """
    with _pause_lock:
        _cycle_parked.clear()
        _repl_released.clear()
        _pause_wanted.set()
        _window_local.active = True
        try:
            parked = _cycle_parked.wait(timeout=acquire_timeout_s)
            yield parked
        finally:
            _window_local.active = False
            _pause_wanted.clear()
            _repl_released.set()
