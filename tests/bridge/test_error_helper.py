"""Tests for the shared execution error formatter.

`format_execution_error` is the one-and-only truth about how user-code
exceptions get packaged for the LLM — filtering out YADE/bridge frames,
capping the inline traceback, and handing off to an overflow log when
the excerpt is truncated. Both execute_code and execute_task depend on
it, so the behaviour gets exercised independently here.
"""



from yade_mcp_bridge.execution.errors import (
    TRACEBACK_MAX_LINES,
    format_execution_error,
)


def _raise_at(depth: int, filename: str = "<user>") -> BaseException:
    """Raise a NameError at a synthetic frame tagged with ``filename``.

    Builds a chain of `depth` user frames (via `compile`+`exec` so
    co_filename matches) so we can verify frame filtering and truncation.
    """
    src = "\n".join(
        [f"def f{i}(): return f{i+1}()" for i in range(depth - 1)] + [f"def f{depth-1}(): return missing"] + ["f0()"]
    )
    code = compile(src, filename, "exec")
    ns: dict = {}
    exec(code, ns)  # noqa: S102 — test intentionally runs synthetic code
    raise AssertionError("unreachable")


def test_user_frames_only_in_message():
    try:
        _raise_at(3, filename="my_script.py")
    except NameError as e:
        payload = format_execution_error(
            e,
            output_text="",
            is_user_frame=lambda fn: fn == "my_script.py",
            display_path="my_script.py",
        )
    assert payload["status"] == "error"
    assert payload["exception_type"] == "NameError"
    assert "my_script.py" in payload["message"]
    # Frames from the test harness / helper itself must be excluded.
    assert "test_error_helper" not in payload["message"]
    assert "_raise_at" not in payload["message"]


def test_short_traceback_not_truncated():
    try:
        raise ValueError("boom")
    except ValueError as e:
        payload = format_execution_error(
            e,
            output_text="before crash\n",
            is_user_frame=lambda fn: True,
        )
    assert "traceback_truncated" not in payload
    assert "log_file" not in payload
    assert payload["output"] == "before crash\n"
    # Traceback excerpt must carry the exception summary line at the end.
    assert "ValueError: boom" in payload["traceback"]


def test_long_traceback_truncated_and_writes_log(tmp_path):
    written = {}

    def writer(full_tb: str) -> str:
        path = tmp_path / "tb.log"
        path.write_text(full_tb)
        written["full"] = full_tb
        return str(path)

    # Synthesize a traceback taller than the cap.
    try:
        _raise_at(TRACEBACK_MAX_LINES + 5, filename="deep.py")
    except NameError as e:
        payload = format_execution_error(
            e,
            output_text="",
            is_user_frame=lambda fn: fn == "deep.py",
            overflow_writer=writer,
        )

    assert payload.get("traceback_truncated") is True
    assert payload["log_file"] == str(tmp_path / "tb.log")
    excerpt_lines = payload["traceback"].splitlines()
    # Header line + capped body.
    assert len(excerpt_lines) <= TRACEBACK_MAX_LINES + 1
    assert "earlier frames omitted" in excerpt_lines[0]
    # Last line of the full tb (the exception summary) survives.
    assert written["full"].splitlines()[-1] in payload["traceback"]


def test_overflow_writer_failure_is_swallowed():
    def broken(_full_tb: str) -> str:
        raise OSError("disk full")

    try:
        _raise_at(TRACEBACK_MAX_LINES + 5, filename="deep.py")
    except NameError as e:
        payload = format_execution_error(
            e,
            output_text="",
            is_user_frame=lambda fn: fn == "deep.py",
            overflow_writer=broken,
        )

    # Excerpt must survive even when the overflow writer blows up;
    # log_file just gets omitted.
    assert payload.get("traceback_truncated") is True
    assert "log_file" not in payload


def test_recursion_frames_collapsed_in_message():
    """Deep recursion must not flood the message with hundreds of
    identical frame lines. Mirrors CPython's own repeat collapsing."""
    import sys as _sys

    _sys.setrecursionlimit(80)

    def rec():
        return rec()

    try:
        rec()
    except RecursionError as e:
        payload = format_execution_error(
            e,
            output_text="",
            is_user_frame=lambda fn: __file__ == fn,
            display_path="test.py",
        )
    finally:
        _sys.setrecursionlimit(1000)

    msg = payload["message"]
    # Exactly one frame line for `rec` + a single "Previous line repeated"
    # summary, not dozens of identical "File ... line ... in rec" lines.
    assert msg.count("in rec") == 1
    assert "[Previous line repeated" in msg


def test_no_user_frames_falls_back_to_short_message():
    try:
        raise RuntimeError("opaque")
    except RuntimeError as e:
        payload = format_execution_error(
            e,
            output_text="",
            is_user_frame=lambda fn: False,  # nothing counts as user code
            message_prefix="Execution failed",
        )
    assert payload["message"] == "Execution failed: RuntimeError: opaque"
