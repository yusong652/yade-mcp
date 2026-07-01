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


def _extract_frames(keep_frame):
    """Walk the current exception traceback, returning (lineno, name)
    tuples for frames accepted by ``keep_frame``.
    """
    frames = []
    tb = sys.exc_info()[2]
    while tb is not None:
        code = tb.tb_frame.f_code
        if keep_frame(code.co_filename):
            frames.append((tb.tb_lineno, code.co_name))
        tb = tb.tb_next
    return frames


def _build_message(exc, frames, display_path):
    """Render the selected frames as a Python-style traceback for the LLM.

    Consecutive identical frames collapse with a repeat count, like
    CPython's ``[Previous line repeated N more times]``.
    """
    exc_line = f"{type(exc).__name__}: {exc}"
    if not frames:
        return exc_line
    parts = ["Traceback (most recent call last):\n"]
    i = 0
    while i < len(frames):
        lineno, name = frames[i]
        repeats = 1
        while i + repeats < len(frames) and frames[i + repeats] == (lineno, name):
            repeats += 1
        parts.append(f'  File "{display_path}", line {lineno}, in {name}\n')
        if repeats > 1:
            parts.append(f"  [Previous line repeated {repeats - 1} more times]\n")
        i += repeats
    parts.append(exc_line)
    return "".join(parts)


def _cap_traceback(full_tb):
    """Truncate to the last ``TRACEBACK_MAX_LINES`` lines — the tail holds the
    innermost frame and exception message, the decision-relevant part.
    """
    lines = full_tb.splitlines()
    if len(lines) <= TRACEBACK_MAX_LINES:
        return full_tb, False
    kept = lines[-TRACEBACK_MAX_LINES:]
    header = f"... ({len(lines) - TRACEBACK_MAX_LINES} earlier frames omitted; see log_file for full)\n"
    return header + "\n".join(kept), True


def format_execution_error(
    exc,
    output_text,
    *,
    keep_frame,
    display_path="<code>",
    overflow_writer=None,
):
    """Build a uniform error response for user-code execution failures."""
    full_tb = traceback.format_exc()
    frames = _extract_frames(keep_frame)
    message = _build_message(exc, frames, display_path)
    excerpt, truncated = _cap_traceback(full_tb)

    response = {
        "status": "error",
        "output": output_text,
        "message": message,
        "exception_type": type(exc).__name__,
        "traceback": excerpt,
    }
    if truncated:
        response["traceback_truncated"] = True
        if overflow_writer is not None:
            try:
                response["log_file"] = overflow_writer(full_tb)
            except Exception:
                # Overflow persistence is best-effort; caller gets the
                # excerpt regardless.
                pass
    return response


# --- per-caller frame predicates (fed to ``keep_frame``) ---


def is_execute_code_frame(filename):
    """Keep only the execute_code snippet's own frames, dropping bridge and
    boost-Python internals.
    """
    return filename == EXECUTE_CODE_TAG


def script_frame_filter(script_path):
    """Frame predicate for a task script: its own frames, plus ``<string>``
    frames — code run via ``exec``/``eval`` or a YADE PyRunner command,
    which boost-Python tags ``<string>``.
    """
    normalized = os.path.normpath(script_path)
    return lambda filename: os.path.normpath(filename) == normalized or filename == "<string>"


# --- per-caller overflow writers (fed to ``overflow_writer``) ---


def write_code_overflow(full_tb):
    """Persist a full traceback to its own log file."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    # Fresh timestamped file per error keeps this side-channel simple.
    path = os.path.join(LOGS_DIR, f"exec_code_error_{int(time.time() * 1000)}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(full_tb)
    return os.path.abspath(path)


def task_overflow_writer(task_log_path, task_id):
    """Build an overflow writer that appends the traceback to the task's log."""

    def _write(full_tb):
        # Append to the task's own log so it lands with stdout for paginated
        # check_task_status reads.
        if task_log_path and os.path.isfile(task_log_path):
            with open(task_log_path, "a", encoding="utf-8") as f:
                f.write("\n--- traceback ---\n")
                f.write(full_tb)
            return os.path.abspath(task_log_path)
        # Fallback: dedicated error log if the task log is gone.
        fallback = os.path.join(LOGS_DIR, f"task_{task_id}_error.log")
        os.makedirs(os.path.dirname(fallback), exist_ok=True)
        with open(fallback, "w", encoding="utf-8") as f:
            f.write(full_tb)
        return os.path.abspath(fallback)

    return _write
