# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""The bridge's foothold inside a running simulation.

A PyRunner engine registered in ``O.engines`` so that, while a task runs, YADE
calls back into Python each step to check:

1. whether the running task was asked to interrupt;
2. whether an execute_code snippet wants to hold the cycle for a consistent
   snapshot.

A hook around ``O.run()`` keeps the engine installed, re-injecting it before
every run so it survives ``O.reset()`` and user scripts that reassign
``O.engines``.
"""

import threading
import time

_INTERRUPT_CHECK_PERIOD = 1  # PyRunner iterPeriod — check every step
_ASYNC_CYCLING_PICKUP_TIMEOUT_S = 0.5  # max wait for the cycle to start (pickup gap)

_async_cycling = threading.local()


def mark_async_cycling(pending):
    """Set this thread's flag for whether its last ``O.run`` left cycling undrained."""
    _async_cycling.pending = pending


def _async_cycling_pending():
    """True if THIS thread's last ``O.run`` was a ``wait=False`` dispatch
    whose cycling has not been drained yet."""
    return getattr(_async_cycling, "pending", False)


def _is_async_run(args, kwargs):
    """True if this ``O.run`` was dispatched ``wait=False`` (returns while its
    cycling continues). ``wait`` is ``O.run``'s 2nd positional or its ``wait``
    keyword; default False."""
    wait = kwargs["wait"] if "wait" in kwargs else (args[1] if len(args) >= 2 else False)
    return not wait


def drain_async_cycling():
    """Block until the cycling a pending ``O.run(wait=False)`` dispatched
    finishes, so the caller does not report success while the sim still cycles
    (orphan cycling hides errors and evades interrupts).
    """
    try:
        from yade import O as _O
    except ImportError:
        return
    if not _async_cycling_pending():
        return
    # wait=False returns before O.running flips True: poll for the cycle to
    # start (bounded), then O.wait() for the run itself (unbounded).
    deadline = time.monotonic() + _ASYNC_CYCLING_PICKUP_TIMEOUT_S
    while not _O.running and time.monotonic() < deadline:
        time.sleep(0.005)
    if _O.running:
        _O.wait()  # blocks; re-raises cycling errors as RuntimeError


