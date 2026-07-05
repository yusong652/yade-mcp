# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Task Manager - Registry, lifecycle, and persistence for long-running tasks."""

import json
import logging
import os
import time
import uuid

from ..paths import DATA_DIR, LOGS_DIR
from ..utils import errorBody, okBody
from .task import DEFAULT_PAGINATION_LIMIT, ScriptTask

logger = logging.getLogger("MCP-Bridge")

TASKS_FILENAME = os.path.join(DATA_DIR, "tasks.json")


DEFAULT_MAX_TASKS = 1024


class TaskManager:
    """Manage long-running task tracking, status queries, and disk persistence."""

    def __init__(self, maxTasks=DEFAULT_MAX_TASKS, onTaskTerminal=None):
        self.tasks = {}
        self._maxTasks = maxTasks
        self.onTaskTerminal = onTaskTerminal

        for d in (DATA_DIR, LOGS_DIR):
            if not os.path.exists(d):
                os.makedirs(d)

        self._loadHistoricalTasks()
        self._pruneOldTasks()
        logger.info("TaskManager initialized")

    def createScriptTask(self, future, scriptName, scriptPath, outputBuffer=None, description=None, taskId=None):
        if taskId is None:
            taskId = uuid.uuid4().hex[:8]
        task = ScriptTask(
            taskId,
            future,
            scriptName,
            scriptPath,
            outputBuffer,
            description,
            onStatusChange=self._onTaskStatusChange,
        )
        self.tasks[taskId] = task
        self._pruneOldTasks()
        self._saveTasks()
        return taskId

    def hasRunningTasks(self):
        for task in self.tasks.values():
            self._refreshRuntimeStatus(task)
            if task.status == "running":
                return True
        return False

    def getTaskStatus(self, taskId, skipNewest=0, limit=DEFAULT_PAGINATION_LIMIT, filterText=None):
        task = self.tasks.get(taskId)
        if not task:
            return errorBody("not_found", f"Task ID not found: {taskId}")
        self._refreshRuntimeStatus(task)
        return task.getStatusResponse(skipNewest=skipNewest, limit=limit, filterText=filterText)

    def listAllTasks(self, offset=0, limit=None):
        for task in self.tasks.values():
            self._refreshRuntimeStatus(task)

        sortedTasks = sorted(self.tasks.values(), key=lambda t: t.startTime, reverse=True)

        totalCount = len(sortedTasks)
        endIdx = offset + limit if limit else totalCount
        paginatedTasks = sortedTasks[offset:endIdx]
        tasksInfo = [task.getTaskInfo() for task in paginatedTasks]

        # total_count + offset/limit are the primitives; a consumer derives
        # displayed_count (len of data) and has_more (offset + len < total),
        # so we don't emit those derived fields.
        return {
            **okBody(data=tasksInfo),
            "pagination": {
                "total_count": totalCount,
                "offset": offset,
                "limit": limit,
            },
        }

    def _refreshRuntimeStatus(self, task):
        if task.status != "pending":
            return
        future = getattr(task, "future", None)
        if future is None:
            return
        try:
            if future.running():
                task.status = "running"
                if task.onStatusChange:
                    task.onStatusChange(task)
        except RuntimeError:
            return

    def shutdown(self):
        """Flush output buffers and mark active tasks before exit."""
        nFlushed = 0
        nStopped = 0
        for task in self.tasks.values():
            if task.outputBuffer:
                try:
                    task.outputBuffer.flush()
                    nFlushed += 1
                except (ValueError, OSError):
                    pass
            # A running task was cut off mid-flight (interrupted); a queued
            # one never started (canceled).
            if task.status == "running":
                task.status = "interrupted"
                task.endTime = time.time()
                task.error = "Bridge shutdown"
                nStopped += 1
            elif task.status == "pending":
                task.status = "canceled"
                task.endTime = time.time()
                nStopped += 1
        self._saveTasks()
        if nFlushed or nStopped:
            logger.info("Shutdown: flushed %d buffer(s), stopped %d task(s)", nFlushed, nStopped)

    def _pruneOldTasks(self):
        """Remove oldest tasks when count exceeds ``_maxTasks``, and delete their log files."""
        if len(self.tasks) <= self._maxTasks:
            return
        sortedTasks = sorted(self.tasks.values(), key=lambda t: t.startTime)
        nRemove = len(self.tasks) - self._maxTasks
        removed = sortedTasks[:nRemove]
        for task in removed:
            if task.logPath:
                try:
                    os.remove(task.logPath)
                except OSError:
                    pass
            del self.tasks[task.taskId]
        logger.info("Pruned %d old task(s), keeping %d", nRemove, len(self.tasks))
        self._saveTasks()

    def _onTaskStatusChange(self, task):
        logger.debug(f"Task {task.taskId} status changed to: {task.status}")
        self._saveTasks()

        if task.status in ("completed", "failed", "interrupted", "canceled") and self.onTaskTerminal:
            try:
                self.onTaskTerminal(task.taskId, task.status)
            except Exception as e:
                logger.warning(f"Failed to broadcast task status: {e}")

    def _saveTasks(self):
        try:
            tasksData = [self._serializeTask(task) for task in self.tasks.values()]
            temp = TASKS_FILENAME + ".tmp"
            with open(temp, "w") as f:
                json.dump(tasksData, f, indent=2)
            os.replace(temp, TASKS_FILENAME)
        except OSError as e:
            logger.error(f"Failed to save tasks: {e}")

    def _loadHistoricalTasks(self):
        if not os.path.exists(TASKS_FILENAME):
            return
        try:
            with open(TASKS_FILENAME) as f:
                allData = json.load(f)
            for taskData in allData:
                task = self._restoreTask(taskData)
                if task:
                    self.tasks[task.taskId] = task
            logger.info("Loaded %d historical task(s)", len(allData))
        except (OSError, json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to load historical tasks: {e}")

    @staticmethod
    def _serializeTask(task):
        return {
            "task_id": task.taskId,
            "task_type": "script",
            "description": task.description,
            "status": task.status,
            "start_time": task.startTime,
            "end_time": task.endTime,
            "script_name": task.scriptName,
            "script_path": task.scriptPath,
            "log_path": task.logPath,
            "error": task.error,
            "error_details": getattr(task, "errorDetails", None),
        }

    @staticmethod
    def _restoreTask(taskData):
        try:
            if taskData.get("status") == "running":
                taskData["status"] = "failed"
            elif taskData.get("status") == "pending":
                # The queue does not survive a restart; a task that was
                # still waiting in it is gone for good.
                taskData["status"] = "canceled"
            return ScriptTask.fromPersisted(taskData)
        except (KeyError, TypeError) as e:
            logger.error(f"Failed to restore task {taskData.get('task_id')}: {e}")
            return None
