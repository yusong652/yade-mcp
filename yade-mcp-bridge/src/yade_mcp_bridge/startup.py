# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Bridge entry point: ``start()`` brings up all components.

- logging (file + stdout)
- PyRunner interrupt hook
- HTTP + SSE server on a background thread
- console capture and shutdown hooks
- execute_task worker (runs queued scripts one at a time)
- execute_code pump (Qt timer in gui mode, daemon thread in console mode)
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
from .execution import CodeExecutor, TaskExecutor
from .paths import DATA_DIR
from .runtime import installPyrunner, startBackgroundPump, startQtPump
from .transport import createServer
from .utils.safeLogging import GapFreeFileHandler, GapFreeStreamHandler

VALID_RUNTIME_MODES = ("auto", "gui", "console")


def _checkPortFree(host, port):
    """Fail fast on the main thread if the port is taken."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        raise RuntimeError(f"Port {port} is already in use. Try: {__package__}.start(port={port + 1})") from exc
    finally:
        sock.close()


def _setupLogging():
    """Configure the bridge's own logger and return (logger, log file path)."""
    bridgeDir = os.path.join(os.getcwd(), DATA_DIR)
    if not os.path.exists(bridgeDir):
        os.makedirs(bridgeDir)
    logFile = os.path.join(bridgeDir, "bridge.log")

    formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
    stdoutHandler = GapFreeStreamHandler(sys.stdout)
    stdoutHandler.setLevel(logging.WARNING)
    stdoutHandler.setFormatter(formatter)
    fileHandler = GapFreeFileHandler(logFile, mode="w", encoding="utf-8")
    fileHandler.setFormatter(formatter)

    # Only the bridge's named logger — the root logger belongs to the host
    # process (user scripts, IPython).
    logger = logging.getLogger("MCP-Bridge")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()  # start() called again: replace our handlers, not stack them
    logger.addHandler(stdoutHandler)
    logger.addHandler(fileHandler)

    return logger, logFile


def _startServerThread(bridgeServer, logger):
    """Serve the bridge on a daemon thread."""

    def run():
        try:
            bridgeServer.serve_forever()
        except Exception as e:
            logger.error(f"Server error: {e}")
            traceback.print_exc()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    if not thread.is_alive():
        raise RuntimeError("Bridge server thread failed to start")


def _installShutdown(bridgeServer, logger):
    """Register idempotent shutdown on atexit and SIGTERM."""
    done = {"value": False}

    def shutdown():
        if done["value"]:
            return
        done["value"] = True
        logger.info("Bridge shutting down...")
        bridgeServer.shutdown()

    atexit.register(shutdown)

    def handler(signum, _frame):
        logger.info("Received %s, shutting down...", signal.Signals(signum).name)
        shutdown()
        # Re-raise under the default handler so the OS terminates normally
        # and the shell can restore terminal settings (echo, line mode).
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, handler)


def _startPump(codeExecutor, bridgeServer, logger, mode):
    """Start the execute_code pump and record the resolved runtime mode."""
    if mode in ("auto", "gui") and startQtPump(codeExecutor, logger):
        bridgeServer.setRuntimeMode("gui")
        logger.info("execute_code pump running via Qt timer")
        return

    if mode == "gui":
        raise RuntimeError("Qt is not available; cannot start in gui mode")

    if mode in ("auto", "console") and startBackgroundPump(codeExecutor, logger):
        bridgeServer.setRuntimeMode("console")
        logger.info("execute_code pump running via background thread")


def start(
    host="localhost",
    port=9002,
    mode="auto",
):
    """Start the MCP bridge server.

    - check ``host``:``port`` is free (default localhost:9002)
    - configure logging and install the PyRunner interrupt hook
    - create the HTTP + SSE server and install console capture (user input)
    - run the server on a background thread, register shutdown hooks
    - start the execute_task worker (queued scripts, one at a time)
    - start the execute_code pump; ``mode`` selects it: "auto" tries a
      Qt timer and falls back to a blocking background thread, "gui"
      forces Qt, "console" forces blocking
    """
    if mode not in VALID_RUNTIME_MODES:
        raise ValueError(f"Invalid mode '{mode}'. Expected one of: {', '.join(VALID_RUNTIME_MODES)}")

    _checkPortFree(host, port)

    logger, logFile = _setupLogging()

    # installPyrunner logs its own failure warning; ignore the return value.
    installPyrunner(logger)

    codeExecutor = CodeExecutor()
    taskExecutor = TaskExecutor()
    bridgeServer = createServer(
        codeExecutor=codeExecutor, taskExecutor=taskExecutor, host=host, port=port, runtimeMode=mode
    )
    ConsoleCapture(bridgeServer.context.consoleHistory).install()

    _startServerThread(bridgeServer, logger)
    _installShutdown(bridgeServer, logger)

    print(f"YADE MCP Bridge on http://{host}:{port}, log: {logFile}")

    taskExecutor.start()
    _startPump(codeExecutor, bridgeServer, logger, mode)
