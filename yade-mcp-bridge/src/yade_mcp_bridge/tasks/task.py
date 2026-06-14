# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Script Task - Lifecycle management for long-running YADE script execution."""

import logging
import os
import time

from ..utils import TaskDataBuilder, ok_body

logger = logging.getLogger("MCP-Bridge")

DEFAULT_PAGINATION_LIMIT = 64


class ScriptTask:
    """Task for Python script execution with real-time output capture.

    Status values:
    - "pending": Task queued, waiting for main thread
    - "running": Task currently executing
    - "completed": Task finished successfully
    - "failed": Task finished with error
    - "interrupted": Task was interrupted by user
    """

    def __init__(
        self, task_id, future, script_name, entry_script, output_buffer=None, description=None, on_status_change=None
    ):
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
        # Structured error details captured from the executor (user-frame
        # traceback, exception type, overflow log path). Promoted into
        # check_task_status responses so the LLM has full debugging context
        # without chasing log files.
        self.error_details: dict | None = None

        self.log_path = None
        if output_buffer and hasattr(output_buffer, "get_path"):
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
        task.error_details = task_data.get("error_details")
        task.future = None
        task.output_buffer = None
        task.on_status_change = None
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
                    diag = {
                        k: result[k]
                        for k in ("exception_type", "traceback", "traceback_truncated", "log_file")
                        if k in result
                    }
                    self.error_details = diag or None
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

    def get_paginated_output(self, skip_newest=0, limit=DEFAULT_PAGINATION_LIMIT, filter_text=None):
        """Return (output_text, pagination) paginating the task log on the bridge side.

        Reads the complete log file, optionally filters by substring, then
        extracts a tail-biased window: skip `skip_newest` lines from the end,
        then take up to `limit` lines backwards from there. Pagination metadata
        reflects the full log (or the filtered view), so MCP can trust it.
        """
        if self.output_buffer:
            try:
                self.output_buffer.flush()
            except (ValueError, OSError):
                pass

        full = ""
        if self.log_path and os.path.exists(self.log_path):
            try:
                with open(self.log_path, encoding="utf-8", errors="replace") as f:
                    full = f.read()
            except OSError as e:
                logger.warning(f"Failed to read log file: {e}")

        lines = full.splitlines()
        if filter_text:
            lines = [line for line in lines if filter_text in line]

        total_lines = len(lines)
        start_idx = max(0, total_lines - limit - skip_newest)
        end_idx = max(0, total_lines - skip_newest)
        selected = lines[start_idx:end_idx]

        # `line_range` against `total_lines` fully determines whether older
        # (range start > 1) or newer (range end < total) lines exist, so we
        # don't emit separate has_older/has_newer booleans.
        pagination = {
            "total_lines": total_lines,
            "line_range": f"{start_idx + 1}-{end_idx}" if selected else "0-0",
        }

        text = "\n".join(selected) if selected else ""
        return text, pagination

    def _create_data_builder(self):
        return TaskDataBuilder(self.task_id, "script", self.entry_script, self.description)

    def get_status_response(self, skip_newest=0, limit=DEFAULT_PAGINATION_LIMIT, filter_text=None):
        current_status = self.status
        elapsed_time = self.get_elapsed_time()
        output_text, pagination = self.get_paginated_output(
            skip_newest=skip_newest, limit=limit, filter_text=filter_text
        )

        builder = self._create_data_builder().with_status(current_status)

        if current_status in ("pending", "running"):
            builder.with_timing(self.start_time, elapsed_time=elapsed_time)
            builder.with_output(output_text)
            builder.with_pagination(pagination)
            return ok_body(data=builder.build())

        # completed / failed / interrupted
        builder.with_timing(self.start_time, self.end_time, elapsed_time)
        builder.with_output(output_text)
        builder.with_pagination(pagination)

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
            return ok_body(data=builder.build())

        if current_status == "interrupted":
            return ok_body(data=builder.build())

        # failed — the task failed, but the *request* succeeded (ok: True).
        # The error and lifecycle status both live in task data (data.status ==
        # "failed", data.error), never as a request-level error{}.
        error_msg = self.error or "Task execution failed"
        builder.with_error(error_msg)
        data = builder.build()
        if self.error_details:
            data["error_details"] = self.error_details
        return ok_body(data=data)

    def get_task_info(self):
        info = {
            "task_id": self.task_id,
            "task_type": "script",
            "description": self.description,
            "status": self.status,
            "elapsed_time": self.get_elapsed_time(),
            "start_time": self.start_time,
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
