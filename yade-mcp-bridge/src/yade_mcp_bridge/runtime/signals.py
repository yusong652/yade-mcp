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
_currentTaskId = None
_currentTaskLock = threading.Lock()
_interruptRequested = {}  # task_id -> bool

# Thread registry for the fallback interrupt: maps a task's task_id / an
# execute_code's request_id to its worker thread, so a Python exception can be
# injected when execution is not in a running step.
_execThreadIds = {}
_execThreadLock = threading.Lock()


def setCurrentTask(taskId):
    """Set the currently executing task ID."""
    global _currentTaskId
    with _currentTaskLock:
        _currentTaskId = taskId


def clearCurrentTask():
    """Clear the currently executing task ID."""
    global _currentTaskId
    with _currentTaskLock:
        _currentTaskId = None


def getCurrentTask():
    """Return the currently-set task/request id, or None."""
    with _currentTaskLock:
        return _currentTaskId


def requestInterrupt(taskId):
    """Set the interrupt flag for an id (opaque: a task_id or a request_id)."""
    _interruptRequested[taskId] = True
    logger.info(f"Interrupt requested for task: {taskId}")


def isTaskInterruptRequested(taskId):
    """Check if interruption was requested for a specific task."""
    return _interruptRequested.get(taskId, False)


def clearInterrupt(taskId):
    """Clear interrupt flag for a task."""
    _interruptRequested.pop(taskId, None)


def registerExecThread(execId, threadId):
    """Map a task_id / execute_code request_id to its worker thread."""
    with _execThreadLock:
        # Drop entries whose thread has died — defensive, in case a caller
        # ever skips its unregister.
        if _execThreadIds:
            alive = {t.ident for t in threading.enumerate() if t.is_alive()}
            stale = [eid for eid, tid in _execThreadIds.items() if tid not in alive]
            for eid in stale:
                _execThreadIds.pop(eid, None)
        _execThreadIds[execId] = threadId


def unregisterExecThread(execId):
    """Drop the thread record for ``execId``. Idempotent."""
    with _execThreadLock:
        _execThreadIds.pop(execId, None)


def getExecThread(execId):
    """Return the recorded thread id for ``execId``, or None."""
    with _execThreadLock:
        return _execThreadIds.get(execId)


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

_holdLock = threading.Lock()  # only one thread touches the hold state at a time
_holdWanted = threading.Event()  # execute_code wants to hold the task
_cycleHeld = threading.Event()  # task is held, scene frozen
_snippetReleased = threading.Event()  # execute_code done, task resumes
_holdLocal = threading.local()  # marks the thread currently holding the task

_MAX_HOLD_S = 30.0  # fallback hold limit, for holds that set no maxHoldS
_holdMaxS = None  # per-hold limit, set by holdSim from the request timeout


def holdIfWanted(maxHoldS=None):
    """Called by the PyRunner tick each step: if a hold is wanted, hold the
    task here until the snippet releases (or the hold limit elapses).

    The limit is ``maxHoldS`` if given, else the holding snippet's
    (``holdSim(maxHoldS=...)``), else ``_MAX_HOLD_S``."""
    if not _holdWanted.is_set():
        return
    if maxHoldS is None:
        maxHoldS = _holdMaxS if _holdMaxS is not None else _MAX_HOLD_S
    _snippetReleased.clear()
    _cycleHeld.set()
    if not _snippetReleased.wait(timeout=maxHoldS):
        logger.warning(
            "execute_code held the cycle past %.0fs; resuming sim (snippet may be stuck in a C call while holding)",
            maxHoldS,
        )
    _cycleHeld.clear()


def snippetHoldsSim():
    """True if the current thread (a snippet) is currently holding the task."""
    return bool(getattr(_holdLocal, "active", False))


@contextlib.contextmanager
def holdSim(acquireTimeoutS=2.0, maxHoldS=None):
    """Snippet side: hold the sim cycle for a consistent snapshot.

    ``maxHoldS`` bounds how long the cycle may stay held (None →
    ``_MAX_HOLD_S``). Always releases the task on exit (incl. exception /
    async abort).
    """
    global _holdMaxS
    with _holdLock:
        _cycleHeld.clear()
        _snippetReleased.clear()
        _holdMaxS = maxHoldS
        _holdWanted.set()
        _holdLocal.active = True
        try:
            held = _cycleHeld.wait(timeout=acquireTimeoutS)
            yield held
        finally:
            _holdLocal.active = False
            _holdWanted.clear()
            _holdMaxS = None
            _snippetReleased.set()
