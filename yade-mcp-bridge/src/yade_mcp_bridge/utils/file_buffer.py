"""File Buffer - Disk-based output capture for task execution.

Provides a file-like buffer that writes directly to disk, ensuring
complete output preservation for long-running simulations.
"""

import logging
import os

# Module logger
logger = logging.getLogger("YADE-Bridge")

# Default tail size for real-time monitoring (100KB)
DEFAULT_TAIL_SIZE = 100_000


class FileBuffer:
    """A file-like buffer that writes output directly to disk.

    Designed for capturing stdout from long-running YADE simulations.
    Uses Python's file buffering to batch writes efficiently.
    """

    def __init__(self, log_path, buffer_size=8192):
        self._path = log_path
        self._size = 0
        self._closed = False

        log_dir = os.path.dirname(log_path)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)

        self._file = open(log_path, 'w', encoding='utf-8', buffering=buffer_size)
        logger.debug(f"FileBuffer created: {log_path}")

    def write(self, s):
        if self._closed or not s:
            return 0
        self._file.write(s)
        self._size += len(s)
        return len(s)

    def getvalue(self, max_size=None):
        self._ensure_flushed()

        if not os.path.exists(self._path):
            return ""

        try:
            with open(self._path, encoding='utf-8') as f:
                if max_size is None or self._size <= max_size:
                    return f.read()
                return self._read_tail(f, max_size)
        except OSError as e:
            logger.warning(f"Failed to read output file: {e}")
            return ""

    def get_tail(self, tail_bytes=DEFAULT_TAIL_SIZE):
        return self.getvalue(max_size=tail_bytes)

    def _read_tail(self, f, max_size):
        seek_pos = max(0, self._size - max_size)
        f.seek(seek_pos)
        if seek_pos > 0:
            f.readline()
        content = f.read()
        truncated = self._size - len(content) - (seek_pos if seek_pos == 0 else 0)
        if truncated > 0:
            header = f"[... {self._format_bytes(truncated)} earlier bytes, showing recent output ...]\n\n"
            return header + content
        return content

    def get_size(self):
        return self._size

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

    @staticmethod
    def _format_bytes(num_bytes):
        if num_bytes < 1024:
            return f"{num_bytes} bytes"
        elif num_bytes < 1024 * 1024:
            return f"{num_bytes / 1024:.1f} KB"
        else:
            return f"{num_bytes / (1024 * 1024):.1f} MB"

    def readable(self):
        return False

    def writable(self):
        return not self._closed

    def seekable(self):
        return False

    def isatty(self):
        return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


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

    def getvalue(self, max_size=None):
        return self._file_buffer.getvalue(max_size)

    def get_tail(self, tail_bytes=DEFAULT_TAIL_SIZE):
        return self._file_buffer.get_tail(tail_bytes)

    def get_size(self):
        return self._file_buffer.get_size()

    def get_path(self):
        return self._file_buffer.get_path()

    def close(self):
        self._file_buffer.close()

    def readable(self):
        return False

    def writable(self):
        return self._file_buffer.writable()

    def seekable(self):
        return False

    def isatty(self):
        return False
