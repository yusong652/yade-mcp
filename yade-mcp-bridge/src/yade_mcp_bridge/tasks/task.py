# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Script Task - Lifecycle management for long-running YADE script execution."""

import logging
import os
import time

from ..utils import TaskDataBuilder, okBody

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
        self, taskId, future, scriptName, scriptPath, outputBuffer=None, description=None, onStatusChange=None
    ):
        self.taskId = taskId
        self.future = future
        self.description = description or ""
        self.scriptName = scriptName
        self.scriptPath = scriptPath
        self.outputBuffer = outputBuffer
        self.startTime = time.time()
        self.endTime = None
        self._status = "pending"
        self.onStatusChange = onStatusChange
        self.error = None
        # Structured error details captured from the executor (user-frame
        # traceback, exception type, overflow log path). Promoted into
        # check_task_status responses so the LLM has full debugging context
        # without chasing log files.
        self.errorDetails = None

        self.logPath = None
        if outputBuffer and hasattr(outputBuffer, "getPath"):
            self.logPath = outputBuffer.getPath()

        future.add_done_callback(self._onComplete)

        logger.info("Script task registered: %s (id=%s)", scriptName, taskId)

    @classmethod
    def fromPersisted(cls, taskData):
        """Create a task from persisted data (no Future or buffer)."""
        task = cls.__new__(cls)
        task.taskId = taskData["task_id"]
        task.description = taskData["description"]
        task.scriptName = taskData.get("script_name", "")
        task.scriptPath = taskData.get("script_path") or taskData.get("entry_script") or ""
        task._status = taskData["status"]
        task.startTime = taskData["start_time"]
        task.endTime = taskData.get("end_time")
        task.logPath = taskData.get("log_path")
        task.error = taskData.get("error")
        task.errorDetails = taskData.get("error_details")
        task.future = None
        task.outputBuffer = None
        task.onStatusChange = None
        return task

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        self._status = value

    def _onComplete(self, f):
        self.endTime = time.time()
        try:
            result = f.result(timeout=0)
            if isinstance(result, dict):
                resultStatus = result.get("status")
                if resultStatus == "error":
                    self.status = "failed"
                    self.error = result.get("message", "Task execution failed")
                    diag = {
                        k: result[k]
                        for k in ("exception_type", "traceback", "traceback_truncated", "log_file")
                        if k in result
                    }
                    self.errorDetails = diag or None
                elif resultStatus == "interrupted":
                    self.status = "interrupted"
                else:
                    self.status = "completed"
            else:
                self.status = "completed"
        except Exception as e:
            self.status = "failed"
            self.error = str(e)

        if self.onStatusChange:
            try:
                self.onStatusChange(self)
            except Exception as e:
                logger.warning(f"Status change callback failed: {e}")

    def getElapsedTime(self):
        if self.endTime is not None:
            return self.endTime - self.startTime
        if self.future is None:
            return 0.0
        return time.time() - self.startTime

    def getPaginatedOutput(self, skipNewest=0, limit=DEFAULT_PAGINATION_LIMIT, filterText=None):
        """Return (outputText, pagination) paginating the task log on the bridge side.

        Reads the complete log file, optionally filters by substring, then
        extracts a page from the tail: skip ``skipNewest`` lines from the end,
        then take up to ``limit`` lines backwards from there. Pagination metadata
        reflects the full log (or the filtered view), so MCP can trust it.
        """
        if self.outputBuffer:
            try:
                self.outputBuffer.flush()
            except (ValueError, OSError):
                pass

        full = ""
        if self.logPath and os.path.exists(self.logPath):
            try:
                with open(self.logPath, encoding="utf-8", errors="replace") as f:
                    full = f.read()
            except OSError as e:
                logger.warning(f"Failed to read log file: {e}")

        lines = full.splitlines()
        if filterText:
            lines = [line for line in lines if filterText in line]

        totalLines = len(lines)
        startIdx = max(0, totalLines - limit - skipNewest)
        endIdx = max(0, totalLines - skipNewest)
        selected = lines[startIdx:endIdx]

        # `line_range` against `total_lines` fully determines whether older
        # (range start > 1) or newer (range end < total) lines exist, so we
        # don't emit separate has_older/has_newer booleans.
        pagination = {
            "total_lines": totalLines,
            "line_range": f"{startIdx + 1}-{endIdx}" if selected else "0-0",
        }

        text = "\n".join(selected) if selected else ""
        return text, pagination

    def _createDataBuilder(self):
        return TaskDataBuilder(self.taskId, "script", self.scriptPath, self.description)

    def getStatusResponse(self, skipNewest=0, limit=DEFAULT_PAGINATION_LIMIT, filterText=None):
        currentStatus = self.status
        elapsedTime = self.getElapsedTime()
        outputText, pagination = self.getPaginatedOutput(skipNewest=skipNewest, limit=limit, filterText=filterText)

        builder = self._createDataBuilder().withStatus(currentStatus)

        if currentStatus in ("pending", "running"):
            builder.withTiming(self.startTime, elapsedTime=elapsedTime)
            builder.withOutput(outputText)
            builder.withPagination(pagination)
            return okBody(data=builder.build())

        # completed / failed / interrupted
        builder.withTiming(self.startTime, self.endTime, elapsedTime)
        builder.withOutput(outputText)
        builder.withPagination(pagination)

        if currentStatus == "completed":
            resultData = None
            if self.future:
                try:
                    result = self.future.result(timeout=0)
                    if isinstance(result, dict):
                        resultData = result.get("result")
                except Exception:
                    pass
            builder.withResult(self._serializeResult(resultData))
            return okBody(data=builder.build())

        if currentStatus == "interrupted":
            return okBody(data=builder.build())

        # failed — the task failed, but the *request* succeeded (ok: True).
        # The error and lifecycle status both live in task data (data.status ==
        # "failed", data.error), never as a request-level error{}.
        errorMsg = self.error or "Task execution failed"
        builder.withError(errorMsg)
        data = builder.build()
        if self.errorDetails:
            data["error_details"] = self.errorDetails
        return okBody(data=data)

    def getTaskInfo(self):
        info = {
            "task_id": self.taskId,
            "task_type": "script",
            "description": self.description,
            "status": self.status,
            "elapsed_time": self.getElapsedTime(),
            "start_time": self.startTime,
            "script_path": self.scriptPath,
        }
        if self.status in ["completed", "failed", "interrupted"] and self.endTime is not None:
            info["end_time"] = self.endTime
        if self.status == "failed" and self.error:
            info["error"] = self.error
        return info

    @staticmethod
    def _serializeResult(result):
        if result is None:
            return None
        elif isinstance(result, (str, int, float, bool)):
            return result
        elif isinstance(result, (list, tuple)):
            return [ScriptTask._serializeResult(item) for item in result]
        elif isinstance(result, dict):
            return {k: ScriptTask._serializeResult(v) for k, v in result.items()}
        else:
            return str(result)
