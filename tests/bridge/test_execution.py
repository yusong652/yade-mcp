"""Tests for CodeExecutor queue."""

import threading
from concurrent.futures import Future

from yade_mcp_bridge.execution.executor import CodeExecutor


class TestCodeExecutor:
    def test_submit_and_process(self):
        executor = CodeExecutor()
        future = executor.submit(lambda: 42)
        assert isinstance(future, Future)
        assert executor.queueSize() == 1

        assert executor.runNext() is True
        assert future.result(timeout=1) == 42

    def test_process_empty_queue(self):
        executor = CodeExecutor()
        assert executor.runNext() is False

    def test_processes_one_call_per_invocation(self):
        executor = CodeExecutor()
        executor.submit(lambda: 1)
        executor.submit(lambda: 2)
        executor.submit(lambda: 3)

        # Each call drains exactly one callable; draining the queue takes
        # one call per callable.
        assert executor.runNext() is True
        assert executor.queueSize() == 2
        assert executor.runNext() is True
        assert executor.queueSize() == 1
        assert executor.runNext() is True
        assert executor.queueSize() == 0
        # Queue empty now.
        assert executor.runNext() is False

    def test_exception_captured(self):
        executor = CodeExecutor()

        def fail():
            raise ValueError("boom")

        future = executor.submit(fail)
        executor.runNext()

        assert future.done()
        with __import__("pytest").raises(ValueError, match="boom"):
            future.result()

    def test_submit_with_args(self):
        executor = CodeExecutor()
        future = executor.submit(lambda x, y: x + y, 3, 4)
        executor.runNext()
        assert future.result(timeout=1) == 7

    def test_submit_with_kwargs(self):
        executor = CodeExecutor()
        future = executor.submit(lambda x, y=10: x + y, 5, y=20)
        executor.runNext()
        assert future.result(timeout=1) == 25

    def test_queue_size(self):
        executor = CodeExecutor()
        assert executor.queueSize() == 0
        executor.submit(lambda: None)
        assert executor.queueSize() == 1
        executor.submit(lambda: None)
        assert executor.queueSize() == 2
        executor.runNext()
        assert executor.queueSize() == 1
        executor.runNext()
        assert executor.queueSize() == 0

    def test_cross_thread_submit(self):
        executor = CodeExecutor()
        future = None

        def background():
            nonlocal future
            future = executor.submit(lambda: threading.current_thread().name)

        t = threading.Thread(target=background)
        t.start()
        t.join()

        assert future is not None
        executor.runNext()
        # The callable runs in the thread where runNext is called.
        result = future.result(timeout=1)
        assert result == threading.current_thread().name