def install_pyrunner(logger):
    """Install the interrupt/snapshot PyRunner at ``O.engines[0]`` and hook
    ``O.run()`` to re-inject it. Returns True on success."""
    try:
        from yade import O, PyRunner
    except ImportError:
        logger.warning("PyRunner not available, interrupt checking during simulation disabled")
        return False

    import sys as _sys

    from ..execution.termination import CycleInterrupt
    from .signals import (
        get_current_task,
        hold_if_wanted,
        is_task_interrupt_requested,
        snippet_holds_sim,
    )

    # Flag checked after O.run() returns
    _interrupt_triggered = {"value": False}

    def _mcp_pyrunner_tick():
        # Never raise on the sim thread (→ C++ FATAL). Set a flag + O.pause();
        # the O.run() hook raises CycleInterrupt to interrupt the task once
        # O.run() returns.
        task_id = get_current_task()
        if task_id and is_task_interrupt_requested(task_id):
            _interrupt_triggered["value"] = True
            try:
                O.pause()
            except Exception:
                pass
        # Cooperative hold point. If an execute_code snippet has asked for
        # a consistent-snapshot window, hold here (GIL released) at this
        # engine boundary until it releases — see signals.sim_hold_window.
        hold_if_wanted()

    def _ensure_tick_in_main():
        # PyRunner resolves its command name against the live __main__, which
        # %run can swap mid-run; (re-)bind the tick into whatever is __main__ now.
        main_mod = _sys.modules.get("__main__")
        if main_mod is not None and getattr(main_mod, "_mcp_pyrunner_tick", None) is not _mcp_pyrunner_tick:
            main_mod._mcp_pyrunner_tick = _mcp_pyrunner_tick

    _ensure_tick_in_main()

    # Identify the MCP bridge PyRunner by its command string, not an engine label —
    # auto-injected labels break outside __main__ (e.g. inside %run).
    _PYRUNNER_COMMAND = "_mcp_pyrunner_tick()  # mcp bridge: DO NOT MODIFY"

    def _make_pyrunner():
        """Build the bridge's PyRunner engine."""
        return PyRunner(
            command=_PYRUNNER_COMMAND,
            iterPeriod=_INTERRUPT_CHECK_PERIOD,
            dead=False,
        )

    def _find_our_pyrunner():
        """Return the MCP bridge PyRunner engine if present, else None."""
        for e in O.engines:
            if getattr(e, "command", None) == _PYRUNNER_COMMAND:
                return e
        return None

    def _normalize_pyrunner():
        """Ensure the MCP bridge PyRunner is still present, live, and at O.engines[0],
        restoring it if a user script wiped, disabled, or moved it."""
        pyrunner = _find_our_pyrunner()

        if pyrunner is None:
            # Wiped (O.reset() or a user reassigned O.engines) — re-add at front.
            try:
                # O.engines += [LLM()] # Yet another engine! - Yusong with Nagisa
                O.engines = [_make_pyrunner()] + list(O.engines)
                logger.debug("PyRunner auto-injected at O.engines[0] before O.run()")
            except Exception as e:
                logger.warning(f"PyRunner auto-injection failed: {e}")
            return

        # Restore config in case a user script changed it (e.g. a negative index
        # landing on the engine at O.engines[0]).
        try:
            if pyrunner.iterPeriod != _INTERRUPT_CHECK_PERIOD or pyrunner.dead:
                logger.debug("MCP bridge PyRunner iterPeriod/dead changed; restoring")
                pyrunner.iterPeriod = _INTERRUPT_CHECK_PERIOD
                pyrunner.dead = False
        except Exception as e:
            logger.warning(f"Failed to normalize PyRunner config: {e}")

        # Move back to index 0 if a user script (e.g. negative indexing) pushed
        # the engine out of front.
        try:
            if O.engines[0] is not pyrunner:
                new_engines = [pyrunner] + [e for e in O.engines if e is not pyrunner]
                O.engines = new_engines
        except Exception as e:
            logger.warning(f"Failed to move PyRunner to front: {e}")

    # Hook O.run() to auto-inject PyRunner before each simulation run.
    # This handles O.reset() clearing engines — the MCP bridge PyRunner gets
    # re-added transparently before simulation starts.
    # Guard against double-hooking if start() is called multiple times.
    if not getattr(O.run, "_mcp_hooked", False):
        _original_run = O.run

        def _hooked_run(*args, **kwargs):
            # A snippet holding a sim-hold window must not drive the cycle: the
            # cycle is frozen for its snapshot, so O.run() here would wait on a
            # step that can never come → deadlock. No syntax parsing — the
            # snippet's own O.run() routes through this hook, where
            # snippet_holds_sim() catches it at runtime and refuses cleanly.
            # (The task's own O.run is on the companion thread, never in a
            # window, so this never fires for it.)
            if snippet_holds_sim():
                raise RuntimeError(
                    "O.run() refused: execute_code is holding a sim-hold "
                    "window (the simulation cycle is frozen for a consistent "
                    "read/edit). A snippet must not drive the cycle here. Use "
                    "yade_execute_task for simulation runs."
                )
            _ensure_tick_in_main()
            _normalize_pyrunner()
            _interrupt_triggered["value"] = False
            result = _original_run(*args, **kwargs)
            # A wait=False run returns before its cycling finishes; flag it for
            # the task drain (wait=True already drained synchronously).
            mark_async_cycling(_is_async_run(args, kwargs))
            # After O.run() returns (possibly due to O.pause() from interrupt),
            # check if interrupt was the reason and raise at Python level.
            if _interrupt_triggered["value"]:
                _interrupt_triggered["value"] = False
                raise CycleInterrupt("Interrupted by MCP bridge")
            return result

        _hooked_run._mcp_hooked = True
        O.run = _hooked_run

    try:
        # Idempotent inject: add-if-missing (a second install_pyrunner won't
        # duplicate the engine), then restore config and move to front.
        _normalize_pyrunner()
        logger.info(f"PyRunner installed at O.engines[0] (iterPeriod={_INTERRUPT_CHECK_PERIOD}) — interrupt check")
        return True
    except Exception as e:
        logger.warning(f"Failed to install PyRunner: {e}")
        return False
