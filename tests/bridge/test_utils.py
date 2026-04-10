"""Tests for bridge utility modules."""

import os
import tempfile

from yade_mcp_bridge.utils.path_utils import path_to_llm_format
from yade_mcp_bridge.utils.response import TaskDataBuilder, build_response
from yade_mcp_bridge.utils.file_buffer import FileBuffer, TeeBuffer


class TestPathToLlmFormat:
    def test_forward_slashes_unchanged(self):
        assert path_to_llm_format("/home/user/script.py") == "/home/user/script.py"

    def test_backslashes_converted(self):
        assert path_to_llm_format("C:\\Users\\script.py") == "C:/Users/script.py"

    def test_mixed_slashes(self):
        assert path_to_llm_format("C:\\Users/project\\main.py") == "C:/Users/project/main.py"


class TestBuildResponse:
    def test_basic_response(self):
        resp = build_response("success", "done", {"task_id": "t1"})
        assert resp == {
            "status": "success",
            "message": "done",
            "data": {"task_id": "t1"},
        }


class TestTaskDataBuilder:
    def test_basic_build(self):
        data = TaskDataBuilder("t1", "script", "test.py", "/tmp/test.py", "desc").build()
        assert data["task_id"] == "t1"
        assert data["task_type"] == "script"
        assert data["script_name"] == "test.py"
        assert data["entry_script"] == "/tmp/test.py"
        assert data["description"] == "desc"

    def test_with_timing(self):
        data = (
            TaskDataBuilder("t1", "script", "s", "/s", "d")
            .with_timing(100.0, end_time=110.0, elapsed_time=10.0)
            .build()
        )
        assert data["start_time"] == 100.0
        assert data["end_time"] == 110.0
        assert data["elapsed_time"] == 10.0

    def test_with_timing_no_end(self):
        data = TaskDataBuilder("t1", "script", "s", "/s", "d").with_timing(100.0).build()
        assert data["start_time"] == 100.0
        assert "end_time" not in data

    def test_with_output(self):
        data = TaskDataBuilder("t1", "script", "s", "/s", "d").with_output("hello").build()
        assert data["output"] == "hello"

    def test_with_output_none_skipped(self):
        data = TaskDataBuilder("t1", "script", "s", "/s", "d").with_output(None).build()
        assert "output" not in data

    def test_with_pagination(self):
        pagination = {"total_lines": 42, "line_range": "1-42", "has_older": False, "has_newer": False}
        data = TaskDataBuilder("t1", "script", "s", "/s", "d").with_pagination(pagination).build()
        assert data["pagination"] == pagination

    def test_with_pagination_none_skipped(self):
        data = TaskDataBuilder("t1", "script", "s", "/s", "d").with_pagination(None).build()
        assert "pagination" not in data

    def test_with_result(self):
        data = TaskDataBuilder("t1", "script", "s", "/s", "d").with_result(42).build()
        assert data["result"] == 42

    def test_with_error(self):
        data = TaskDataBuilder("t1", "script", "s", "/s", "d").with_error("boom").build()
        assert data["error"] == "boom"

    def test_with_error_none_skipped(self):
        data = TaskDataBuilder("t1", "script", "s", "/s", "d").with_error(None).build()
        assert "error" not in data

    def test_chaining(self):
        data = (
            TaskDataBuilder("t1", "script", "s", "/s", "d")
            .with_timing(1.0)
            .with_output("out")
            .with_result("res")
            .build()
        )
        assert data["start_time"] == 1.0
        assert data["output"] == "out"
        assert data["result"] == "res"

    def test_build_returns_copy(self):
        builder = TaskDataBuilder("t1", "script", "s", "/s", "d")
        d1 = builder.build()
        d2 = builder.build()
        assert d1 == d2
        d1["extra"] = True
        assert "extra" not in d2


class TestFileBuffer:
    def test_write_and_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            buf = FileBuffer(path)
            buf.write("hello ")
            buf.write("world")
            content = buf.getvalue()
            assert content == "hello world"
            buf.close()

    def test_empty_buffer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            buf = FileBuffer(path)
            assert buf.getvalue() == ""
            buf.close()

    def test_get_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            buf = FileBuffer(path)
            buf.write("12345")
            assert buf.get_size() == 5
            buf.close()

    def test_get_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            buf = FileBuffer(path)
            assert buf.get_path() == path
            buf.close()

    def test_context_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            with FileBuffer(path) as buf:
                buf.write("test")
            assert not buf.writable()

    def test_write_after_close_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            buf = FileBuffer(path)
            buf.close()
            assert buf.write("data") == 0

    def test_tail_for_large_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            buf = FileBuffer(path)
            # Write enough data to trigger tail behavior
            for i in range(2000):
                buf.write("line {}\n".format(i))
            tail = buf.get_tail(tail_bytes=500)
            assert len(tail) < buf.get_size()
            buf.close()

    def test_file_like_interface(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            buf = FileBuffer(path)
            assert not buf.readable()
            assert buf.writable()
            assert not buf.seekable()
            assert not buf.isatty()
            buf.close()
            assert not buf.writable()

    def test_creates_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sub", "dir", "out.log")
            buf = FileBuffer(path)
            buf.write("test")
            buf.close()
            assert os.path.exists(path)


class TestTeeBuffer:
    def test_writes_to_both(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            terminal = StringIO()
            fb = FileBuffer(path)
            tee = TeeBuffer(terminal, fb)
            tee.write("hello\n")
            tee.flush()
            assert terminal.getvalue() == "hello\n"
            assert fb.getvalue() == "hello\n"
            fb.close()

    def test_getvalue_delegates_to_file_buffer(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            terminal = StringIO()
            fb = FileBuffer(path)
            tee = TeeBuffer(terminal, fb)
            tee.write("line1\nline2\n")
            assert tee.getvalue() == "line1\nline2\n"
            assert tee.get_size() == 12
            fb.close()

    def test_terminal_error_does_not_break_capture(self):
        """If terminal write fails, file capture still works."""
        class BrokenWriter:
            def write(self, s):
                raise OSError("terminal broken")
            def flush(self):
                raise OSError("terminal broken")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            fb = FileBuffer(path)
            tee = TeeBuffer(BrokenWriter(), fb)
            tee.write("still captured\n")
            tee.flush()
            assert fb.getvalue() == "still captured\n"
            fb.close()

    def test_file_like_interface(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "out.log")
            fb = FileBuffer(path)
            tee = TeeBuffer(StringIO(), fb)
            assert not tee.readable()
            assert tee.writable()
            assert not tee.seekable()
            assert not tee.isatty()
            fb.close()
