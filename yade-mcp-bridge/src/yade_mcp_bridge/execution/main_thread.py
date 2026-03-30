"""YADE Main Thread Executor - Thread-safe task queue for main thread execution.

Provides task queue mechanism to execute YADE operations in the main thread
while WebSocket server runs in background thread.
"""

import queue
import logging
import threading
from concurrent.futures import Future


logger = logging.getLogger("YADE-Bridge")


class MainThreadExecutor:
    """Execute tasks in YADE main thread via queue.

    WebSocket server (background thread) submits tasks via submit(),
    main thread processes tasks via process_tasks().
    """

    def __init__(self):
        self.task_queue = queue.Queue()
        self.main_thread_id = threading.current_thread().ident
        self.main_thread_name = threading.current_thread().name
        logger.info(
            "MainThreadExecutor initialized (main_thread=%s, id=%s)",
            self.main_thread_name, self.main_thread_id
        )

    def submit(self, func, *args, **kwargs):
        """Submit task to main thread queue (called from background thread).

        Returns:
            Future: Future object to await result
        """
        future = Future()
        self.task_queue.put((func, args, kwargs, future))
        logger.debug(
            "Task submitted: %s (queue_size=%d)",
            func.__name__, self.task_queue.qsize()
        )
        return future

    def process_tasks(self, max_tasks=None):
        """Process pending tasks in queue (called from main thread).

        Args:
            max_tasks: Optional maximum number of tasks to process.
                None = process all pending tasks.

        Returns:
            int: Number of tasks processed
        """
        task_limit = None
        if max_tasks is not None:
            try:
                parsed = int(max_tasks)
            except (TypeError, ValueError):
                parsed = 1
            if parsed > 0:
                task_limit = parsed

        current_thread_id = threading.current_thread().ident
        is_main_thread = (current_thread_id == self.main_thread_id)

        if not is_main_thread:
            # Expected when called from YADE's PyRunner during O.run() —
            # C++ simulation thread invokes Python callback on a Dummy thread.
            # Still safe due to Python GIL and thread-safe queue.
            logger.debug(
                "process_tasks() called from non-main thread: "
                "current=%s, expected=%s",
                threading.current_thread().name, self.main_thread_name
            )

        processed_count = 0

        while True:
            if task_limit is not None and processed_count >= task_limit:
                break
            try:
                func, args, kwargs, future = self.task_queue.get_nowait()
                processed_count += 1

                try:
                    if not future.set_running_or_notify_cancel():
                        logger.debug("Task skipped (cancelled): %s", func.__name__)
                        continue

                    result = func(*args, **kwargs)
                    future.set_result(result)
                    logger.debug("Task completed: %s", func.__name__)

                except Exception as e:
                    future.set_exception(e)
                    logger.error("Task failed: %s - %s", func.__name__, e)

            except queue.Empty:
                break

        if processed_count > 0:
            logger.debug("Processed %d task(s)", processed_count)

        return processed_count

    def queue_size(self):
        return self.task_queue.qsize()
