# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Shared error formatting for user-code execution paths.

Both ``execute_code`` (synchronous REPL) and ``execute_task`` (async
script) catch user-code exceptions and need the same treatment:

* Filter the traceback to **user frames only** (YADE/bridge internals
  are noise to the LLM).
* Build a concise human message pinpointing file + line.
* Carry the full traceback, but bounded — truncate to the most recent
  N lines and fall back to a log-file pointer for the rest.

This module owns those decisions so the two execution paths stay
consistent. Each caller provides:

* ``is_user_frame``: predicate on a frame's filename — true when the
  frame belongs to user code (not YADE / bridge internals).
* ``display_path``: how user files should be rendered in the message
  (e.g. YADE's ``path_to_llm_format`` for scripts, ``<execute_code>``
  for REPL snippets).
* ``overflow_writer``: optional callable invoked only when the
  traceback exceeds the inline cap. It receives the full traceback
  text, persists it, and returns an absolute path. Callers that do
  not want to persist anything can omit it.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any, Callable


class BridgeTimeout(BaseException):
    """Injected via PyThreadState_SetAsyncExc to terminate a timed-out
    execute_code body.

    Inherits ``BaseException`` (not ``Exception``) so user-code handlers
    of the form ``except Exception:`` cannot swallow it. ``repl.py``'s
    ``_run_code`` catches it explicitly and returns a
    ``status="terminated"`` marker — this exception must never escape
    ``_run_code``, or ``SerialExecutor.run_next``'s
    ``except Exception`` (which does NOT catch BaseException) will let
    it kill the pump thread permanently.
    """


class TaskInterrupt(BaseException):
    """Injected via PyThreadState_SetAsyncExc to force-terminate a
    script task that the flag-based interrupt path cannot reach —
    e.g. a pure-Python ``while True`` loop with no ``O.run`` on the
    stack, so no PyRunner tick ever fires to observe the flag.

    Inherits ``BaseException`` (not ``Exception``) so ``except
    Exception:`` in user code will NOT swallow it. ``ScriptRunner._execute``
    catches it explicitly and returns ``status="interrupted"`` with a
    sim-state cleanup (``O.pause`` + ``O.wait``) so the next task
    inherits a clean YADE state.

    Must never escape ``_execute`` — the script thread is a
    ``threading.Thread`` with no default ``except BaseException``
    barrier, so a leak would terminate the thread and leave the
    task future in an exception state instead of ``interrupted``.
    """


# Cap the inline traceback excerpt. 80 lines comfortably covers most
# user-code stacks (incl. YADE's boost-Python wrappers, which
# occasionally produce 30-50 frame chains). Anything longer is
# available via the overflow log file.
TRACEBACK_MAX_LINES = 80


def _extract_user_frames(
    is_user_frame: Callable[[str], bool],
) -> list[tuple[int, str]]:
    """Walk the current exception traceback, returning (lineno, name)
    tuples for frames accepted by ``is_user_frame``.
    """
    frames: list[tuple[int, str]] = []
    tb = sys.exc_info()[2]
    while tb is not None:
        code = tb.tb_frame.f_code
        if is_user_frame(code.co_filename):
            frames.append((tb.tb_lineno, code.co_name))
        tb = tb.tb_next
    return frames


def _build_user_message(
    exc: BaseException,
    frames: list[tuple[int, str]],
    display_path: str,
    prefix: str,
) -> str:
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


def _cap_traceback(full_tb: str) -> tuple[str, bool]:
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
    exc: BaseException,
    output_text: str,
    *,
    is_user_frame: Callable[[str], bool],
    display_path: str = "<code>",
    message_prefix: str = "Execution failed",
    overflow_writer: Callable[[str], str] | None = None,
) -> dict[str, Any]:
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

    payload: dict[str, Any] = {
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
