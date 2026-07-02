# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Interrupt/termination control-flow signals and the async-injection mechanism.

Two ``BaseException`` signals carry an interrupt out to its catcher, plus the
``injectAsyncException`` helper that delivers the forced one across threads.
These are control flow, not errors — hence ``BaseException``, so a user
``except Exception:`` cannot swallow them.
"""

import ctypes
import threading


class CycleInterrupt(BaseException):
    """Cooperative interrupt at a cycle boundary: the PyRunner tick sets a flag
    and ``O.pause()``s, then the ``O.run`` hook raises this once the run returns.

    Inherits ``BaseException`` (not ``Exception``) so a user ``except
    Exception:`` around their ``O.run`` cannot swallow it and defeat the
    interrupt. Both ``_execute`` runners — code_runner (execute_code) and
    script_runner (tasks) — catch it explicitly.
    """


class AsyncAbort(BaseException):
    """Async-injected (PyThreadState_SetAsyncExc) to force-terminate a stuck
    pure-Python body the cycle-interrupt path cannot reach — e.g. a
    ``while True`` with no ``O.run`` on the stack, so no PyRunner tick fires to
    observe the interrupt flag.

    Inherits ``BaseException`` (not ``Exception``) so a user ``except
    Exception:`` cannot swallow it. Each catcher — ``_execute`` in code_runner
    (execute_code) and in script_runner (tasks) — handles it explicitly and must
    never let it escape, or it would kill the worker thread.
    """


def injectAsyncException(threadId, excType):
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
    it is a guardrail on the raw ctypes call, not a reachable branch.
    """
    for t in threading.enumerate():
        if t.ident == threadId and t.name.startswith("Dummy-"):
            return 0

    exc = ctypes.py_object(excType)
    tid = ctypes.c_ulong(threadId)
    affected = ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, exc)
    if affected > 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, ctypes.c_void_p())
        return -1
    return int(affected)
