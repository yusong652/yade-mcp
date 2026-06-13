# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Async exception injection for terminating stuck code execution.

Used by ``handlers/execute_code.py`` when a timeout fires: we inject a
``BridgeTimeout`` into the thread that is running user code so the code
unwinds at the next Python bytecode edge, freeing the pump thread.

This is a last-resort mechanism. The standard cancellation path is
``signals.request_interrupt`` + PyRunner tick, which pauses ``O.run``
but does not interrupt pure Python loops. ``fire_async_exception``
fills that gap.

Safety: ``Dummy-N`` threads must NOT receive an injected exception —
the exception would propagate back into ``boost::python`` and trigger
YADE's C++ FATAL handler.

MainThread is intentionally ALLOWED. In Qt mode the bridge pump runs
on MainThread via a ``QTimer`` tick, so a stuck ``execute_code`` body
is always sitting on MainThread's Python stack (``QTimer →
_process_tick → process_tasks → _execute_code → exec(user_code)``).
Refusing to inject there would strand the pump forever with no way to
recover short of restarting the bridge. The injection is safe because:

* The caller (``_terminate_stuck_execution``) looks up the target
  ``tid`` via ``get_exec_thread(request_id)``, which only returns a
  value while ``_execute_code`` is inside its try-block — so MainThread
  is demonstrably running user code, not idling in ``QApplication.exec_``.
* ``BridgeTimeout`` is caught by ``_execute_code``'s explicit
  ``except BridgeTimeout`` branch and never propagates back up into
  the Qt event loop.

``is_safe_to_async_raise`` encodes the Dummy-N guard. Callers must
check it before invoking ``fire_async_exception``.
"""

from __future__ import annotations

import ctypes
import threading


def _find_thread(thread_id: int) -> threading.Thread | None:
    """Return the ``Thread`` object for the given ident, or None.

    ``threading.enumerate()`` is a snapshot; a thread that exited
    between enumeration and our inspection is reported as None.
    """
    for t in threading.enumerate():
        if t.ident == thread_id:
            return t
    return None


def is_safe_to_async_raise(thread_id: int) -> tuple[bool, str]:
    """Check whether it is safe to inject an exception into ``thread_id``.

    Returns ``(True, "ok")`` when injection is allowed, otherwise
    ``(False, reason)`` with a short machine-readable reason code.

    Rejected cases:

    * ``thread_not_alive`` — thread already exited or never existed.
    * ``nested_boost_python_callback`` — name starts with ``Dummy-``;
      injecting would escape via boost::python → YADE FATAL.

    MainThread is accepted: see module docstring for the rationale.
    """
    target = _find_thread(thread_id)
    if target is None or not target.is_alive():
        return False, "thread_not_alive"
    if target.name.startswith("Dummy-"):
        return False, "nested_boost_python_callback"
    return True, "ok"


def fire_async_exception(thread_id: int, exc_type: type[BaseException]) -> int:
    """Inject ``exc_type`` into the target thread via CPython's async
    exception API. Returns the number of threads affected.

    Semantics per ``PyThreadState_SetAsyncExc`` (CPython docs):

    * Return 0 → the thread_id was invalid / no matching thread.
    * Return 1 → success; exception will fire at the next bytecode edge
      in the target thread.
    * Return >1 → API misuse; docs mandate immediately undoing by
      calling again with NULL. We do that and return -1 to signal the
      caller that something went wrong.

    The exception fires only at Python bytecode edges. Threads stuck in
    C code (numpy, scipy, ``time.sleep``, GIL-releasing I/O) receive
    the exception queued but it does not fire until control returns to
    Python. ``time.sleep`` is exempt — it re-raises on wakeup because
    ``PyErr_CheckSignals``-like paths run post-sleep.
    """
    exc = ctypes.py_object(exc_type)
    tid = ctypes.c_ulong(thread_id)
    affected = ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, exc)
    if affected > 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, ctypes.c_void_p())
        return -1
    return int(affected)
