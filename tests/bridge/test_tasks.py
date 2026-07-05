"""Tests for ScriptTask lifecycle and TaskManager."""

import json
import os
import time
from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest
from yade_mcp_bridge.tasks.manager import TaskManager
from yade_mcp_bridge.tasks.task import ScriptTask

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
        assert task.taskId == "t1"
        assert task.scriptName == "test.py"
        assert task.description == "test task"

    def test_status_completed_on_success(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_result({"status": "success", "result": 42})
        assert task.status == "completed"
        assert task.endTime is not None

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

    def test_status_canceled_when_canceled_while_queued(self):
        """Canceling the future of a still-queued task marks it canceled and
        closes the log buffer (the script never runs, so nobody else will)."""
        f = Future()
        buffer = MagicMock()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py", outputBuffer=buffer)
        assert f.cancel()
        assert task.status == "canceled"
        assert task.endTime is not None
        buffer.close.assert_called_once()

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
        ScriptTask(
            "t1",
            f,
            "test.py",
            "/tmp/test.py",
            onStatusChange=lambda t: changes.append(t.status),
        )
        f.set_result({"status": "success"})
        assert "completed" in changes

    def test_get_elapsed_time_running(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        time.sleep(0.01)
        elapsed = task.getElapsedTime()
        assert elapsed > 0

    def test_get_elapsed_time_completed(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_result({"status": "success"})
        elapsed = task.getElapsedTime()
        assert elapsed >= 0
        # Should be stable after completion
        time.sleep(0.01)
        assert task.getElapsedTime() == elapsed

    def test_get_task_info(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py", description="my task")
        info = task.getTaskInfo()
        assert info["task_id"] == "t1"
        assert info["task_type"] == "script"
        assert info["description"] == "my task"
        assert info["status"] == "pending"
        assert "start_time" in info

    def test_get_task_info_completed(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_result({"status": "success"})
        info = task.getTaskInfo()
        assert info["status"] == "completed"
        assert "end_time" in info

    def test_get_task_info_canceled(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.cancel()
        info = task.getTaskInfo()
        assert info["status"] == "canceled"
        assert "end_time" in info

    def test_get_task_info_failed_includes_error(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        f.set_result({"status": "error", "message": "oops"})
        info = task.getTaskInfo()
        assert info["status"] == "failed"
        assert info["error"] == "oops"

    def test_serialize_result(self):
        assert ScriptTask._serializeResult(None) is None
        assert ScriptTask._serializeResult(42) == 42
        assert ScriptTask._serializeResult("hello") == "hello"
        assert ScriptTask._serializeResult(True) is True
        assert ScriptTask._serializeResult([1, 2]) == [1, 2]
        assert ScriptTask._serializeResult({"a": 1}) == {"a": 1}
        # Non-serializable types become strings
        assert isinstance(ScriptTask._serializeResult(object()), str)

    def test_from_persisted(self):
        # Uses the legacy ``entry_script`` key to exercise the back-compat
        # read in fromPersisted (``script_path or entry_script``), so already
        # persisted tasks.json files keep loading after the rename.
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
        }
        task = ScriptTask.fromPersisted(data)
        assert task.taskId == "t1"
        assert task.status == "completed"
        assert task.description == "restored task"
        assert task.scriptPath == "/tmp/test.py"
        assert task.future is None

    def test_get_status_response_pending(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py", description="pending task")
        resp = task.getStatusResponse()
        assert resp["ok"] is True
        # Lifecycle status rides inside data, not at the envelope top level.
        assert resp["data"]["status"] == "pending"
        assert "status" not in resp
        # No cosmetic top-level message anymore.
        assert "message" not in resp

    def test_get_status_response_completed(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py", description="done task")
        f.set_result({"status": "success", "result": 99})
        resp = task.getStatusResponse()
        assert resp["ok"] is True
        assert resp["data"]["status"] == "completed"
        assert resp["data"]["result"] == 99

    def test_get_status_response_failed(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py", description="bad task")
        f.set_result({"status": "error", "message": "kaboom"})
        resp = task.getStatusResponse()
        # A failed task is still a successful request: ok stays True and the
        # failure rides in data (data.status == "failed", data.error).
        assert resp["ok"] is True
        assert resp["data"]["status"] == "failed"
        assert "kaboom" in resp["data"]["error"]

    def test_get_status_response_canceled(self):
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py", description="queued task")
        f.cancel()
        resp = task.getStatusResponse()
        assert resp["ok"] is True
        assert resp["data"]["status"] == "canceled"

    def _make_task_with_log(self, tmp_path, lines):
        """Helper: create a task backed by a real log file containing given lines."""
        logPath = str(tmp_path / "task.log")
        with open(logPath, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        task.logPath = logPath
        return task

    def test_paginated_output_empty_log(self, tmp_path):
        task = self._make_task_with_log(tmp_path, [])
        text, pag = task.getPaginatedOutput()
        assert text == ""
        assert pag["total_lines"] == 0
        assert pag["line_range"] == "0-0"

    def test_paginated_output_tail_window(self, tmp_path):
        task = self._make_task_with_log(tmp_path, [f"line {i}" for i in range(20)])
        text, pag = task.getPaginatedOutput(skipNewest=0, limit=5)
        assert pag["total_lines"] == 20
        # window is the last 5 lines (16-20 of 20): older lines exist, none newer
        assert pag["line_range"] == "16-20"
        assert "line 19" in text
        assert "line 15" in text
        assert "line 14" not in text

    def test_paginated_output_skip_newest(self, tmp_path):
        task = self._make_task_with_log(tmp_path, [f"line {i}" for i in range(20)])
        text, pag = task.getPaginatedOutput(skipNewest=5, limit=5)
        # skipping 5 newest leaves a mid-log window (11-15 of 20): newer lines exist
        assert pag["line_range"] == "11-15"
        assert "line 19" not in text
        assert "line 14" in text

    def test_paginated_output_filter(self, tmp_path):
        task = self._make_task_with_log(tmp_path, ["error: one", "info: ok", "error: two", "debug: noise"])
        text, pag = task.getPaginatedOutput(filterText="error")
        assert pag["total_lines"] == 2
        assert "info" not in text
        assert "debug" not in text
        assert "error: one" in text
        assert "error: two" in text

    def test_paginated_output_limit_exceeds_total(self, tmp_path):
        task = self._make_task_with_log(tmp_path, ["a", "b"])
        text, pag = task.getPaginatedOutput(limit=100)
        assert pag["total_lines"] == 2
        # whole log fits in one window: no older lines
        assert pag["line_range"] == "1-2"
        assert "a" in text and "b" in text

    def test_paginated_output_handles_bad_bytes(self, tmp_path):
        """Log with invalid UTF-8 should not crash thanks to errors='replace'."""
        logPath = str(tmp_path / "task.log")
        with open(logPath, "wb") as fh:
            fh.write(b"ok line\n\xff\xfe bad bytes\nlast line")
        f = Future()
        task = ScriptTask("t1", f, "test.py", "/tmp/test.py")
        task.logPath = logPath
        text, pag = task.getPaginatedOutput()
        assert pag["total_lines"] == 3
        assert "last line" in text

    def test_get_status_response_includes_pagination(self, tmp_path):
        task = self._make_task_with_log(tmp_path, ["alpha", "beta"])
        resp = task.getStatusResponse()
        data = resp["data"]
        assert "pagination" in data
        assert data["pagination"]["total_lines"] == 2
        assert "alpha" in data["output"]


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
        tid = tm.createScriptTask(f, "test.py", "/test.py", description="test")
        assert tid in tm.tasks
        assert tm.tasks[tid].description == "test"

    def test_create_task_custom_id(self):
        tm = TaskManager()
        f = Future()
        tid = tm.createScriptTask(f, "test.py", "/test.py", taskId="custom-id")
        assert tid == "custom-id"

    def test_get_task_status_found(self):
        tm = TaskManager()
        f = Future()
        tid = tm.createScriptTask(f, "test.py", "/test.py", description="desc")
        result = tm.getTaskStatus(tid)
        assert result["ok"] is True
        assert result["data"]["status"] in ("pending", "running")

    def test_get_task_status_not_found(self):
        tm = TaskManager()
        result = tm.getTaskStatus("nonexistent")
        assert result["ok"] is False
        assert result["error"]["code"] == "not_found"

    def test_list_all_tasks(self):
        tm = TaskManager()
        for i in range(3):
            f = Future()
            tm.createScriptTask(f, f"s{i}.py", f"/s{i}.py")
        result = tm.listAllTasks()
        assert result["ok"] is True
        assert len(result["data"]) == 3

    def test_list_tasks_pagination(self):
        tm = TaskManager()
        for i in range(5):
            f = Future()
            tm.createScriptTask(f, f"s{i}.py", f"/s{i}.py")
        result = tm.listAllTasks(offset=1, limit=2)
        assert len(result["data"]) == 2
        assert result["pagination"]["total_count"] == 5
        # 2-of-5 slice → more tasks exist beyond this page
        pag = result["pagination"]
        assert pag["offset"] + len(result["data"]) < pag["total_count"]

    def test_has_running_tasks(self):
        tm = TaskManager()
        f = Future()
        tm.createScriptTask(f, "test.py", "/test.py")
        # Future not started yet, so still pending
        assert not tm.hasRunningTasks()

    def test_persistence_save_and_load(self):
        tm = TaskManager()
        f = Future()
        tm.createScriptTask(f, "test.py", "/test.py", description="persist me", taskId="persist-1")
        f.set_result({"status": "success"})

        # Create new manager to load from disk
        tm2 = TaskManager()
        assert "persist-1" in tm2.tasks
        assert tm2.tasks["persist-1"].description == "persist me"

    def test_running_task_restored_as_failed(self):
        """Tasks that were 'running' when persisted should load as 'failed'."""
        tm = TaskManager()
        f = Future()
        tid = tm.createScriptTask(f, "test.py", "/test.py", taskId="was-running")
        # Manually set status to running and save
        tm.tasks[tid].status = "running"
        tm._saveTasks()

        tm2 = TaskManager()
        assert tm2.tasks["was-running"].status == "failed"

    def test_pending_task_restored_as_canceled(self):
        """The queue does not survive a restart, so a task persisted while
        still 'pending' loads as 'canceled', not as queued-forever."""
        tm = TaskManager()
        f = Future()
        tm.createScriptTask(f, "test.py", "/test.py", taskId="was-queued")
        tm._saveTasks()

        tm2 = TaskManager()
        assert tm2.tasks["was-queued"].status == "canceled"

    def test_prune_on_startup(self):
        """Old tasks beyond max_tasks are pruned at startup."""
        tm = TaskManager()
        for i in range(5):
            f = Future()
            f.set_result({"status": "success"})
            tm.createScriptTask(f, f"s{i}.py", f"/s{i}.py", taskId=f"t{i}")
            # Space out start_time so ordering is deterministic
            tm.tasks[f"t{i}"].startTime = 1000.0 + i
        tm._saveTasks()

        # Reload with max_tasks=3 — oldest 2 should be pruned
        tm2 = TaskManager(maxTasks=3)
        assert len(tm2.tasks) == 3
        assert "t0" not in tm2.tasks
        assert "t1" not in tm2.tasks
        assert "t2" in tm2.tasks
        assert "t3" in tm2.tasks
        assert "t4" in tm2.tasks

    def test_prune_deletes_log_files(self, tmp_path):
        """Pruning removes associated log files from disk."""
        tm = TaskManager(maxTasks=2)
        logs_dir = tmp_path / ".yade-mcp" / "logs"

        for i in range(3):
            f = Future()
            f.set_result({"status": "success"})
            logPath = str(logs_dir / f"task_t{i}.log")
            # Create the log file
            with open(logPath, "w") as fh:
                fh.write(f"output {i}")
            tm.createScriptTask(f, f"s{i}.py", f"/s{i}.py", taskId=f"t{i}")
            tm.tasks[f"t{i}"].logPath = logPath
            tm.tasks[f"t{i}"].startTime = 1000.0 + i

        # After adding t2, max_tasks=2 should prune t0
        tm._pruneOldTasks()
        tm._saveTasks()
        assert len(tm.tasks) == 2
        assert not os.path.exists(str(logs_dir / "task_t0.log"))
        assert os.path.exists(str(logs_dir / "task_t2.log"))

    def test_prune_on_create(self):
        """Creating a new task triggers pruning when limit exceeded."""
        tm = TaskManager(maxTasks=3)
        for i in range(3):
            f = Future()
            f.set_result({"status": "success"})
            tm.createScriptTask(f, f"s{i}.py", f"/s{i}.py", taskId=f"t{i}")
            tm.tasks[f"t{i}"].startTime = 1000.0 + i

        assert len(tm.tasks) == 3

        # Adding one more should prune the oldest
        f = Future()
        f.set_result({"status": "success"})
        tm.createScriptTask(f, "s3.py", "/s3.py", taskId="t3")
        tm.tasks["t3"].startTime = 1003.0

        assert len(tm.tasks) == 3
        assert "t0" not in tm.tasks

    def test_shutdown_flushes_buffers(self):
        """Shutdown flushes all active output buffers."""
        tm = TaskManager()
        f = Future()
        tm.createScriptTask(f, "test.py", "/test.py", taskId="buf-1")
        mock_buffer = MagicMock()
        tm.tasks["buf-1"].outputBuffer = mock_buffer
        tm.tasks["buf-1"].status = "running"

        tm.shutdown()
        mock_buffer.flush.assert_called_once()

    def test_shutdown_marks_active_tasks(self):
        """Shutdown interrupts running tasks and cancels queued ones."""
        tm = TaskManager()
        for status, tid in [("running", "r1"), ("pending", "p1"), ("completed", "c1")]:
            f = Future()
            if status == "completed":
                f.set_result({"status": "success"})
            tm.createScriptTask(f, "test.py", "/test.py", taskId=tid)
            if status != "completed":
                tm.tasks[tid]._status = status

        tm.shutdown()

        assert tm.tasks["r1"].status == "interrupted"
        assert tm.tasks["r1"].endTime is not None
        assert tm.tasks["r1"].error == "Bridge shutdown"
        assert tm.tasks["p1"].status == "canceled"
        assert tm.tasks["p1"].endTime is not None
        assert tm.tasks["c1"].status == "completed"  # untouched

    def test_shutdown_persists_to_disk(self, tmp_path):
        """Shutdown saves updated statuses to tasks.json."""
        tm = TaskManager()
        f = Future()
        tm.createScriptTask(f, "test.py", "/test.py", taskId="persist-s")
        tm.tasks["persist-s"]._status = "running"

        tm.shutdown()

        tasks_file = tmp_path / ".yade-mcp" / "tasks.json"
        data = json.loads(tasks_file.read_text())
        saved = {t["task_id"]: t for t in data}
        assert saved["persist-s"]["status"] == "interrupted"

    def test_shutdown_not_restored_as_failed(self, tmp_path):
        """Tasks interrupted by shutdown should NOT become 'failed' on reload."""
        tm = TaskManager()
        f = Future()
        tm.createScriptTask(f, "test.py", "/test.py", taskId="graceful-1")
        tm.tasks["graceful-1"]._status = "running"
        tm.shutdown()

        # Reload — should stay "interrupted", not become "failed"
        tm2 = TaskManager()
        assert tm2.tasks["graceful-1"].status == "interrupted"
