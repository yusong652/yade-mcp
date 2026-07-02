# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""The bridge's PyRunner engine: per-step checks while a simulation runs.

Registered in ``O.engines`` so that, while a task runs, YADE calls back
into Python each step to check:

1. whether the running task was asked to interrupt;
2. whether an execute_code snippet wants to hold the cycle for a consistent
   snapshot.

A hook around ``O.run()`` keeps the engine installed, re-injecting it before
every run so it survives ``O.reset()`` and user scripts that reassign
``O.engines``.
"""

from .background_run import isBackgroundRun, markBackgroundRun

_INTERRUPT_CHECK_PERIOD = 1  # PyRunner iterPeriod — check every step


def installPyrunner(logger):
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
        getCurrentTask,
        holdIfWanted,
        isTaskInterruptRequested,
        snippetHoldsSim,
    )

    # Flag checked after O.run() returns
    _interruptTriggered = {"value": False}

    def _mcpPyrunnerTick():
        # Never raise on the sim thread (→ C++ FATAL). Set a flag + O.pause();
        # the O.run() hook raises CycleInterrupt to interrupt the task once
        # O.run() returns.
        taskId = getCurrentTask()
        if taskId and isTaskInterruptRequested(taskId):
            _interruptTriggered["value"] = True
            try:
                O.pause()
            except Exception:
                pass
        # Cooperative hold point. If an execute_code snippet has asked for
        # a consistent snapshot, hold here (GIL released) at this engine
        # boundary until it releases — see signals.holdSim.
        holdIfWanted()

    def _ensureTickInMain():
        # PyRunner resolves its command name against the live __main__, which
        # %run can swap mid-run; (re-)bind the tick into whatever is __main__ now.
        mainMod = _sys.modules.get("__main__")
        if mainMod is not None and getattr(mainMod, "_mcpPyrunnerTick", None) is not _mcpPyrunnerTick:
            mainMod._mcpPyrunnerTick = _mcpPyrunnerTick

    _ensureTickInMain()

    # Identify the MCP bridge PyRunner by its command string, not an engine label —
    # auto-injected labels break outside __main__ (e.g. inside %run).
    _PYRUNNER_COMMAND = "_mcpPyrunnerTick()  # mcp bridge: DO NOT MODIFY"

    def _makePyrunner():
        """Build the bridge's PyRunner engine."""
        return PyRunner(
            command=_PYRUNNER_COMMAND,
            iterPeriod=_INTERRUPT_CHECK_PERIOD,
            dead=False,
        )

    def _findOurPyrunner():
        """Return the MCP bridge PyRunner engine if present, else None."""
        for e in O.engines:
            if getattr(e, "command", None) == _PYRUNNER_COMMAND:
                return e
        return None

    def _normalizePyrunner():
        """Ensure the MCP bridge PyRunner is still present, live, and at O.engines[0],
        restoring it if a user script wiped, disabled, or moved it."""
        pyrunner = _findOurPyrunner()

        if pyrunner is None:
            # Wiped (O.reset() or a user reassigned O.engines) — re-add at front.
            try:
                # O.engines += [LLM()] # Yet another engine!
                O.engines = [_makePyrunner()] + list(O.engines)
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
                newEngines = [pyrunner] + [e for e in O.engines if e is not pyrunner]
                O.engines = newEngines
        except Exception as e:
            logger.warning(f"Failed to move PyRunner to front: {e}")

    # Hook O.run() to auto-inject PyRunner before each simulation run.
    # This handles O.reset() clearing engines — the MCP bridge PyRunner gets
    # re-added transparently before simulation starts.
    # Guard against double-hooking if start() is called multiple times.
    if not getattr(O.run, "_mcp_hooked", False):
        _originalRun = O.run

        def _hookedRun(*args, **kwargs):
            # While a task runs, an execute_code snippet holds the cycle
            # frozen; an O.run() inside that snippet is refused here. The
            # task's own O.run (on its own thread, never holds) is unaffected.
            if snippetHoldsSim():
                raise RuntimeError(
                    "O.run() refused: execute_code is holding the simulation "
                    "cycle frozen for a consistent read/edit, so it must not "
                    "drive the cycle at the same time. Use yade_execute_task "
                    "for simulation runs."
                )
            _ensureTickInMain()
            _normalizePyrunner()
            _interruptTriggered["value"] = False
            result = _originalRun(*args, **kwargs)
            # A wait=False run returns before its cycling finishes; flag it so
            # the task waits for it before reporting (wait=True already has).
            markBackgroundRun(isBackgroundRun(args, kwargs))
            # After O.run() returns (possibly due to O.pause() from interrupt),
            # check if interrupt was the reason and raise at Python level.
            if _interruptTriggered["value"]:
                _interruptTriggered["value"] = False
                raise CycleInterrupt("Interrupted by MCP bridge")
            return result

        _hookedRun._mcp_hooked = True
        O.run = _hookedRun

    try:
        # Idempotent inject: add-if-missing (a second installPyrunner won't
        # duplicate the engine), then restore config and move to front.
        _normalizePyrunner()
        logger.info(f"PyRunner installed at O.engines[0] (iterPeriod={_INTERRUPT_CHECK_PERIOD}) — interrupt check")
        return True
    except Exception as e:
        logger.warning(f"Failed to install PyRunner: {e}")
        return False
