# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Task Manager - Registry, lifecycle, and persistence for long-running tasks."""

import json
import logging
import os
import time
import uuid

from ..paths import DATA_DIR, LOGS_DIR
from ..utils import error_body, ok_body
from .task import DEFAULT_PAGINATION_LIMIT, ScriptTask

logger = logging.getLogger("MCP-Bridge")

TASKS_FILENAME = os.path.join(DATA_DIR, "tasks.json")


DEFAULT_MAX_TASKS = 1024


class TaskManager:
    """Manage long-running task tracking, status queries, and disk persistence."""

    def __init__(self, max_tasks=DEFAULT_MAX_TASKS, on_task_terminal=None):
        self.tasks = {}
        self._max_tasks = max_tasks
        self.on_task_terminal = on_task_terminal

        for d in (DATA_DIR, LOGS_DIR):
            if not os.path.exists(d):
                os.makedirs(d)

        self._load_historical_tasks()
        self._prune_old_tasks()
        logger.info("TaskManager initialized")

    def create_script_task(self, future, script_name, script_path, output_buffer=None, description=None, task_id=None):
        if task_id is None:
            task_id = uuid.uuid4().hex[:8]
        task = ScriptTask(
            task_id,
            future,
            script_name,
            script_path,
            output_buffer,
            description,
            on_status_change=self._on_task_status_change,
        )
        self.tasks[task_id] = task
        self._prune_old_tasks()
        self._save_tasks()
        return task_id

    def has_running_tasks(self):
        for task in self.tasks.values():
            self._refresh_runtime_status(task)
            if task.status == "running":
                return True
        return False

    def get_task_status(self, task_id, skip_newest=0, limit=DEFAULT_PAGINATION_LIMIT, filter_text=None):
        task = self.tasks.get(task_id)
        if not task:
            return error_body("not_found", f"Task ID not found: {task_id}")
        self._refresh_runtime_status(task)
        return task.get_status_response(skip_newest=skip_newest, limit=limit, filter_text=filter_text)

    def list_all_tasks(self, offset=0, limit=None):
        for task in self.tasks.values():
            self._refresh_runtime_status(task)

        sorted_tasks = sorted(self.tasks.values(), key=lambda t: t.start_time, reverse=True)

        total_count = len(sorted_tasks)
        end_idx = offset + limit if limit else total_count
        paginated_tasks = sorted_tasks[offset:end_idx]
        tasks_info = [task.get_task_info() for task in paginated_tasks]

        # total_count + offset/limit are the primitives; a consumer derives
        # displayed_count (len of data) and has_more (offset + len < total),
        # so we don't emit those derived fields.
        return {
            **ok_body(data=tasks_info),
            "pagination": {
                "total_count": total_count,
                "offset": offset,
                "limit": limit,
            },
        }

    def _refresh_runtime_status(self, task):
        if task.status != "pending":
            return
        future = getattr(task, "future", None)
        if future is None:
            return
        try:
            if future.running():
                task.status = "running"
                if task.on_status_change:
                    task.on_status_change(task)
        except RuntimeError:
            return

    def shutdown(self):
        """Flush output buffers and mark active tasks before exit."""
        n_flushed = 0
        n_interrupted = 0
        for task in self.tasks.values():
            if task.output_buffer:
                try:
                    task.output_buffer.flush()
                    n_flushed += 1
                except (ValueError, OSError):
                    pass
            if task.status in ("pending", "running"):
                task.status = "interrupted"
                task.end_time = time.time()
                task.error = "Bridge shutdown"
                n_interrupted += 1
        self._save_tasks()
        if n_flushed or n_interrupted:
            logger.info("Shutdown: flushed %d buffer(s), interrupted %d task(s)", n_flushed, n_interrupted)

    def _prune_old_tasks(self):
        """Remove oldest tasks when count exceeds max_tasks, and delete their log files."""
        if len(self.tasks) <= self._max_tasks:
            return
        sorted_tasks = sorted(self.tasks.values(), key=lambda t: t.start_time)
        n_remove = len(self.tasks) - self._max_tasks
        removed = sorted_tasks[:n_remove]
        for task in removed:
            if task.log_path:
                try:
                    os.remove(task.log_path)
                except OSError:
                    pass
            del self.tasks[task.task_id]
        logger.info("Pruned %d old task(s), keeping %d", n_remove, len(self.tasks))
        self._save_tasks()

    def _on_task_status_change(self, task):
        logger.debug(f"Task {task.task_id} status changed to: {task.status}")
        self._save_tasks()

        if task.status in ("completed", "failed", "interrupted") and self.on_task_terminal:
            try:
                self.on_task_terminal(task.task_id, task.status)
            except Exception as e:
                logger.warning(f"Failed to broadcast task status: {e}")

    def _save_tasks(self):
        try:
            tasks_data = [self._serialize_task(task) for task in self.tasks.values()]
            temp = TASKS_FILENAME + ".tmp"
            with open(temp, "w") as f:
                json.dump(tasks_data, f, indent=2)
            os.replace(temp, TASKS_FILENAME)
        except OSError as e:
            logger.error(f"Failed to save tasks: {e}")

    def _load_historical_tasks(self):
        if not os.path.exists(TASKS_FILENAME):
            return
        try:
            with open(TASKS_FILENAME) as f:
                all_data = json.load(f)
            for task_data in all_data:
                task = self._restore_task(task_data)
                if task:
                    self.tasks[task.task_id] = task
            logger.info("Loaded %d historical task(s)", len(all_data))
        except (OSError, json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to load historical tasks: {e}")

    @staticmethod
    def _serialize_task(task):
        return {
            "task_id": task.task_id,
            "task_type": "script",
            "description": task.description,
            "status": task.status,
            "start_time": task.start_time,
            "end_time": task.end_time,
            "script_name": task.script_name,
            "script_path": task.script_path,
            "log_path": task.log_path,
            "error": task.error,
            "error_details": getattr(task, "error_details", None),
        }

    @staticmethod
    def _restore_task(task_data):
        try:
            if task_data.get("status") == "running":
                task_data["status"] = "failed"
            return ScriptTask.from_persisted(task_data)
        except (KeyError, TypeError) as e:
            logger.error(f"Failed to restore task {task_data.get('task_id')}: {e}")
            return None
