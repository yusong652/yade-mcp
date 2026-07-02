"""Tests for waitForBackgroundRun — the post-exec O.wait() that
aligns task lifetime with cycling lifetime (fixes wait=False orphan
cycling + silent-success bugs)."""

import sys
import types
from unittest.mock import MagicMock

import pytest
from yade_mcp_bridge.execution.scriptRunner import ScriptRunner
from yade_mcp_bridge.runtime.backgroundRun import markBackgroundRun
from yade_mcp_bridge.runtime.signals import clearInterrupt, requestInterrupt


@pytest.fixture
def fake_yade(monkeypatch):
    """Install a fake `yade` module in sys.modules so scriptRunner.py's
    `from yade import O as _O` resolves to our mock."""
    module = types.ModuleType("yade")
    module.O = MagicMock()
    module.O.running = False
    monkeypatch.setitem(sys.modules, "yade", module)
    yield module.O
    # monkeypatch cleans up sys.modules automatically


@pytest.fixture
def runner():
    taskManager = MagicMock()
    taskManager.tasks = {}
    return ScriptRunner(taskManager=taskManager)


@pytest.fixture
def scratch_script(tmp_path):
    """Create a scratch script file and matching FileBuffer."""

    def make(content):
        scriptPath = tmp_path / "script.py"
        scriptPath.write_text(content, encoding="utf-8")
        from yade_mcp_bridge.utils import FileBuffer

        logPath = tmp_path / "task.log"
        buffer = FileBuffer(str(logPath))
        return str(scriptPath), buffer

    return make


class TestWaitForBackgroundRun:
    def test_waits_when_running_true(self, fake_yade, runner, scratch_script):
        """If cycling is live after exec, O.wait() must be called."""
        fake_yade.running = True
        markBackgroundRun(True)  # as if the script called O.run(wait=False)
        scriptPath, buffer = scratch_script("x = 1\n")

        result = runner._execute(scriptPath, "x = 1\n", buffer, taskId="t1")

        assert result["status"] == "success"
        fake_yade.wait.assert_called_once()

    def test_skipped_when_running_false(self, fake_yade, runner, scratch_script):
        """If cycling isn't running, O.wait() is skipped — don't block."""
        fake_yade.running = False
        scriptPath, buffer = scratch_script("x = 1\n")

        result = runner._execute(scriptPath, "x = 1\n", buffer, taskId="t2")

        assert result["status"] == "success"
        fake_yade.wait.assert_not_called()

    def test_reraises_cycling_error_as_failed(self, fake_yade, runner, scratch_script):
        """O.wait() raising RuntimeError (cycling died) → task marked failed."""
        fake_yade.running = True
        markBackgroundRun(True)  # as if the script called O.run(wait=False)
        fake_yade.wait.side_effect = RuntimeError("PyRunner error. COMMAND: 'bad()'")
        scriptPath, buffer = scratch_script("pass\n")

        result = runner._execute(scriptPath, "pass\n", buffer, taskId="t3")

        assert result["status"] == "error"
        assert "PyRunner" in result["message"]
        assert result["exception_type"] == "RuntimeError"

    def test_interrupt_flag_after_wait_marks_interrupted(self, fake_yade, runner, scratch_script):
        """Flag set during/after O.wait() → CycleInterrupt → task interrupted."""
        fake_yade.running = True
        markBackgroundRun(True)  # as if the script called O.run(wait=False)
        fake_yade.wait.side_effect = lambda: requestInterrupt("t4")
        scriptPath, buffer = scratch_script("pass\n")

        try:
            result = runner._execute(scriptPath, "pass\n", buffer, taskId="t4")
        finally:
            clearInterrupt("t4")

        assert result["status"] == "interrupted"

    def test_import_error_is_swallowed(self, runner, scratch_script, monkeypatch):
        """If yade isn't importable (tests outside YADE process), the wait no-ops."""
        monkeypatch.setitem(sys.modules, "yade", None)  # make import fail
        scriptPath, buffer = scratch_script("x = 42\n")

        result = runner._execute(scriptPath, "x = 42\n", buffer, taskId="t5")

        assert result["status"] == "success"

    def test_closes_output_buffer_when_done(self, fake_yade, runner, scratch_script):
        """Once execution is terminal, the log handle is released (fd leak
        fix). Writes become no-ops, but reads still work via re-open by path."""
        fake_yade.running = False
        scriptPath, buffer = scratch_script("print('hi')\n")

        runner._execute(scriptPath, "print('hi')\n", buffer, taskId="t6")

        assert buffer.write("more") == 0  # closed -> writes are no-ops
        assert buffer.getvalue() == "hi\n"  # reads still re-open by path

    def test_script_error_before_wait_still_surfaces(self, fake_yade, runner, scratch_script):
        """Normal script-raised errors should still be caught (the wait doesn't swallow them)."""
        fake_yade.running = True
        scriptPath, buffer = scratch_script("raise ValueError('boom')\n")

        result = runner._execute(scriptPath, "raise ValueError('boom')\n", buffer, taskId="t6")

        assert result["status"] == "error"
        # the wait should not have been reached — O.wait not called
        fake_yade.wait.assert_not_called()


@pytest.fixture(autouse=True)
def _cleanup_signals():
    """Ensure signals state is clean between tests. The background-run flag is
    thread-local and persists on pytest's thread across tests, so it must be
    reset or a prior test's markBackgroundRun(True) would leak."""
    markBackgroundRun(False)
    for tid in ("t1", "t2", "t3", "t4", "t5", "t6"):
        clearInterrupt(tid)
    yield
    markBackgroundRun(False)
    for tid in ("t1", "t2", "t3", "t4", "t5", "t6"):
        clearInterrupt(tid)
