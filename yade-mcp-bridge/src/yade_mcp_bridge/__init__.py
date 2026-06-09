"""YADE MCP Bridge - HTTP + SSE bridge for YADE DEM simulation.

Runs inside YADE's Python environment and exposes simulation control
as a remote HTTP API (request/response over POST, server push over a
Server-Sent Events stream) for MCP clients.

Usage (inside YADE Python console):
    import yade_mcp_bridge
    yade_mcp_bridge.start()

Usage (batch/console mode):
    import yade_mcp_bridge
    yade_mcp_bridge.start(mode="console")
"""

__version__ = "0.5.0"

# Keep global references to avoid Qt timer garbage collection.
_qt_task_timer = None

DEFAULT_TIMER_INTERVAL_MS = 20
DEFAULT_MAX_TASKS_PER_TICK = 1
DEFAULT_INTERRUPT_CHECK_PERIOD = 1
DEFAULT_MAX_TASKS = 1024
VALID_RUNTIME_MODES = ("auto", "gui", "console")


def _start_qt_pump(main_executor, interval_ms, max_tasks_per_tick, logger):
    """Try to attach task processing to Qt event loop. Returns True on success."""
    global _qt_task_timer

    try:
        from PyQt5 import QtCore
    except ImportError:
        return False

    app = QtCore.QCoreApplication.instance()
    if app is None:
        return False

    # Stop previous timer if start() is called multiple times.
    if _qt_task_timer is not None:
        try:
            _qt_task_timer.stop()
        except RuntimeError:
            pass

    per_tick = None
    if max_tasks_per_tick is not None:
        try:
            value = int(max_tasks_per_tick)
            if value > 0:
                per_tick = value
        except (TypeError, ValueError):
            per_tick = 1

    def _process_tick():
        try:
            main_executor.process_tasks(max_tasks=per_tick)
        except Exception as e:  # task pump must not crash event loop
            logger.error(f"Task pump tick failed: {e}")

    timer = QtCore.QTimer()
    timer.setInterval(interval_ms)
    timer.timeout.connect(_process_tick)
    timer.start()

    _qt_task_timer = timer
    return True


def _run_background_pump(main_executor, interval_ms, max_tasks_per_tick, logger):
    """Poll task queue in a loop. Runs in a background daemon thread."""
    import time

    per_tick = None
    if max_tasks_per_tick is not None:
        try:
            value = int(max_tasks_per_tick)
            if value > 0:
                per_tick = value
        except (TypeError, ValueError):
            per_tick = 1

    sleep_s = interval_ms / 1000.0
    while True:
        try:
            main_executor.process_tasks(max_tasks=per_tick)
        except Exception as e:  # task pump must not crash
            logger.error(f"Task pump tick failed: {e}")
        time.sleep(sleep_s)


