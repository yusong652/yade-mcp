"""Tests for MainThreadExecutor task queue."""

import threading
from concurrent.futures import Future

from yade_mcp_bridge.execution.main_thread import MainThreadExecutor


class TestMainThreadExecutor:
    def test_submit_and_process(self):
        executor = MainThreadExecutor()
        future = executor.submit(lambda: 42)
        assert isinstance(future, Future)
        assert executor.queue_size() == 1

        assert executor.process_task() is True
        assert future.result(timeout=1) == 42

    def test_process_empty_queue(self):
        executor = MainThreadExecutor()
        assert executor.process_task() is False

    def test_processes_one_task_per_call(self):
        executor = MainThreadExecutor()
        executor.submit(lambda: 1)
        executor.submit(lambda: 2)
        executor.submit(lambda: 3)

        # Each call drains exactly one task; draining the queue takes
        # one call per task.
        assert executor.process_task() is True
        assert executor.queue_size() == 2
        assert executor.process_task() is True
        assert executor.queue_size() == 1
        assert executor.process_task() is True
        assert executor.queue_size() == 0
        # Queue empty now.
        assert executor.process_task() is False

    def test_task_exception_captured(self):
        executor = MainThreadExecutor()

        def fail():
            raise ValueError("boom")

        future = executor.submit(fail)
        executor.process_task()

        assert future.done()
        with __import__("pytest").raises(ValueError, match="boom"):
            future.result()

    def test_submit_with_args(self):
        executor = MainThreadExecutor()
        future = executor.submit(lambda x, y: x + y, 3, 4)
        executor.process_task()
        assert future.result(timeout=1) == 7

    def test_submit_with_kwargs(self):
        executor = MainThreadExecutor()
        future = executor.submit(lambda x, y=10: x + y, 5, y=20)
        executor.process_task()
        assert future.result(timeout=1) == 25

    def test_queue_size(self):
        executor = MainThreadExecutor()
        assert executor.queue_size() == 0
        executor.submit(lambda: None)
        assert executor.queue_size() == 1
        executor.submit(lambda: None)
        assert executor.queue_size() == 2
        executor.process_task()
        assert executor.queue_size() == 1
        executor.process_task()
        assert executor.queue_size() == 0

    def test_cross_thread_submit(self):
        executor = MainThreadExecutor()
        future = None

        def background():
            nonlocal future
            future = executor.submit(lambda: threading.current_thread().name)

        t = threading.Thread(target=background)
        t.start()
        t.join()

        assert future is not None
        executor.process_task()
        # Task runs in the thread where process_task is called.
        result = future.result(timeout=1)
        assert result == threading.current_thread().name
