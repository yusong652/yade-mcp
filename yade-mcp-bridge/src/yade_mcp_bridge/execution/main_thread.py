# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""YADE Main Thread Executor - Thread-safe task queue for main thread execution.

Provides task queue mechanism to execute YADE operations in the main thread
while the HTTP server runs in background threads.
"""

import logging
import queue
import threading
from concurrent.futures import Future

logger = logging.getLogger("YADE-Bridge")


class MainThreadExecutor:
    """Execute tasks in YADE main thread via queue.

    HTTP request threads submit tasks via submit(), and the main thread
    processes them one at a time via process_task().
    """

    def __init__(self):
        self.task_queue = queue.Queue()
        self.main_thread_id = threading.current_thread().ident
        self.main_thread_name = threading.current_thread().name
        logger.info(
            "MainThreadExecutor initialized (main_thread=%s, id=%s)", self.main_thread_name, self.main_thread_id
        )

    def submit(self, func, *args, **kwargs):
        """Submit a task to the main-thread queue, called from a background
        thread. Returns a ``Future`` to await the result.
        """
        future = Future()
        self.task_queue.put((func, args, kwargs, future))
        logger.debug("Task submitted: %s (queue_size=%d)", func.__name__, self.task_queue.qsize())
        return future

    def process_task(self):
        """Run the next queued task on the main thread, if any.

        Returns True if a task was dequeued (ran, failed, or was already
        cancelled), False if the queue was empty. The pump calls this once
        per tick (see pump.py): handling a single task per tick hands
        control back to the Qt event loop between tasks, keeping the GUI
        responsive.
        """
        current_thread_id = threading.current_thread().ident
        if current_thread_id != self.main_thread_id:
            # In console mode the pump runs on a background daemon thread, so
            # the task does not actually execute on the main thread. Safe due
            # to the GIL and the thread-safe queue; logged for diagnostics.
            logger.debug(
                "process_task() called from non-main thread: current=%s, expected=%s",
                threading.current_thread().name,
                self.main_thread_name,
            )

        try:
            func, args, kwargs, future = self.task_queue.get_nowait()
        except queue.Empty:
            return False

        if not future.set_running_or_notify_cancel():
            logger.debug("Task skipped (cancelled): %s", func.__name__)
            return True

        try:
            result = func(*args, **kwargs)
            future.set_result(result)
            logger.debug("Task completed: %s", func.__name__)
        except Exception as e:
            future.set_exception(e)
            logger.error("Task failed: %s - %s", func.__name__, e)
        return True

    def queue_size(self):
        return self.task_queue.qsize()
