# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Bridge startup and process lifecycle.

``start()`` is the package's single entry point. It composes the helpers
below: configure logging, install the PyRunner interrupt hook, bring up
the HTTP + SSE server on a background thread, wire console capture and
graceful shutdown, then start the execute_code pump that drains queued
requests (Qt timer in gui mode, daemon thread in console mode).
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
from .execution import SerialExecutor
from .paths import DATA_DIR
from .runtime import install_pyrunner, start_background_pump, start_qt_pump
from .transport import create_server
from .utils.safe_logging import GapFreeFileHandler, GapFreeStreamHandler

VALID_RUNTIME_MODES = ("auto", "gui", "console")


def _setup_logging():
    """Configure root logging and return (logger, log_file path).

    stdout shows WARNING+ only (keeps the interactive prompt clean); the
    file handler keeps everything for post-mortem debugging.
    """
    bridge_dir = os.path.join(os.getcwd(), DATA_DIR)
    if not os.path.exists(bridge_dir):
        os.makedirs(bridge_dir)
    log_file = os.path.join(bridge_dir, "bridge.log")

    formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
    stdout_handler = GapFreeStreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.WARNING)
    stdout_handler.setFormatter(formatter)
    file_handler = GapFreeFileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(stdout_handler)
    root_logger.addHandler(file_handler)

    return logging.getLogger("MCP-Bridge"), log_file


def _check_port_free(host, port):
    """Fail fast on the main thread if the port is taken.

    SO_REUSEADDR handles crash/restart scenarios.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        raise RuntimeError(f"Port {port} is already in use. Try: {__package__}.start(port={port + 1})") from exc
    finally:
        sock.close()


def _start_server_thread(bridge_server, logger):
    """Serve the bridge on a daemon thread."""

    def run():
        try:
            bridge_server.serve_forever()
        except Exception as e:
            logger.error(f"Server error: {e}")
            traceback.print_exc()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    if not thread.is_alive():
        raise RuntimeError("Bridge server thread failed to start")


def _install_shutdown(bridge_server, logger):
    """Register idempotent shutdown on atexit and SIGTERM/SIGINT.

    atexit alone is not enough: it doesn't run while the main thread is
    blocked (e.g. time.sleep inside a task), so signals are handled too.
    """
    done = {"value": False}

    def shutdown():
        if done["value"]:
            return
        done["value"] = True
        logger.info("Bridge shutting down...")
        bridge_server.shutdown()

    atexit.register(shutdown)

    def handler(signum, _frame):
        logger.info("Received %s, shutting down...", signal.Signals(signum).name)
        shutdown()
        # Re-raise under the default handler so the OS terminates normally
        # and the shell can restore terminal settings (echo, line mode).
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _start_pump(executor, bridge_server, logger, mode):
    """Start the execute_code pump and record the resolved runtime mode."""
    if mode in ("auto", "gui") and start_qt_pump(executor, logger):
        bridge_server.set_runtime_mode("gui")
        logger.info("execute_code pump running via Qt timer")
        return

    if mode == "gui":
        raise RuntimeError("Qt is not available; cannot start in gui mode")

    if mode in ("auto", "console") and start_background_pump(executor, logger):
        bridge_server.set_runtime_mode("console")
        logger.info("execute_code pump running via background thread")


def start(
    host="localhost",
    port=9002,
    mode="auto",
):
    """Start the MCP bridge server.

    Brings up an HTTP + SSE server on a background thread (``host``:``port``,
    default localhost:9002), then drains queued execute_code work via a
    pump. ``mode`` selects the pump: "auto" tries a Qt timer and falls back
    to a blocking background thread, "gui" forces Qt, "console" forces
    blocking.
    """
    if mode not in VALID_RUNTIME_MODES:
        raise ValueError(f"Invalid mode '{mode}'. Expected one of: {', '.join(VALID_RUNTIME_MODES)}")

    logger, log_file = _setup_logging()

    # install_pyrunner logs its own failure warning; ignore the return value.
    install_pyrunner(logger)

    _check_port_free(host, port)

    executor = SerialExecutor()
    bridge_server = create_server(executor=executor, host=host, port=port, runtime_mode=mode)
    ConsoleCapture(bridge_server.context.console_history).install()

    _start_server_thread(bridge_server, logger)
    _install_shutdown(bridge_server, logger)

    print(f"YADE MCP Bridge on http://{host}:{port}, log: {log_file}")

    _start_pump(executor, bridge_server, logger, mode)
