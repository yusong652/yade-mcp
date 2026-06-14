# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Serial executor - thread-safe queue that runs submitted callables one at a time.

HTTP request threads concurrently ``submit()`` callables; a single pump
drains them in FIFO order via ``run_next()``. The single-consumer invariant
serializes YADE operations so they never race on engine state.

Which thread the pump runs on depends on the runtime mode (see pump.py):

* gui mode: the Qt event loop ticks ``run_next()`` on the main thread.
  This is mandatory — Qt objects have thread affinity, so anything touching
  the GUI (``yade.qt.View()`` etc.) must run on the main thread.
* console mode: a background daemon thread ticks ``run_next()``. There are
  no live Qt objects to violate affinity with, so any thread is safe (the
  GIL and the thread-safe queue keep it correct).

The executor itself only guarantees serial FIFO execution; the owning
thread is a property of the pump, not of this class.
"""

import logging
import queue
import threading
from concurrent.futures import Future

logger = logging.getLogger("MCP-Bridge")


class SerialExecutor:
    """Run submitted callables serially via a queue.

    HTTP request threads submit callables via submit(); the pump drains them
    one at a time via run_next().
    """

    def __init__(self):
        self._queue = queue.Queue()
        # Records the constructing thread, used only for the diagnostic warning
        # in run_next(). Not an enforced constraint.
        self.owner_thread_id = threading.current_thread().ident
        self.owner_thread_name = threading.current_thread().name
        logger.info("SerialExecutor initialized (owner_thread=%s, id=%s)", self.owner_thread_name, self.owner_thread_id)

    def submit(self, func, *args, **kwargs):
        """Submit a callable to the serial queue, called from a background
        thread. Returns a ``Future`` to await the result.
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
        current_thread_id = threading.current_thread().ident
        if current_thread_id != self.owner_thread_id:
            # In console mode the pump runs on a background daemon thread, so
            # the callable does not run on the constructing thread. Safe due
            # to the GIL and the thread-safe queue; logged for diagnostics.
            logger.debug(
                "run_next() called from a different thread: current=%s, owner=%s",
                threading.current_thread().name,
                self.owner_thread_name,
            )

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
