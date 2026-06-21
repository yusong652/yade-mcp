# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""YADE simulation-side integration: PyRunner injection and the O.run() hook.

This is the bridge's only foothold inside the running simulation. A
self-identifying PyRunner engine is kept at ``O.engines[0]`` so that while
``O.run()`` is live, YADE's C++ sim loop periodically calls back into
Python to (a) observe the MCP interrupt flag and (b) park at a consistent
engine boundary when an ``execute_code`` snippet holds a paused-snapshot
window. A hook around ``O.run()`` re-injects and normalizes the engine
before every run, surviving ``O.reset()`` and user scripts that reassign
``O.engines``.
"""

# PyRunner check cadence (iterPeriod): how many simulation iterations pass
# between interrupt-flag checks. Fixed at 1 (every step), not a tunable — the
# execute_code cycle-interrupt grace (_CYCLE_INTERRUPT_GRACE_S) assumes
# O.pause() lands within ~one step, so a larger period would silently break
# it. Per-step cost is one trivial flag check, negligible against a DEM step.
_INTERRUPT_CHECK_PERIOD = 1


def install_pyrunner(logger):
    """Install a YADE PyRunner engine for interrupt checking during
    simulation.

    While ``O.run()`` is live, YADE's C++ sim loop calls this tick on a
    ``Dummy-N`` (boost::python) thread every simulation step
    (``_INTERRUPT_CHECK_PERIOD``). The tick's only job is to observe the
    interrupt flag and call ``O.pause()`` — the Python side then raises
    ``InterruptedError`` after ``O.run()`` returns. It must do nothing
    else: any other work would run on the ``Dummy-N`` thread, which
    ``is_safe_to_async_raise`` cannot abort (boost::python stack → C++
    FATAL on exception).

    Returns True if the PyRunner was installed successfully.
    """
    try:
        from yade import O
        from yade._utils import PyRunner  # noqa: F811
    except ImportError:
        try:
            from yade import PyRunner  # type: ignore
        except ImportError:
            logger.warning("PyRunner not available, interrupt checking during simulation disabled")
            return False

    import sys as _sys

    from .signals import (
        is_current_interrupt_requested,
        mark_async_cycling,
        park_if_pause_wanted,
        snippet_holds_sim,
    )

    # Flag checked after O.run() returns
    _interrupt_triggered = {"value": False}

    def _mcp_pyrunner_tick():
        # Interrupt flag check only — do nothing else here (see
        # install_pyrunner docstring: this runs on a Dummy-N thread).
        # Instead of raising an exception inside PyRunner (which causes
        # YADE's C++ layer to log a FATAL ERROR), we just pause the
        # simulation. The hooked O.run() will check the flag after
        # O.run() returns and raise InterruptedError at the Python level.
        if is_current_interrupt_requested():
            _interrupt_triggered["value"] = True
            try:
                O.pause()
            except Exception:
                pass
        # Cooperative pause point. If an execute_code snippet has asked for
        # a consistent-snapshot window, park here (GIL released) at this
        # engine boundary until it releases — see signals.sim_paused_window.
        park_if_pause_wanted()

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

    # Store config for re-injection
    _pyrunner_config = {
        "period": _INTERRUPT_CHECK_PERIOD,
        "PyRunner": PyRunner,
        "O": O,
    }

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
        return _pyrunner_config["PyRunner"](
            command=_PYRUNNER_COMMAND,
            iterPeriod=_pyrunner_config["period"],
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
        expected_period = _pyrunner_config["period"]
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
            # sim-pause snapshot window: the cycle is frozen (parked in the
            # PyRunner tick), so this O.run()'s O.wait() would block on an
            # iteration the cycle can never reach → deadlock. The task's own
            # O.run runs on its companion thread (never inside a window), so
            # snippet_holds_sim() is False there and the task is unaffected.
            if snippet_holds_sim():
                raise RuntimeError(
                    "O.run() refused: execute_code is holding a paused-snapshot "
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
            # Record fire-and-forget cycling so the task drain knows to wait
            # for the C++ sim thread to pick it up. wait=True already drained
            # synchronously. Signature: O.run(nSteps=-1, wait=False), so wait
            # is the 2nd positional or the "wait" keyword.
            wait = kwargs["wait"] if "wait" in kwargs else (args[1] if len(args) >= 2 else False)
            mark_async_cycling(not wait)
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