def _install_pyrunner(main_executor, interrupt_check_period, logger):
    """Install a YADE PyRunner engine for interrupt checking during
    simulation.

    While ``O.run()`` is live, YADE's C++ sim loop calls this tick on a
    ``Dummy-N`` (boost::python) thread every ``interrupt_check_period``
    iterations. The tick's only job is to observe the interrupt flag
    and call ``O.pause()`` — the Python side then raises
    ``InterruptedError`` after ``O.run()`` returns.

    The tick deliberately does NOT pump ``main_executor`` tasks. Older
    versions did, to minimize ``execute_code`` latency during sim. But
    that makes user-supplied code execute on ``Dummy-N``, which
    ``is_safe_to_async_raise`` refuses to inject into (boost::python
    stack → C++ FATAL on exception). A misbehaving REPL ``while True``
    on ``Dummy-N`` would then freeze the sim, block the task's
    ``O.wait()``, and be unrecoverable because the script thread sits
    in released-GIL C code where the queued ``TaskInterrupt`` never
    fires. Confining ``execute_code`` execution to the pump thread —
    which ``is_safe_to_async_raise`` accepts — keeps async abort
    viable at the cost of ~20ms (``pump_interval``) extra REPL latency.

    Args:
        main_executor: MainThreadExecutor instance. Not used for
            queue pumping here (see note above); kept in signature for
            API stability.
        interrupt_check_period: Check every N iterations. Set to 1 for every step.

    Returns:
        True if PyRunner was installed successfully.
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

    from .signals import is_interrupt_requested, park_if_pause_wanted, repl_holds_sim

    # Flag checked after O.run() returns
    _interrupt_triggered = {"value": False}

    def _mcp_pyrunner_tick():
        # Interrupt flag check only — do NOT process the main_executor
        # queue here. See _install_pyrunner docstring for why.
        # Instead of raising an exception inside PyRunner (which causes
        # YADE's C++ layer to log a FATAL ERROR), we just pause the
        # simulation. The hooked O.run() will check the flag after
        # O.run() returns and raise InterruptedError at the Python level.
        if is_interrupt_requested():
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
        "period": int(interrupt_check_period),
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
            # repl_holds_sim() is False there and the task is unaffected.
            if repl_holds_sim():
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
        logger.info(
            f"PyRunner installed at O.engines[0] (iterPeriod={interrupt_check_period}) — interrupt check + task queue processing"
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to install PyRunner: {e}")
        return False


def start(
    host="localhost",
    port=9002,
    timer_interval_ms=DEFAULT_TIMER_INTERVAL_MS,
    max_tasks_per_tick=DEFAULT_MAX_TASKS_PER_TICK,
    interrupt_check_period=DEFAULT_INTERRUPT_CHECK_PERIOD,
    max_tasks=DEFAULT_MAX_TASKS,
    mode="auto",
):
    """Start the YADE Bridge server.

    Starts an HTTP + SSE server in a background thread, then starts the
    main-thread task pump.

    Args:
        host: Server host address.
        port: Server port number.
        timer_interval_ms: Timer/poll interval in milliseconds.
        max_tasks_per_tick: Max queued tasks handled per tick.
        interrupt_check_period: PyRunner checks interrupt every N iterations.
            Set to 1 for every step (default).
        max_tasks: Maximum number of tasks to retain. Oldest tasks (and
            their log files) are pruned when this limit is exceeded.
        mode: Task pump mode - "auto" (try Qt, fall back to blocking),
            "gui" (Qt only), or "console" (blocking only).
    """
    import atexit
    import logging
    import os
    import signal
    import socket
    import sys
    import threading

    if mode not in VALID_RUNTIME_MODES:
        raise ValueError(f"Invalid mode '{mode}'. Expected one of: {', '.join(VALID_RUNTIME_MODES)}")

    interval_ms = max(1, int(timer_interval_ms))

    # Logging setup
    bridge_dir = os.path.join(os.getcwd(), ".yade-mcp")
    if not os.path.exists(bridge_dir):
        os.makedirs(bridge_dir)
    log_file = os.path.join(bridge_dir, "bridge.log")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
    # stdout shows WARNING+ only (keeps the interactive prompt clean);
    # file handler keeps everything for post-mortem debugging.
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.WARNING)
    stdout_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)
    root_logger.addHandler(file_handler)
    logger = logging.getLogger("YADE-Bridge")

    # Server components
    from .execution import MainThreadExecutor
    from .server import create_server

    main_executor = MainThreadExecutor()

    # Install PyRunner for interrupt checking + task queue processing during simulation.
    # _install_pyrunner logs its own failure warning; ignore return value here.
    _install_pyrunner(main_executor, interrupt_check_period, logger)

    # Port availability check (SO_REUSEADDR handles crash/restart scenarios)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        raise RuntimeError(f"Port {port} is already in use. Try: yade_mcp_bridge.start(port={port + 1})") from exc
    finally:
        sock.close()

    # Create the HTTP + SSE server (binds the socket eagerly, so a port
    # conflict raises here on the main thread before the serving thread starts)
    yade_server = create_server(
        main_executor=main_executor,
        host=host,
        port=port,
        runtime_mode=mode,
        max_tasks=max_tasks,
    )

    # Install IPython hooks for console history capture
    from .console import ConsoleCapture

    console_capture = ConsoleCapture(yade_server.console_history)
    console_capture.install()

    def run_server_background():
        try:
            yade_server.serve_forever()
        except Exception as e:
            logger.error(f"Server error: {e}")
            import traceback

            traceback.print_exc()

    server_thread = threading.Thread(target=run_server_background, daemon=True)
    server_thread.start()

    if not server_thread.is_alive():
        raise RuntimeError("Bridge server thread failed to start")

    # Graceful shutdown — must be idempotent (signal + atexit may both fire)
    _shutdown_done = {"value": False}

    def _shutdown():
        if _shutdown_done["value"]:
            return
        _shutdown_done["value"] = True
        logger.info("Bridge shutting down...")
        yade_server.shutdown()

    atexit.register(_shutdown)

    # SIGTERM/SIGINT handler: atexit doesn't run when the main thread is
    # blocked (e.g. time.sleep inside a task). Explicit signal handling
    # ensures cleanup always happens.
    def _signal_handler(signum, _frame):
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, shutting down...", sig_name)
        _shutdown()
        # Restore default handler and re-raise so the OS terminates the
        # process normally.  This lets the shell detect signal death and
        # restore terminal settings (echo, line mode, etc.).
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    print(f"YADE MCP Bridge on http://{host}:{port}, log: {log_file}")

    # Main-thread task pump
    use_qt = mode in ("auto", "gui")
    use_blocking = mode in ("auto", "console")

    if use_qt and _start_qt_pump(main_executor, interval_ms, max_tasks_per_tick, logger):
        yade_server.set_runtime_mode("gui")
        logger.info(
            "Task pump running via Qt timer (interval=%dms, max_tasks_per_tick=%d)", interval_ms, max_tasks_per_tick
        )
        return

    if mode == "gui":
        raise RuntimeError("Qt is not available; cannot start in gui mode")

    if use_blocking:
        yade_server.set_runtime_mode("console")
        pump_thread = threading.Thread(
            target=_run_background_pump,
            args=(main_executor, interval_ms, max_tasks_per_tick, logger),
            daemon=True,
            name="mcp-task-pump",
        )
        pump_thread.start()
        logger.info("Task pump running via background thread (interval=%dms)", interval_ms)
