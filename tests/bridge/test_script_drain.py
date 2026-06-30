"""Tests for ScriptRunner drain behavior — the post-exec O.wait() that
aligns task lifetime with cycling lifetime (fixes wait=False orphan
cycling + silent-success bugs)."""

import sys
import types
from unittest.mock import MagicMock

import pytest
from yade_mcp_bridge.execution.script_runner import ScriptRunner
from yade_mcp_bridge.runtime.pyrunner import mark_async_cycling
from yade_mcp_bridge.runtime.signals import clear_interrupt, request_interrupt


@pytest.fixture
def fake_yade(monkeypatch):
    """Install a fake `yade` module in sys.modules so script_runner.py's
    `from yade import O as _O` resolves to our mock."""
    module = types.ModuleType("yade")
    module.O = MagicMock()
    module.O.running = False
    monkeypatch.setitem(sys.modules, "yade", module)
    yield module.O
    # monkeypatch cleans up sys.modules automatically


@pytest.fixture
def runner():
    task_manager = MagicMock()
    task_manager.tasks = {}
    return ScriptRunner(task_manager=task_manager)


@pytest.fixture
def scratch_script(tmp_path):
    """Create a scratch script file and matching FileBuffer."""

    def make(content):
        script_path = tmp_path / "script.py"
        script_path.write_text(content, encoding="utf-8")
        from yade_mcp_bridge.utils import FileBuffer

        log_path = tmp_path / "task.log"
        buffer = FileBuffer(str(log_path))
        return str(script_path), buffer

    return make


class TestDrain:
    def test_waits_when_running_true(self, fake_yade, runner, scratch_script):
        """If cycling is live after exec, drain must call O.wait()."""
        fake_yade.running = True
        mark_async_cycling(True)  # simulate the script's O.run(wait=False) dispatch
        script_path, buffer = scratch_script("x = 1\n")

        result = runner._execute(script_path, "x = 1\n", buffer, task_id="t1")

        assert result["status"] == "success"
        fake_yade.wait.assert_called_once()

    def test_skipped_when_running_false(self, fake_yade, runner, scratch_script):
        """If cycling isn't running, drain skips O.wait() — don't block."""
        fake_yade.running = False
        script_path, buffer = scratch_script("x = 1\n")

        result = runner._execute(script_path, "x = 1\n", buffer, task_id="t2")

        assert result["status"] == "success"
        fake_yade.wait.assert_not_called()

    def test_reraises_cycling_error_as_failed(self, fake_yade, runner, scratch_script):
        """O.wait() raising RuntimeError (cycling died) → task marked failed."""
        fake_yade.running = True
        mark_async_cycling(True)  # simulate the script's O.run(wait=False) dispatch
        fake_yade.wait.side_effect = RuntimeError("PyRunner error. COMMAND: 'bad()'")
        script_path, buffer = scratch_script("pass\n")

        result = runner._execute(script_path, "pass\n", buffer, task_id="t3")

        assert result["status"] == "error"
        assert "PyRunner" in result["message"]
        assert result["exception_type"] == "RuntimeError"

    def test_interrupt_flag_after_wait_marks_interrupted(self, fake_yade, runner, scratch_script):
        """Flag set during/after O.wait() → drain raises CycleInterrupt → task interrupted."""
        fake_yade.running = True
        mark_async_cycling(True)  # simulate the script's O.run(wait=False) dispatch
        fake_yade.wait.side_effect = lambda: request_interrupt("t4")
        script_path, buffer = scratch_script("pass\n")

        try:
            result = runner._execute(script_path, "pass\n", buffer, task_id="t4")
        finally:
            clear_interrupt("t4")

        assert result["status"] == "interrupted"

    def test_import_error_is_swallowed(self, runner, scratch_script, monkeypatch):
        """If yade isn't importable (tests outside YADE process), drain no-ops."""
        monkeypatch.setitem(sys.modules, "yade", None)  # make import fail
        script_path, buffer = scratch_script("x = 42\n")

        result = runner._execute(script_path, "x = 42\n", buffer, task_id="t5")

        assert result["status"] == "success"

    def test_closes_output_buffer_when_done(self, fake_yade, runner, scratch_script):
        """Once execution is terminal, the log handle is released (fd leak
        fix). Writes become no-ops, but reads still work via re-open by path."""
        fake_yade.running = False
        script_path, buffer = scratch_script("print('hi')\n")

        runner._execute(script_path, "print('hi')\n", buffer, task_id="t6")

        assert buffer.write("more") == 0  # closed -> writes are no-ops
        assert buffer.getvalue() == "hi\n"  # reads still re-open by path

    def test_script_error_before_drain_still_surfaces(self, fake_yade, runner, scratch_script):
        """Normal script-raised errors should still be caught (drain doesn't swallow them)."""
        fake_yade.running = True
        script_path, buffer = scratch_script("raise ValueError('boom')\n")

        result = runner._execute(script_path, "raise ValueError('boom')\n", buffer, task_id="t6")

        assert result["status"] == "error"
        # drain should not have been reached — O.wait not called
        fake_yade.wait.assert_not_called()


@pytest.fixture(autouse=True)
def _cleanup_signals():
    """Ensure signals state is clean between tests. The async-cycling flag is
    thread-local and persists on pytest's thread across tests, so it must be
    reset or a prior test's mark_async_cycling(True) would leak."""
    mark_async_cycling(False)
    for tid in ("t1", "t2", "t3", "t4", "t5", "t6"):
        clear_interrupt(tid)
    yield
    mark_async_cycling(False)
    for tid in ("t1", "t2", "t3", "t4", "t5", "t6"):
        clear_interrupt(tid)
