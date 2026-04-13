"""YADE MCP Bridge - WebSocket bridge for YADE DEM simulation.

Runs inside YADE's Python environment and exposes simulation control
as a remote WebSocket API for MCP clients.

Usage (inside YADE Python console):
    import yade_mcp_bridge
    yade_mcp_bridge.start()

Usage (batch/console mode):
    import yade_mcp_bridge
    yade_mcp_bridge.start(mode="console")
"""

__version__ = "0.2.3"

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
    """Install a YADE PyRunner engine for interrupt checking and task queue
    processing during simulation.

    This enables two critical features while O.run() is blocking the main thread:
    1. Interrupt checking — allows cancelling a running task
    2. Task queue processing — allows execute_code requests to be handled
       in between simulation steps

    Args:
        main_executor: MainThreadExecutor instance for queue processing.
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

    from .signals import is_interrupt_requested

    # Flag checked after O.run() returns
    _interrupt_triggered = {"value": False}

    def _mcp_pyrunner_tick():
        # Process pending tasks in queue (e.g. execute_code requests)
        main_executor.process_tasks(max_tasks=1)
        # Check interrupt flag for current task.
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
    _PYRUNNER_COMMAND = "_mcp_pyrunner_tick()"

    def _make_pyrunner():
        """Create a fresh PyRunner instance."""
        return _pyrunner_config["PyRunner"](
            command=_PYRUNNER_COMMAND,
            iterPeriod=_pyrunner_config["period"],
            dead=False,
        )

    def _has_our_pyrunner():
        """Check if our PyRunner is in O.engines."""
        return any(getattr(e, "command", None) == _PYRUNNER_COMMAND for e in O.engines)

    # Hook O.run() to auto-inject PyRunner before each simulation run.
    # This handles O.reset() clearing engines — our PyRunner gets
    # re-added transparently before simulation starts.
    # Guard against double-hooking if start() is called multiple times.
    if not getattr(O.run, "_mcp_hooked", False):
        _original_run = O.run

        def _hooked_run(*args, **kwargs):
            # Defend against __main__ replacement (e.g. IPython %run) between
            # bridge start and this O.run() call.
            _ensure_tick_in_main()
            if not _has_our_pyrunner():
                try:
                    O.engines = list(O.engines) + [_make_pyrunner()]
                    logger.info("PyRunner auto-injected before O.run()")
                except Exception as e:
                    logger.warning(f"PyRunner auto-injection failed: {e}")
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
        O.engines = list(O.engines) + [_make_pyrunner()]
        logger.info(
            f"PyRunner installed (iterPeriod={interrupt_check_period}) — interrupt check + task queue processing"
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to install PyRunner: {e}")
        return False


def start(
    host="localhost",
    port=9002,
    ping_interval=20,
    ping_timeout=40,
    timer_interval_ms=DEFAULT_TIMER_INTERVAL_MS,
    max_tasks_per_tick=DEFAULT_MAX_TASKS_PER_TICK,
    interrupt_check_period=DEFAULT_INTERRUPT_CHECK_PERIOD,
    max_tasks=DEFAULT_MAX_TASKS,
    mode="auto",
):
    """Start the YADE Bridge server.

    Starts a WebSocket server in a background thread, then starts the
    main-thread task pump.

    Args:
        host: Server host address.
        port: Server port number.
        ping_interval: Seconds between WebSocket ping frames.
        ping_timeout: Seconds to wait for pong before disconnect.
        timer_interval_ms: Timer/poll interval in milliseconds.
        max_tasks_per_tick: Max queued tasks handled per tick.
        interrupt_check_period: PyRunner checks interrupt every N iterations.
            Set to 1 for every step (default).
        max_tasks: Maximum number of tasks to retain. Oldest tasks (and
            their log files) are pruned when this limit is exceeded.
        mode: Task pump mode - "auto" (try Qt, fall back to blocking),
            "gui" (Qt only), or "console" (blocking only).
    """
    import asyncio
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
    for handler in [logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, mode="w", encoding="utf-8")]:
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
    logger = logging.getLogger("YADE-Bridge")

    # Server components
    from .execution import MainThreadExecutor
    from .server import create_server

    main_executor = MainThreadExecutor()

    # Install PyRunner for interrupt checking + task queue processing during simulation
    pyrunner_ok = _install_pyrunner(main_executor, interrupt_check_period, logger)

    # Port availability check (SO_REUSEADDR handles crash/restart scenarios)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        raise RuntimeError(f"Port {port} is already in use. Try: yade_mcp_bridge.start(port={port + 1})") from exc
    finally:
        sock.close()

    # Start WebSocket server in background thread
    yade_server = create_server(
        main_executor=main_executor,
        host=host,
        port=port,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        runtime_mode=mode,
        max_tasks=max_tasks,
    )

    # Install IPython hooks for console history capture
    from .console import ConsoleCapture

    console_capture = ConsoleCapture(yade_server.console_history)
    capture_ok = console_capture.install()

    def run_server_background():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(yade_server.start())
            loop.run_forever()
        except Exception as e:
            logger.error(f"Server error: {e}")
            import traceback

            traceback.print_exc()
        finally:
            loop.close()

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

    # Status display
    print("\n" + "=" * 60)
    print("YADE MCP Bridge Server")
    print("=" * 60)
    print(f"  URL:         ws://{host}:{port}")
    print(f"  Log:         {log_file}")
    print(f"  PyRunner:    {f'installed (period={interrupt_check_period})' if pyrunner_ok else 'not available'}")
    print(f"  Console:     {'capture active' if capture_ok else 'capture not available'}")
    print("=" * 60 + "\n")

    # Main-thread task pump
    use_qt = mode in ("auto", "gui")
    use_blocking = mode in ("auto", "console")

    if use_qt and _start_qt_pump(main_executor, interval_ms, max_tasks_per_tick, logger):
        yade_server.set_runtime_mode("gui")
        print(f"Task loop running via Qt timer (interval={interval_ms}ms, max_tasks_per_tick={max_tasks_per_tick})")
        print("Bridge started in non-blocking mode (GUI remains responsive).")
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
        # Remove stdout handler so background log messages don't
        # interfere with the interactive console prompt.
        # Logs are still written to bridge.log.
        for h in root_logger.handlers[:]:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                root_logger.removeHandler(h)
        print(f"Task loop running via background thread (interval={interval_ms}ms)")
        print(f"Bridge started. Console is available. Logs: {log_file}\n")
