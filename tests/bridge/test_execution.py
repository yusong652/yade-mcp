"""Tests for SerialExecutor queue."""

import threading
from concurrent.futures import Future

from yade_mcp_bridge.execution.executor import SerialExecutor


class TestSerialExecutor:
    def test_submit_and_process(self):
        executor = SerialExecutor()
        future = executor.submit(lambda: 42)
        assert isinstance(future, Future)
        assert executor.queue_size() == 1

        assert executor.run_next() is True
        assert future.result(timeout=1) == 42

    def test_process_empty_queue(self):
        executor = SerialExecutor()
        assert executor.run_next() is False

    def test_processes_one_call_per_invocation(self):
        executor = SerialExecutor()
        executor.submit(lambda: 1)
        executor.submit(lambda: 2)
        executor.submit(lambda: 3)

        # Each call drains exactly one callable; draining the queue takes
        # one call per callable.
        assert executor.run_next() is True
        assert executor.queue_size() == 2
        assert executor.run_next() is True
        assert executor.queue_size() == 1
        assert executor.run_next() is True
        assert executor.queue_size() == 0
        # Queue empty now.
        assert executor.run_next() is False

    def test_exception_captured(self):
        executor = SerialExecutor()

        def fail():
            raise ValueError("boom")

        future = executor.submit(fail)
        executor.run_next()

        assert future.done()
        with __import__("pytest").raises(ValueError, match="boom"):
            future.result()

    def test_submit_with_args(self):
        executor = SerialExecutor()
        future = executor.submit(lambda x, y: x + y, 3, 4)
        executor.run_next()
        assert future.result(timeout=1) == 7

    def test_submit_with_kwargs(self):
        executor = SerialExecutor()
        future = executor.submit(lambda x, y=10: x + y, 5, y=20)
        executor.run_next()
        assert future.result(timeout=1) == 25

    def test_queue_size(self):
        executor = SerialExecutor()
        assert executor.queue_size() == 0
        executor.submit(lambda: None)
        assert executor.queue_size() == 1
        executor.submit(lambda: None)
        assert executor.queue_size() == 2
        executor.run_next()
        assert executor.queue_size() == 1
        executor.run_next()
        assert executor.queue_size() == 0

    def test_cross_thread_submit(self):
        executor = SerialExecutor()
        future = None

        def background():
            nonlocal future
            future = executor.submit(lambda: threading.current_thread().name)

        t = threading.Thread(target=background)
        t.start()
        t.join()

        assert future is not None
        executor.run_next()
        # The callable runs in the thread where run_next is called.
        result = future.result(timeout=1)
        assert result == threading.current_thread().name
