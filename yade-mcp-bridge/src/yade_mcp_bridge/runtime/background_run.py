# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Track ``O.run(wait=False)`` so a task can wait for it to finish.

``O.run(wait=False)`` returns immediately — usually before the background
run has even started (``O.running`` still False). The ``O.run`` hook (see
pyrunner) records each call here; a task calls
``wait_for_background_run()`` before reporting, so its result covers the
whole run.
"""

import threading
import time

_RUN_START_TIMEOUT_S = 0.5  # max wait for the background run to start

_background_run = threading.local()


def is_background_run(args, kwargs):
    """True if this ``O.run`` call returns while its cycling continues
    (``wait=False`` — the 2nd positional or the ``wait`` keyword; default False).
    """
    wait = kwargs["wait"] if "wait" in kwargs else (args[1] if len(args) >= 2 else False)
    return not wait


def mark_background_run(pending):
    """Set this thread's flag: did its last ``O.run`` leave cycling unfinished?"""
    _background_run.pending = pending


def _background_run_pending():
    return getattr(_background_run, "pending", False)


def wait_for_background_run():
    """Block until the pending background run finishes."""
    try:
        from yade import O as _O
    except ImportError:
        return
    if not _background_run_pending():
        return
    # Poll (bounded) until the background run starts, then O.wait() for it
    # to finish (unbounded).
    deadline = time.monotonic() + _RUN_START_TIMEOUT_S
    while not _O.running and time.monotonic() < deadline:
        time.sleep(0.005)
    if _O.running:
        _O.wait()  # blocks; re-raises cycling errors as RuntimeError
