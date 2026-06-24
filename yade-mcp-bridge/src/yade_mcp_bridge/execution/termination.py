# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Async exception injection for terminating stuck code execution.

Used by ``code_runner.py`` (and the task-interrupt handler) when a timeout
or interrupt fires: inject a ``BridgeTimeout`` / ``TaskInterrupt`` into the
thread running user code so it unwinds at the next Python bytecode edge,
freeing the pump thread.

Last-resort only. The standard path is ``signals.request_interrupt`` + the
PyRunner tick, which pauses ``O.run`` but cannot break a pure-Python loop;
``inject_async_exception`` fills that gap.
"""

from __future__ import annotations

import ctypes
import threading


def inject_async_exception(thread_id: int, exc_type: type[BaseException]) -> int:
    """Inject ``exc_type`` into the target thread via CPython's async
    exception API. Returns the number of threads affected: 0 = no matching
    thread, 1 = queued, -1 = API misuse (undone immediately, per the docs).

    The exception fires only at Python bytecode edges. A thread stuck in C
    (numpy/scipy, ``time.sleep``, GIL-releasing I/O) receives it queued and
    unwinds when control returns to Python.

    Refuses a ``Dummy-N`` target — a non-Python thread borrowing the GIL
    (e.g. YADE's sim thread inside a PyRunner command). An exception there
    would unwind out of Python back into boost::python and trip YADE's C++
    FATAL handler. By construction we only ever target threads we registered
    (the pump and task threads, all real), so this never fires in practice;
    it is a guardrail on the ctypes footgun, not a reachable branch.
    """
    for t in threading.enumerate():
        if t.ident == thread_id and t.name.startswith("Dummy-"):
            return 0

    exc = ctypes.py_object(exc_type)
    tid = ctypes.c_ulong(thread_id)
    affected = ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, exc)
    if affected > 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, ctypes.c_void_p())
        return -1
    return int(affected)
