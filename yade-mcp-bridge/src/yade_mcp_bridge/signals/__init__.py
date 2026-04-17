"""YADE signal handling for task interruption."""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("YADE-Bridge")

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


def is_interrupt_requested(task_id=None):
    """Check if interruption was requested for a task."""
    if task_id:
        return _interrupt_requested.get(task_id, False)
    with _current_task_lock:
        if _current_task_id:
            return _interrupt_requested.get(_current_task_id, False)
    return False


def clear_interrupt(task_id):
    """Clear interrupt flag for a task."""
    _interrupt_requested.pop(task_id, None)


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
