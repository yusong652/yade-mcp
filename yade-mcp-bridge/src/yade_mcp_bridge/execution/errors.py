# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Shared error formatting for code run via ``execute_code`` / ``execute_task``."""

import os
import sys
import time
import traceback

from ..paths import LOGS_DIR

# Error tag for execute_code snippets.
EXECUTE_CODE_TAG = "<execute_code>"

# Inline traceback excerpt cap; anything longer spills to the overflow log.
TRACEBACK_MAX_LINES = 80


def _extractFrames(keepFrame):
    """Walk the current exception traceback, returning (lineno, name)
    tuples for frames accepted by ``keepFrame``.
    """
    frames = []
    tb = sys.exc_info()[2]
    while tb is not None:
        code = tb.tb_frame.f_code
        if keepFrame(code.co_filename):
            frames.append((tb.tb_lineno, code.co_name))
        tb = tb.tb_next
    return frames


def _buildMessage(exc, frames, displayPath):
    """Render the selected frames as a Python-style traceback for the LLM.

    Consecutive identical frames collapse with a repeat count, like
    CPython's ``[Previous line repeated N more times]``.
    """
    excLine = f"{type(exc).__name__}: {exc}"
    if not frames:
        return excLine
    parts = ["Traceback (most recent call last):\n"]
    i = 0
    while i < len(frames):
        lineno, name = frames[i]
        repeats = 1
        while i + repeats < len(frames) and frames[i + repeats] == (lineno, name):
            repeats += 1
        parts.append(f'  File "{displayPath}", line {lineno}, in {name}\n')
        if repeats > 1:
            parts.append(f"  [Previous line repeated {repeats - 1} more times]\n")
        i += repeats
    parts.append(excLine)
    return "".join(parts)


def _capTraceback(fullTb):
    """Truncate to the last ``TRACEBACK_MAX_LINES`` lines — the tail holds the
    innermost frame and exception message, the decision-relevant part.
    """
    lines = fullTb.splitlines()
    if len(lines) <= TRACEBACK_MAX_LINES:
        return fullTb, False
    kept = lines[-TRACEBACK_MAX_LINES:]
    header = f"... ({len(lines) - TRACEBACK_MAX_LINES} earlier frames omitted; see log_file for full)\n"
    return header + "\n".join(kept), True


def formatExecutionError(
    exc,
    outputText,
    *,
    keepFrame,
    displayPath="<code>",
    overflowWriter=None,
):
    """Build a uniform error response for user-code execution failures."""
    fullTb = traceback.format_exc()
    frames = _extractFrames(keepFrame)
    message = _buildMessage(exc, frames, displayPath)
    excerpt, truncated = _capTraceback(fullTb)

    response = {
        "status": "error",
        "output": outputText,
        "message": message,
        "exception_type": type(exc).__name__,
        "traceback": excerpt,
    }
    if truncated:
        response["traceback_truncated"] = True
        if overflowWriter is not None:
            try:
                response["log_file"] = overflowWriter(fullTb)
            except Exception:
                # Overflow persistence is best-effort; caller gets the
                # excerpt regardless.
                pass
    return response


def isExecuteCodeFrame(filename):
    """Keep only the execute_code snippet's own frames, dropping bridge and
    boost-Python internals.
    """
    return filename == EXECUTE_CODE_TAG


def scriptFrameFilter(scriptPath):
    """Frame predicate for a task script: its own frames, plus ``<string>``
    frames — code run via ``exec``/``eval`` or a YADE PyRunner command,
    which boost-Python tags ``<string>``.
    """
    normalized = os.path.normpath(scriptPath)
    return lambda filename: os.path.normpath(filename) == normalized or filename == "<string>"


def logExecuteCodeOverflow(fullTb):
    """Persist a full traceback to its own log file."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    # Fresh timestamped file per error keeps this side-channel simple.
    path = os.path.join(LOGS_DIR, f"exec_code_error_{int(time.time() * 1000)}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(fullTb)
    return os.path.abspath(path)


def logExecuteTaskOverflow(fullTb, taskLogPath, taskId):
    """Append a full traceback to the task's log file."""
    # Append to the task's own log so it lands with stdout for paginated
    # check_task_status reads.
    if taskLogPath and os.path.isfile(taskLogPath):
        with open(taskLogPath, "a", encoding="utf-8") as f:
            f.write("\n--- traceback ---\n")
            f.write(fullTb)
        return os.path.abspath(taskLogPath)
    # Fallback: dedicated error log if the task log is gone.
    fallback = os.path.join(LOGS_DIR, f"task_{taskId}_error.log")
    os.makedirs(os.path.dirname(fallback), exist_ok=True)
    with open(fallback, "w", encoding="utf-8") as f:
        f.write(fullTb)
    return os.path.abspath(fallback)
