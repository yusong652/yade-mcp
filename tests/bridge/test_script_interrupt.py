"""Tests for ScriptRunner's TaskInterrupt path — the async-exc escape
hatch that handles pure-Python deadloops that the flag/PyRunner path
cannot reach."""

import sys
import types
from unittest.mock import MagicMock

import pytest
from yade_mcp_bridge.execution.script import ScriptRunner
from yade_mcp_bridge.signals import (
    _exec_thread_ids,
    clear_interrupt,
    get_exec_thread,
)


@pytest.fixture
def fake_yade(monkeypatch):
    module = types.ModuleType("yade")
    module.O = MagicMock()
    module.O.running = False
    monkeypatch.setitem(sys.modules, "yade", module)
    yield module.O


@pytest.fixture
def runner():
    task_manager = MagicMock()
    task_manager.tasks = {}
    return ScriptRunner(main_executor=MagicMock(), task_manager=task_manager)


@pytest.fixture
def scratch(tmp_path):
    def make(content):
        script_path = tmp_path / "s.py"
        script_path.write_text(content, encoding="utf-8")
        from yade_mcp_bridge.utils import FileBuffer

        buffer = FileBuffer(str(tmp_path / "task.log"))
        return str(script_path), buffer

    return make


@pytest.fixture(autouse=True)
def _cleanup():
    _exec_thread_ids.clear()
    for tid in ("ti-1", "ti-2", "ti-3", "ti-reg"):
        clear_interrupt(tid)
    yield
    _exec_thread_ids.clear()
    for tid in ("ti-1", "ti-2", "ti-3", "ti-reg"):
        clear_interrupt(tid)


class TestTaskInterrupt:
    def test_script_raising_taskinterrupt_returns_interrupted(self, fake_yade, runner, scratch):
        """User script directly raising TaskInterrupt mirrors what the
        async-exc injection does from the outside."""
        code = (
            "from yade_mcp_bridge.execution.errors import TaskInterrupt\n"
            "raise TaskInterrupt()\n"
        )
        script_path, buffer = scratch(code)
        result = runner._execute(script_path, code, buffer, task_id="ti-1")

        assert result["status"] == "interrupted"
        assert "force-interrupted" in result["message"].lower()
        assert result["result"] is None

    def test_sim_is_paused_and_drained_on_taskinterrupt(self, fake_yade, runner, scratch):
        """If the task was mid-simulation when the async abort fires,
        _execute must ``O.pause()`` + ``O.wait()`` so the sim doesn't
        linger and poison the next task's drain check."""
        fake_yade.running = True
        code = (
            "from yade_mcp_bridge.execution.errors import TaskInterrupt\n"
            "raise TaskInterrupt()\n"
        )
        script_path, buffer = scratch(code)
        result = runner._execute(script_path, code, buffer, task_id="ti-2")

        assert result["status"] == "interrupted"
        fake_yade.pause.assert_called_once()
        fake_yade.wait.assert_called_once()

    def test_sim_cleanup_skipped_when_not_running(self, fake_yade, runner, scratch):
        fake_yade.running = False
        code = (
            "from yade_mcp_bridge.execution.errors import TaskInterrupt\n"
            "raise TaskInterrupt()\n"
        )
        script_path, buffer = scratch(code)
        result = runner._execute(script_path, code, buffer, task_id="ti-3")

        assert result["status"] == "interrupted"
        fake_yade.pause.assert_not_called()
        fake_yade.wait.assert_not_called()

    def test_sim_cleanup_exception_is_swallowed(self, fake_yade, runner, scratch):
        """If O.pause or O.wait themselves raise during cleanup, the
        task must still report interrupted — never leak the cleanup
        exception as the task's top-line status."""
        fake_yade.running = True
        fake_yade.wait.side_effect = RuntimeError("sim zombie")
        code = (
            "from yade_mcp_bridge.execution.errors import TaskInterrupt\n"
            "raise TaskInterrupt()\n"
        )
        script_path, buffer = scratch(code)
        result = runner._execute(script_path, code, buffer, task_id="ti-4")

        assert result["status"] == "interrupted"


class TestRegistryLifecycle:
    def test_execute_registers_and_unregisters_thread(self, fake_yade, runner, scratch):
        """Script body must see its own tid in the registry (so the
        interrupt handler can find it), and the registry must be empty
        after the task returns."""
        code = (
            "import threading\n"
            "from yade_mcp_bridge.signals import get_exec_thread\n"
            "result = (get_exec_thread('ti-reg'), threading.get_ident())\n"
        )
        script_path, buffer = scratch(code)
        result = runner._execute(script_path, code, buffer, task_id="ti-reg")

        assert result["status"] == "success"
        # The script captured (registered_tid, actual_tid) — they must match.
        registered, actual = result["result"]
        assert registered is not None
        assert registered == actual
        # Post-return the registry must be clean.
        assert get_exec_thread("ti-reg") is None
