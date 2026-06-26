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

_INTERRUPT_CHECK_PERIOD = 1  # PyRunner iterPeriod — check every step

_async_cycling = threading.local()


def mark_async_cycling(pending):
    """Set this thread's flag for whether its last ``O.run`` left cycling undrained."""
    _async_cycling.pending = pending


def async_cycling_pending():
    """True if THIS thread's last ``O.run`` was a ``wait=False`` dispatch
    whose cycling has not been drained yet."""
    return getattr(_async_cycling, "pending", False)


def _is_async_run(args, kwargs) -> bool:
    """True if this ``O.run`` was dispatched ``wait=False`` (returns while its
    cycling continues). ``wait`` is ``O.run``'s 2nd positional or its ``wait``
    keyword; default False."""
    wait = kwargs["wait"] if "wait" in kwargs else (args[1] if len(args) >= 2 else False)
    return not wait


def install_pyrunner(logger):
    """Install the interrupt/snapshot PyRunner at ``O.engines[0]`` and hook
    ``O.run()`` to re-inject it. Returns True on success."""
    try:
        from yade import O, PyRunner
    except ImportError:
        logger.warning("PyRunner not available, interrupt checking during simulation disabled")
        return False

    import sys as _sys

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
        # the O.run() hook raises InterruptedError to interrupt the task once
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
        # YADE's PyRunner evaluates its command via boost::python::exec with
        # globals = sys.modules['__main__'].__dict__, re-resolved each call.
        # IPython's %run (and similar) temporarily replaces __main__ with the
        # script's module, hiding _mcp_pyrunner_tick and causing
        # "name '_mcp_pyrunner_tick' is not defined" once PyRunner fires.
        # Idempotently re-inject into whichever module is currently __main__.
        main_mod = _sys.modules.get("__main__")
        if main_mod is not None and getattr(main_mod, "_mcp_pyrunner_tick", None) is not _mcp_pyrunner_tick:
            main_mod._mcp_pyrunner_tick = _mcp_pyrunner_tick

    _ensure_tick_in_main()

    # Identify our PyRunner by its command string, not by engine label.
    # YADE's labeled-entity auto-injection runs `__builtins__.<label>=...`
    # on every `O.engines=[...]` assignment, which crashes with
    # "AttributeError: 'dict' object has no attribute '<label>'" in any
    # non-__main__ namespace (e.g. inside `%run script.py`) because
    # __builtins__ is the dict there, not the module.
    #
    # The inline marker comment makes the engine self-identifying when users
    # print(O.engines) — hopefully discouraging them from mutating it.
    _PYRUNNER_COMMAND = "_mcp_pyrunner_tick()  # yade-mcp-bridge: DO NOT MODIFY"

    def _make_pyrunner():
        """Create a fresh PyRunner instance."""
        return PyRunner(
            command=_PYRUNNER_COMMAND,
            iterPeriod=_INTERRUPT_CHECK_PERIOD,
            dead=False,
        )

    def _find_our_pyrunner():
        """Return our PyRunner engine if present, else None."""
        for e in O.engines:
            if getattr(e, "command", None) == _PYRUNNER_COMMAND:
                return e
        return None

    def _normalize_pyrunner():
        """Ensure our PyRunner is present at O.engines[0] and has the
        canonical config. Self-heals against user scripts that (a) reassigned
        O.engines (wiping us), (b) mutated our iterPeriod/dead (e.g. via
        O.engines[-1].iterPeriod = ...), or (c) left us somewhere in the
        middle. Warns on tamper so the cause of any interrupt-latency bug is
        visible in the log."""
        expected_period = _INTERRUPT_CHECK_PERIOD
        existing = _find_our_pyrunner()

        if existing is None:
            try:
                O.engines = [_make_pyrunner()] + list(O.engines)
                logger.info("PyRunner auto-injected at O.engines[0] before O.run()")
            except Exception as e:
                logger.warning(f"PyRunner auto-injection failed: {e}")
            return

        # Detect tamper before restoring so the user sees why their
        # interrupt might have been delayed on the previous run.
        actual_period = getattr(existing, "iterPeriod", expected_period)
        actual_dead = getattr(existing, "dead", False)
        if actual_period != expected_period or actual_dead:
            logger.warning(
                "MCP PyRunner was tampered with (iterPeriod=%r, dead=%r); "
                "restoring to iterPeriod=%d, dead=False. "
                "Likely cause: user script modified O.engines[-1] or similar — "
                "our PyRunner sits at O.engines[0], prefer naming or positive "
                "indices for your own engines.",
                actual_period,
                actual_dead,
                expected_period,
            )

        try:
            existing.iterPeriod = expected_period
            existing.dead = False
        except Exception as e:
            logger.warning(f"Failed to normalize PyRunner config: {e}")

        # If we're not at index 0, move there. Negative indexing from user
        # scripts is the common failure mode we're defending against.
        try:
            if O.engines[0] is not existing:
                new_engines = [existing] + [e for e in O.engines if e is not existing]
                O.engines = new_engines
        except Exception as e:
            logger.warning(f"Failed to move PyRunner to front: {e}")

    # Hook O.run() to auto-inject PyRunner before each simulation run.
    # This handles O.reset() clearing engines — our PyRunner gets
    # re-added transparently before simulation starts.
    # Guard against double-hooking if start() is called multiple times.
    if not getattr(O.run, "_mcp_hooked", False):
        _original_run = O.run

        def _hooked_run(*args, **kwargs):
            # Refuse driving the cycle from a snippet that is holding a
            # sim-hold snapshot window: the cycle is frozen (held in the
            # PyRunner tick), so this O.run()'s O.wait() would block on an
            # iteration the cycle can never reach → deadlock. The task's own
            # O.run runs on its companion thread (never inside a window), so
            # snippet_holds_sim() is False there and the task is unaffected.
            if snippet_holds_sim():
                raise RuntimeError(
                    "O.run() refused: execute_code is holding a sim-hold "
                    "window (the simulation cycle is frozen for a consistent "
                    "read/edit). A snippet must not drive the cycle here. Use "
                    "yade_execute_task for simulation runs."
                )
            # Defend against __main__ replacement (e.g. IPython %run) between
            # bridge start and this O.run() call.
            _ensure_tick_in_main()
            _normalize_pyrunner()
            _interrupt_triggered["value"] = False
            result = _original_run(*args, **kwargs)
            # A wait=False run returns before its cycling finishes; flag it for
            # the task drain (wait=True already drained synchronously).
            mark_async_cycling(_is_async_run(args, kwargs))
            # After O.run() returns (possibly due to O.pause() from interrupt),
            # check if interrupt was the reason and raise at Python level.
            # This avoids the FATAL ERROR from YADE's C++ exception handling.
            if _interrupt_triggered["value"]:
                _interrupt_triggered["value"] = False
                raise InterruptedError("Interrupted by MCP bridge")
            return result

        _hooked_run._mcp_hooked = True
        O.run = _hooked_run

    try:
        O.engines = [_make_pyrunner()] + list(O.engines)
        logger.info(f"PyRunner installed at O.engines[0] (iterPeriod={_INTERRUPT_CHECK_PERIOD}) — interrupt check")
        return True
    except Exception as e:
        logger.warning(f"Failed to install PyRunner: {e}")
        return False
