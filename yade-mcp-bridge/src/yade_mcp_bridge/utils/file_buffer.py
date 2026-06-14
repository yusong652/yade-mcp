# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""File Buffer - Disk-based output capture for task execution.

Provides a file-like buffer that writes directly to disk, ensuring
complete output preservation for long-running simulations.
"""

import logging
import os

# Module logger
logger = logging.getLogger("MCP-Bridge")


class FileBuffer:
    """A file-like buffer that writes output directly to disk.

    Designed for capturing stdout from long-running YADE simulations.
    Uses Python's file buffering to batch writes efficiently.
    """

    def __init__(self, log_path, buffer_size=8192):
        self._path = log_path
        self._closed = False

        log_dir = os.path.dirname(log_path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)

        self._file = open(log_path, "w", encoding="utf-8", buffering=buffer_size)
        logger.debug(f"FileBuffer created: {log_path}")

    def write(self, s):
        if self._closed or not s:
            return 0
        self._file.write(s)
        return len(s)

    def getvalue(self):
        self._ensure_flushed()

        if not os.path.exists(self._path):
            return ""

        try:
            with open(self._path, encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            logger.warning(f"Failed to read output file: {e}")
            return ""

    def get_path(self):
        return self._path

    def flush(self):
        if not self._closed:
            self._file.flush()

    def close(self):
        if not self._closed:
            self._file.close()
            self._closed = True

    def _ensure_flushed(self):
        if not self._closed and self._file:
            try:
                self._file.flush()
            except (ValueError, OSError):
                pass


class TeeBuffer:
    """Write to both the original stdout and a FileBuffer simultaneously.

    Acts as a transparent tee: task output appears in the YADE terminal
    AND is captured to disk for polling via check_task_status.
    """

    def __init__(self, terminal, file_buffer):
        self._terminal = terminal
        self._file_buffer = file_buffer

    def write(self, s):
        if not s:
            return 0
        self._file_buffer.write(s)
        if self._terminal is not None:
            try:
                self._terminal.write(s)
                self._terminal.flush()
            except (ValueError, OSError):
                pass
        return len(s)

    def flush(self):
        self._file_buffer.flush()
        if self._terminal is not None:
            try:
                self._terminal.flush()
            except (ValueError, OSError):
                pass

    def isatty(self):
        return False
