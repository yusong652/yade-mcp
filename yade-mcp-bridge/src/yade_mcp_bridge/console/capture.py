"""IPython event hooks for capturing user console input/output."""

import gc
import logging
import sys
import threading
from io import StringIO

logger = logging.getLogger("YADE-Bridge")


class CaptureBuffer:
    """Stdout wrapper that captures output while still displaying it."""

    def __init__(self, terminal):
        self._terminal = terminal
        self._buffer = StringIO()

    def write(self, s):
        self._buffer.write(s)
        if self._terminal is not None:
            try:
                self._terminal.write(s)
            except (ValueError, OSError):
                pass
        return len(s)

    def flush(self):
        if self._terminal is not None:
            try:
                self._terminal.flush()
            except (ValueError, OSError):
                pass

    def getvalue(self):
        return self._buffer.getvalue()

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    def isatty(self):
        return False


class ConsoleCapture:
    """Hooks into IPython to capture user input/output.

    Only captures user-initiated execution, not MCP-initiated
    (which goes through execute_code handler, bypassing IPython).
    """

    def __init__(self, history):
        self._history = history
        self._capture_buffer = None
        self._old_stdout = None

    def install(self):
        """Register IPython event hooks.

        YADE creates its shell after start() returns, so we poll
        in a background thread until the shell appears.
        """
        ip = _find_ipython_shell()
        if ip is not None:
            self._register(ip)
            return

        def _wait():
            import time
            for _ in range(120):
                time.sleep(0.5)
                ip = _find_ipython_shell()
                if ip is not None:
                    self._register(ip)
                    return
            logger.info("IPython shell not found, console capture disabled")

        threading.Thread(target=_wait, daemon=True, name="mcp-capture-init").start()
        logger.info("Waiting for IPython shell to initialize...")

    def _register(self, ip):
        ip.events.register("pre_run_cell", self._pre_run_cell)
        ip.events.register("post_run_cell", self._post_run_cell)
        logger.info("Console capture hooks installed on %s", type(ip).__name__)

    def _pre_run_cell(self, info):
        """Start capturing stdout before cell execution."""
        self._old_stdout = sys.stdout
        terminal = sys.__stdout__ if sys.__stdout__ is not None else self._old_stdout
        self._capture_buffer = CaptureBuffer(terminal)
        sys.stdout = self._capture_buffer

    def _post_run_cell(self, result):
        """Record entry after cell execution."""
        output = ""
        if self._capture_buffer is not None:
            output = self._capture_buffer.getvalue()
        if self._old_stdout is not None:
            sys.stdout = self._old_stdout
        self._capture_buffer = None
        self._old_stdout = None

        try:
            self._history.add(
                input_text=result.info.raw_cell,
                output=output,
                result=result.result,
                success=result.success,
            )
        except Exception as e:
            logger.error("Failed to record console entry: %s", e)


def _find_ipython_shell():
    """Find the active IPython shell instance.

    YADE uses InteractiveShellEmbed without registering get_ipython()
    or the singleton _instance, so we scan gc as a last resort.
    """
    try:
        from IPython import get_ipython
        ip = get_ipython()
        if ip is not None:
            return ip
    except ImportError:
        return None

    try:
        from IPython.core.interactiveshell import InteractiveShell
        for obj in gc.get_objects():
            try:
                if isinstance(obj, InteractiveShell):
                    return obj
            except Exception:
                pass
    except Exception:
        pass

    return None
