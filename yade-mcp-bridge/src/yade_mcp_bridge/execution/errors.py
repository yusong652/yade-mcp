# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Shared error formatting for code run via ``execute_code`` / ``execute_task``."""

import sys
import traceback

# Cap the inline traceback excerpt. 80 lines comfortably covers most
# user-code stacks (incl. YADE's boost-Python wrappers, which
# occasionally produce 30-50 frame chains). Anything longer is
# available via the overflow log file.
TRACEBACK_MAX_LINES = 80


def _extract_user_frames(is_user_frame):
    """Walk the current exception traceback, returning (lineno, name)
    tuples for frames accepted by ``is_user_frame``.
    """
    frames = []
    tb = sys.exc_info()[2]
    while tb is not None:
        code = tb.tb_frame.f_code
        if is_user_frame(code.co_filename):
            frames.append((tb.tb_lineno, code.co_name))
        tb = tb.tb_next
    return frames


def _build_user_message(exc, frames, display_path, prefix):
    """Render the multi-line pseudo-traceback shown to the LLM.

    Consecutive identical frames (recursion) collapse into a single
    entry with a repeat count — mirrors CPython's own
    ``[Previous line repeated N more times]`` behaviour so deep
    recursion doesn't flood the message with hundreds of identical
    lines.
    """
    if not frames:
        return f"{prefix}: {type(exc).__name__}: {exc}"
    parts = [f"{prefix}:\n"]
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
    parts.append(f"{type(exc).__name__}: {exc}")
    return "".join(parts)


def _cap_traceback(full_tb):
    """Truncate to the last ``TRACEBACK_MAX_LINES`` lines.

    Keeps the tail because Python's ``format_exc()`` puts the innermost
    (most recent) frame and the exception message at the bottom —
    that's the decision-relevant part for debugging.
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
    is_user_frame,
    display_path="<code>",
    message_prefix="Execution failed",
    overflow_writer=None,
):
    """Build a uniform error response for user-code execution failures.

    Returns a dict with:

    * ``status``: always ``"error"``
    * ``output``: stdout captured before the crash (caller-provided)
    * ``message``: multi-line human-readable pseudo-traceback
    * ``exception_type``: ``type(exc).__name__`` — LLM branching hook
    * ``traceback``: capped excerpt (at most ``TRACEBACK_MAX_LINES``)
    * ``traceback_truncated``: true when the excerpt is partial
    * ``log_file``: absolute path to the full traceback on disk — only
      present when the excerpt is truncated AND ``overflow_writer``
      was provided
    """
    full_tb = traceback.format_exc()
    frames = _extract_user_frames(is_user_frame)
    message = _build_user_message(exc, frames, display_path, message_prefix)
    excerpt, truncated = _cap_traceback(full_tb)

    payload = {
        "status": "error",
        "output": output_text,
        "message": message,
        "exception_type": type(exc).__name__,
        "traceback": excerpt,
    }
    if truncated:
        payload["traceback_truncated"] = True
        if overflow_writer is not None:
            try:
                payload["log_file"] = overflow_writer(full_tb)
            except Exception:
                # Overflow persistence is best-effort; caller gets the
                # excerpt regardless.
                pass
    return payload
