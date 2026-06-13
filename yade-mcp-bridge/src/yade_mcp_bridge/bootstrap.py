# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Bridge startup: logging, preflight checks, wiring and process lifecycle.

``start()`` is the package's single entry point. It configures logging,
installs the simulation-side PyRunner hook, creates the HTTP + SSE server
on a background thread, wires console capture and graceful shutdown
(atexit + signals), and finally starts the task pump that executes queued
main-thread work (Qt timer in gui mode, daemon thread in console mode).
"""

import atexit
import logging
import os
import signal
import socket
import sys
import threading
import traceback

from .console import ConsoleCapture
from .execution import MainThreadExecutor
from .pump import run_background_pump, start_qt_pump
from .pyrunner import install_pyrunner
from .server import create_server

DEFAULT_INTERRUPT_CHECK_PERIOD = 1
DEFAULT_MAX_TASKS = 1024
VALID_RUNTIME_MODES = ("auto", "gui", "console")


def start(
    host="localhost",
    port=9002,
    interrupt_check_period=DEFAULT_INTERRUPT_CHECK_PERIOD,
    max_tasks=DEFAULT_MAX_TASKS,
    mode="auto",
):
    """Start the MCP bridge server.

    Brings up an HTTP + SSE server on a background thread (``host``:``port``,
    default localhost:9002), then drives queued main-thread work via a task
    pump. ``mode`` selects the pump: "auto" tries a Qt timer and falls back
    to a blocking background thread, "gui" forces Qt, "console" forces
    blocking.

    ``interrupt_check_period`` is how often, in simulation iterations, the
    PyRunner observes the interrupt flag during ``O.run()`` — 1 means every
    step. ``max_tasks`` bounds task retention; the oldest tasks and their
    log files are pruned past the limit.
    """
    if mode not in VALID_RUNTIME_MODES:
        raise ValueError(f"Invalid mode '{mode}'. Expected one of: {', '.join(VALID_RUNTIME_MODES)}")

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

    main_executor = MainThreadExecutor()

    # Install PyRunner for interrupt checking during simulation.
    # install_pyrunner logs its own failure warning; ignore return value here.
    install_pyrunner(main_executor, interrupt_check_period, logger)

    # Port availability check (SO_REUSEADDR handles crash/restart scenarios)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        raise RuntimeError(f"Port {port} is already in use. Try: {__package__}.start(port={port + 1})") from exc
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
    console_capture = ConsoleCapture(yade_server.context.console_history)
    console_capture.install()

    def run_server_background():
        try:
            yade_server.serve_forever()
        except Exception as e:
            logger.error(f"Server error: {e}")
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

    if use_qt and start_qt_pump(main_executor, logger):
        yade_server.set_runtime_mode("gui")
        logger.info("Task pump running via Qt timer")
        return

    if mode == "gui":
        raise RuntimeError("Qt is not available; cannot start in gui mode")

    if use_blocking:
        yade_server.set_runtime_mode("console")
        pump_thread = threading.Thread(
            target=run_background_pump,
            args=(main_executor, logger),
            daemon=True,
            name="mcp-task-pump",
        )
        pump_thread.start()
        logger.info("Task pump running via background thread")
