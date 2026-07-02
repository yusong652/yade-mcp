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

    def __init__(self, logPath, bufferSize=8192):
        self._path = logPath
        self._closed = False

        logDir = os.path.dirname(logPath)
        if logDir and not os.path.exists(logDir):
            os.makedirs(logDir)

        self._file = open(logPath, "w", encoding="utf-8", buffering=bufferSize)
        logger.debug(f"FileBuffer created: {logPath}")

    def write(self, s):
        if self._closed or not s:
            return 0
        self._file.write(s)
        return len(s)

    def getvalue(self):
        self._ensureFlushed()

        if not os.path.exists(self._path):
            return ""

        try:
            with open(self._path, encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            logger.warning(f"Failed to read output file: {e}")
            return ""

    def getPath(self):
        return self._path

    def flush(self):
        if not self._closed:
            self._file.flush()

    def close(self):
        if not self._closed:
            self._file.close()
            self._closed = True

    def _ensureFlushed(self):
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

    def __init__(self, terminal, fileBuffer):
        self._terminal = terminal
        self._fileBuffer = fileBuffer

    def write(self, s):
        if not s:
            return 0
        self._fileBuffer.write(s)
        if self._terminal is not None:
            try:
                self._terminal.write(s)
                self._terminal.flush()
            except (ValueError, OSError):
                pass
        return len(s)

    def flush(self):
        self._fileBuffer.flush()
        if self._terminal is not None:
            try:
                self._terminal.flush()
            except (ValueError, OSError):
                pass

    def isatty(self):
        return False
