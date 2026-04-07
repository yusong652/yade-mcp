"""Script Task - Lifecycle management for long-running YADE script execution."""

import logging
import os
import time

from ..utils import TaskDataBuilder, build_response

logger = logging.getLogger("YADE-Bridge")


class ScriptTask:
    """Task for Python script execution with real-time output capture.

    Status values:
    - "pending": Task queued, waiting for main thread
    - "running": Task currently executing
    - "completed": Task finished successfully
    - "failed": Task finished with error
    - "interrupted": Task was interrupted by user
    """

    def __init__(self, task_id, future, script_name, entry_script,
                 output_buffer=None, description=None, on_status_change=None):
        self.task_id = task_id
        self.future = future
        self.description = description or ""
        self.script_name = script_name
        self.entry_script = entry_script
        self.output_buffer = output_buffer
        self.start_time = time.time()
        self.end_time = None
        self._status = "pending"
        self.on_status_change = on_status_change
        self.error = None

        self.log_path = None
        if output_buffer and hasattr(output_buffer, 'get_path'):
            self.log_path = output_buffer.get_path()

        future.add_done_callback(self._on_complete)

        logger.info("Script task registered: %s (id=%s)", script_name, task_id)

    @classmethod
    def from_persisted(cls, task_data):
        """Create a task from persisted data (no Future or buffer)."""
        task = cls.__new__(cls)
        task.task_id = task_data["task_id"]
        task.description = task_data["description"]
        task.script_name = task_data.get("script_name", "")
        task.entry_script = task_data.get("entry_script") or task_data.get("script_path") or ""
        task._status = task_data["status"]
        task.start_time = task_data["start_time"]
        task.end_time = task_data.get("end_time")
        task.log_path = task_data.get("log_path")
        task.error = task_data.get("error")
        task.future = None
        task.output_buffer = None
        task.on_status_change = None
        task._output_snapshot = task_data.get("output", "")
        return task

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        self._status = value

    def _on_complete(self, f):
        self.end_time = time.time()
        try:
            result = f.result(timeout=0)
            if isinstance(result, dict):
                result_status = result.get("status")
                if result_status == "error":
                    self.status = "failed"
                    self.error = result.get("message", "Task execution failed")
                elif result_status == "interrupted":
                    self.status = "interrupted"
                else:
                    self.status = "completed"
            else:
                self.status = "completed"
        except Exception as e:
            self.status = "failed"
            self.error = str(e)

        if self.on_status_change:
            try:
                self.on_status_change(self)
            except Exception as e:
                logger.warning(f"Status change callback failed: {e}")

    def get_elapsed_time(self):
        if self.end_time is not None:
            return self.end_time - self.start_time
        if self.future is None:
            return 0.0
        return time.time() - self.start_time

    def get_current_output(self):
        if self.output_buffer:
            try:
                self.output_buffer.flush()
            except (ValueError, OSError):
                pass

        if self.log_path:
            try:
                if os.path.exists(self.log_path):
                    with open(self.log_path, encoding='utf-8') as f:
                        return f.read()
            except OSError as e:
                logger.warning(f"Failed to read log file: {e}")

        snapshot = getattr(self, '_output_snapshot', None)
        return snapshot if snapshot else None

    def _create_data_builder(self):
        return TaskDataBuilder(
            self.task_id, "script",
            self.script_name, self.entry_script, self.description
        )

    def get_status_response(self):
        current_status = self.status
        elapsed_time = self.get_elapsed_time()
        current_output = self.get_current_output()

        builder = self._create_data_builder()

        if current_status in ("pending", "running"):
            builder.with_timing(self.start_time, elapsed_time=elapsed_time)
            builder.with_output(current_output)
            phase = "queued" if current_status == "pending" else "executing"
            message = f"Script {phase}: {self.description}\nElapsed time: {elapsed_time:.2f}s"
            return build_response(current_status, message, builder.build())

        # completed / failed / interrupted
        builder.with_timing(self.start_time, self.end_time, elapsed_time)
        builder.with_output(current_output if current_output else "")

        if current_status == "completed":
            result_data = None
            if self.future:
                try:
                    result = self.future.result(timeout=0)
                    if isinstance(result, dict):
                        result_data = result.get("result")
                except Exception:
                    pass
            builder.with_result(self._serialize_result(result_data))
            message = f"Script completed: {self.description}\nElapsed time: {elapsed_time:.2f}s"
            return build_response("completed", message, builder.build())

        if current_status == "interrupted":
            message = f"Script interrupted: {self.description}\nElapsed time: {elapsed_time:.2f}s"
            return build_response("interrupted", message, builder.build())

        # failed
        error_msg = self.error or "Task execution failed"
        builder.with_error(error_msg)
        message = f"Script failed: {self.description}\nElapsed time: {elapsed_time:.2f}s\nError: {error_msg}"
        return build_response("failed", message, builder.build())

    def get_task_info(self):
        info = {
            "task_id": self.task_id,
            "task_type": "script",
            "description": self.description,
            "status": self.status,
            "elapsed_time": self.get_elapsed_time(),
            "start_time": self.start_time,
            "name": self.script_name,
            "entry_script": self.entry_script,
        }
        if self.status in ["completed", "failed", "interrupted"] and self.end_time is not None:
            info["end_time"] = self.end_time
        if self.status == "failed" and self.error:
            info["error"] = self.error
        return info

    @staticmethod
    def _serialize_result(result):
        if result is None:
            return None
        elif isinstance(result, (str, int, float, bool)):
            return result
        elif isinstance(result, (list, tuple)):
            return [ScriptTask._serialize_result(item) for item in result]
        elif isinstance(result, dict):
            return {k: ScriptTask._serialize_result(v) for k, v in result.items()}
        else:
            return str(result)
