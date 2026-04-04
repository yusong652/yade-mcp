"""Tests for ScriptTask lifecycle and TaskManager."""

import os
import tempfile
import time
from concurrent.futures import Future
from unittest.mock import patch

import pytest

from yade_mcp_bridge.tasks.task import ScriptTask
from yade_mcp_bridge.tasks.manager import TaskManager


# =========================================================================
# ScriptTask
# =========================================================================


class TestScriptTask:
    def _make_future(self, result=None):
        f = Future()
        if result is not None:
            f.set_result(result)
        return f

    def test_initial_status_pending(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py", description="test task")
        assert task.status == "pending"
        assert task.task_id == "t1"
        assert task.script_name == "test.py"
        assert task.description == "test task"

    def test_status_completed_on_success(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_result({"status": "success", "result": 42})
        assert task.status == "completed"
        assert task.end_time is not None

    def test_status_failed_on_error_result(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_result({"status": "error", "message": "boom"})
        assert task.status == "failed"
        assert task.error == "boom"

    def test_status_interrupted(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_result({"status": "interrupted"})
        assert task.status == "interrupted"

    def test_status_failed_on_exception(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_exception(RuntimeError("crash"))
        assert task.status == "failed"
        assert "crash" in task.error

    def test_status_completed_on_non_dict_result(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_result("just a string")
        assert task.status == "completed"

    def test_on_status_change_callback(self):
        changes = []
        f = Future()
        task = ScriptTask(
            "t1", f, "test.py", "/tmp/test.py",
            on_status_change=lambda t: changes.append(t.status),
        )
        f.set_result({"status": "success"})
        assert "completed" in changes

    def test_get_elapsed_time_running(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        time.sleep(0.01)
        elapsed = task.get_elapsed_time()
        assert elapsed > 0

    def test_get_elapsed_time_completed(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_result({"status": "success"})
        elapsed = task.get_elapsed_time()
        assert elapsed >= 0
        # Should be stable after completion
        time.sleep(0.01)
        assert task.get_elapsed_time() == elapsed

    def test_get_task_info(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py", description="my task")
        info = task.get_task_info()
        assert info["task_id"] == "t1"
        assert info["task_type"] == "script"
        assert info["description"] == "my task"
        assert info["status"] == "pending"
        assert "start_time" in info

    def test_get_task_info_completed(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_result({"status": "success"})
        info = task.get_task_info()
        assert info["status"] == "completed"
        assert "end_time" in info

    def test_get_task_info_failed_includes_error(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_result({"status": "error", "message": "oops"})
        info = task.get_task_info()
        assert info["status"] == "failed"
        assert info["error"] == "oops"

    def test_serialize_result(self):
        assert ScriptTask._serialize_result(None) is None
        assert ScriptTask._serialize_result(42) == 42
        assert ScriptTask._serialize_result("hello") == "hello"
        assert ScriptTask._serialize_result(True) is True
        assert ScriptTask._serialize_result([1, 2]) == [1, 2]
        assert ScriptTask._serialize_result({"a": 1}) == {"a": 1}
        # Non-serializable types become strings
        assert isinstance(ScriptTask._serialize_result(object()), str)

    def test_from_persisted(self):
        data = {
            "task_id": "t1",
            "description": "restored task",
            "script_name": "test.py",
            "entry_script": "/tmp/test.py",
            "status": "completed",
            "start_time": 1000.0,
            "end_time": 1010.0,
            "log_path": None,
            "error": None,
            "output": "some output",
        }
        task = ScriptTask.from_persisted(data)
        assert task.task_id == "t1"
        assert task.status == "completed"
        assert task.description == "restored task"
        assert task.future is None

    def test_get_status_response_pending(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py", description="pending task")
        resp = task.get_status_response()
        assert resp["status"] == "pending"
        assert "queued" in resp["message"]

    def test_get_status_response_completed(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py", description="done task")
        f.set_result({"status": "success", "result": 99})
        resp = task.get_status_response()
        assert resp["status"] == "success"
        assert "completed" in resp["message"]

    def test_get_status_response_failed(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py", description="bad task")
        f.set_result({"status": "error", "message": "kaboom"})
        resp = task.get_status_response()
        assert resp["status"] == "error"
        assert "kaboom" in resp["message"]


# =========================================================================
# TaskManager
# =========================================================================


class TestTaskManager:
    @pytest.fixture(autouse=True)
    def use_tmpdir(self, tmp_path, monkeypatch):
        """Run each test in a temporary directory to isolate .yade-mcp/."""
        monkeypatch.chdir(tmp_path)
        # Patch the module-level constants
        monkeypatch.setattr("yade_mcp_bridge.tasks.manager.DATA_DIR", str(tmp_path / ".yade-mcp"))
        monkeypatch.setattr("yade_mcp_bridge.tasks.manager.LOGS_DIR", str(tmp_path / ".yade-mcp" / "logs"))
        monkeypatch.setattr(
            "yade_mcp_bridge.tasks.manager.TASKS_FILENAME",
            str(tmp_path / ".yade-mcp" / "tasks.json"),
        )

    def test_create_task(self):
        tm = TaskManager()
        f = Future()
        tid = tm.create_script_task(f, "test.py", "/test.py", description="test")
        assert tid in tm.tasks
        assert tm.tasks[tid].description == "test"

    def test_create_task_custom_id(self):
        tm = TaskManager()
        f = Future()
        tid = tm.create_script_task(f, "test.py", "/test.py", task_id="custom-id")
        assert tid == "custom-id"

    def test_get_task_status_found(self):
        tm = TaskManager()
        f = Future()
        tid = tm.create_script_task(f, "test.py", "/test.py", description="desc")
        result = tm.get_task_status(tid)
        assert result["status"] in ("pending", "running")

    def test_get_task_status_not_found(self):
        tm = TaskManager()
        result = tm.get_task_status("nonexistent")
        assert result["status"] == "not_found"

    def test_list_all_tasks(self):
        tm = TaskManager()
        for i in range(3):
            f = Future()
            tm.create_script_task(f, "s{}.py".format(i), "/s{}.py".format(i))
        result = tm.list_all_tasks()
        assert result["status"] == "success"
        assert len(result["data"]) == 3

    def test_list_tasks_pagination(self):
        tm = TaskManager()
        for i in range(5):
            f = Future()
            tm.create_script_task(f, "s{}.py".format(i), "/s{}.py".format(i))
        result = tm.list_all_tasks(offset=1, limit=2)
        assert len(result["data"]) == 2
        assert result["pagination"]["total_count"] == 5
        assert result["pagination"]["has_more"] is True

    def test_has_running_tasks(self):
        tm = TaskManager()
        f = Future()
        tm.create_script_task(f, "test.py", "/test.py")
        # Future not started yet, so still pending
        assert not tm.has_running_tasks()

    def test_persistence_save_and_load(self):
        tm = TaskManager()
        f = Future()
        tid = tm.create_script_task(f, "test.py", "/test.py", description="persist me", task_id="persist-1")
        f.set_result({"status": "success"})

        # Create new manager to load from disk
        tm2 = TaskManager()
        assert "persist-1" in tm2.tasks
        assert tm2.tasks["persist-1"].description == "persist me"

    def test_running_task_restored_as_failed(self):
        """Tasks that were 'running' when persisted should load as 'failed'."""
        tm = TaskManager()
        f = Future()
        tid = tm.create_script_task(f, "test.py", "/test.py", task_id="was-running")
        # Manually set status to running and save
        tm.tasks[tid].status = "running"
        tm._save_tasks()

        tm2 = TaskManager()
        assert tm2.tasks["was-running"].status == "failed"
