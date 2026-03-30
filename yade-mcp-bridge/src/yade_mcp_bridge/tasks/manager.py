"""Task Manager - Registry, lifecycle, and persistence for long-running tasks."""

import json
import os
import uuid
import logging

from .task import ScriptTask

logger = logging.getLogger("YADE-Bridge")

DATA_DIR = ".yade-mcp"
LOGS_DIR = os.path.join(DATA_DIR, "logs")
TASKS_FILENAME = os.path.join(DATA_DIR, "tasks.json")


class TaskManager:
    """Manage long-running task tracking, status queries, and disk persistence."""

    def __init__(self):
        self.tasks = {}

        for d in (DATA_DIR, LOGS_DIR):
            if not os.path.exists(d):
                os.makedirs(d)

        self._load_historical_tasks()
        logger.info("TaskManager initialized")

    def create_script_task(self, future, script_name, entry_script, output_buffer=None, description=None, task_id=None):
        if task_id is None:
            task_id = uuid.uuid4().hex[:8]
        task = ScriptTask(
            task_id, future, script_name, entry_script,
            output_buffer, description, on_status_change=self._on_task_status_change,
        )
        self.tasks[task_id] = task
        self._save_tasks()
        return task_id

    def has_running_tasks(self):
        for task in self.tasks.values():
            self._refresh_runtime_status(task)
            if task.status == "running":
                return True
        return False

    def get_task_status(self, task_id):
        task = self.tasks.get(task_id)
        if not task:
            return {
                "status": "not_found",
                "message": "Task ID not found: {}".format(task_id),
                "data": None
            }
        self._refresh_runtime_status(task)
        return task.get_status_response()

    def list_all_tasks(self, offset=0, limit=None):
        for task in self.tasks.values():
            self._refresh_runtime_status(task)

        sorted_tasks = sorted(self.tasks.values(), key=lambda t: t.start_time, reverse=True)

        total_count = len(sorted_tasks)
        end_idx = offset + limit if limit else total_count
        paginated_tasks = sorted_tasks[offset:end_idx]
        tasks_info = [task.get_task_info() for task in paginated_tasks]

        return {
            "status": "success",
            "message": "Found {} tracked task(s)".format(total_count),
            "data": tasks_info,
            "pagination": {
                "total_count": total_count,
                "displayed_count": len(tasks_info),
                "offset": offset,
                "limit": limit,
                "has_more": end_idx < total_count
            }
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
        except Exception:
            return

    def _on_task_status_change(self, task):
        logger.debug("Task {} status changed to: {}".format(task.task_id, task.status))
        self._save_tasks()

    def _save_tasks(self):
        try:
            tasks_data = [self._serialize_task(task) for task in self.tasks.values()]
            temp = TASKS_FILENAME + ".tmp"
            with open(temp, 'w') as f:
                json.dump(tasks_data, f, indent=2)
            os.replace(temp, TASKS_FILENAME)
        except Exception as e:
            logger.error("Failed to save tasks: {}".format(e))

    def _load_historical_tasks(self):
        if not os.path.exists(TASKS_FILENAME):
            return
        try:
            with open(TASKS_FILENAME, 'r') as f:
                all_data = json.load(f)
            for task_data in all_data:
                task = self._restore_task(task_data)
                if task:
                    self.tasks[task.task_id] = task
            logger.info("Loaded %d historical task(s)", len(all_data))
        except Exception as e:
            logger.error("Failed to load historical tasks: {}".format(e))

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
            "entry_script": task.entry_script,
            "log_path": task.log_path,
            "error": task.error,
        }

    @staticmethod
    def _restore_task(task_data):
        try:
            if task_data.get("status") == "running":
                task_data["status"] = "failed"
            return ScriptTask.from_persisted(task_data)
        except Exception as e:
            logger.error("Failed to restore task {}: {}".format(task_data.get("task_id"), e))
            return None
