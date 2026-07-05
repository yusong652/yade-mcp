# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Queue and worker for ``execute_task``: runs queued scripts one at a time."""

import logging
import queue
import threading

logger = logging.getLogger("MCP-Bridge")


class TaskExecutor:
    """Run submitted scripts one at a time, in the order they were submitted."""

    def __init__(self):
        self._queue = queue.Queue()
        self._worker = None
        logger.info("TaskExecutor initialized")

    def start(self):
        """Start the worker thread. Does nothing if it is already running."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._workerLoop, daemon=True, name="mcp-task-worker")
        self._worker.start()

    def submit(self, taskId, runTask):
        """Put a script at the end of the queue and return immediately."""
        self._queue.put((taskId, runTask))
        logger.debug("Task queued: %s (queueSize=%d)", taskId, self._queue.qsize())

    def queueSize(self):
        return self._queue.qsize()

    def _workerLoop(self):
        while True:
            taskId, runTask = self._queue.get()
            try:
                # A dedicated thread per script, so interrupt_task's exception
                # injection cannot hit a neighboring task. A task canceled
                # while queued sees its canceled future and returns at once.
                scriptThread = threading.Thread(target=runTask, name=f"script-{taskId}", daemon=True)
                scriptThread.start()
                scriptThread.join()
            except Exception as e:  # the worker must survive to serve the next task
                logger.error("Task worker failed on %s: %s", taskId, e)
