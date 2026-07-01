# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Queue and execution mechanism for ``execute_code``.

``submit()`` enqueues code; a single pump drains the FIFO queue via
``run_next()``.
"""

import logging
import queue
from concurrent.futures import Future

logger = logging.getLogger("MCP-Bridge")


class SerialExecutor:
    """Run ``execute_code`` submissions serially via a FIFO queue."""

    def __init__(self):
        self._queue = queue.Queue()
        logger.info("SerialExecutor initialized")

    def submit(self, func, *args, **kwargs):
        """Queue code to run on the pump thread. Returns a ``Future`` to
        await the result.
        """
        future = Future()
        self._queue.put((func, args, kwargs, future))
        logger.debug("Callable submitted: %s (queue_size=%d)", func.__name__, self._queue.qsize())
        return future

    def run_next(self):
        """Run the next queued callable, if any.

        Returns True if one was dequeued (ran, failed, or was already
        cancelled), False if the queue was empty. The pump calls this once
        per tick (see pump.py): handling a single callable per tick hands
        control back to the Qt event loop between calls, keeping the GUI
        responsive.
        """
        try:
            func, args, kwargs, future = self._queue.get_nowait()
        except queue.Empty:
            return False

        if not future.set_running_or_notify_cancel():
            logger.debug("Callable skipped (cancelled): %s", func.__name__)
            return True

        try:
            result = func(*args, **kwargs)
            future.set_result(result)
            logger.debug("Callable completed: %s", func.__name__)
        except Exception as e:
            future.set_exception(e)
            logger.error("Callable failed: %s - %s", func.__name__, e)
        return True

    def queue_size(self):
        return self._queue.qsize()
