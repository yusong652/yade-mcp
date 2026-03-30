"""YADE signal handling for task interruption."""

import logging
import threading

logger = logging.getLogger("YADE-Bridge")

# Global state for interrupt signaling
_current_task_id = None
_current_task_lock = threading.Lock()
_interrupt_requested = {}  # task_id -> bool


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


def request_interrupt(task_id):
    """Request interruption of a specific task."""
    _interrupt_requested[task_id] = True
    logger.info("Interrupt requested for task: {}".format(task_id))


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
