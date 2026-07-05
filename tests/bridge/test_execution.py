"""Tests for the CodeExecutor and TaskExecutor queues."""

import threading
from concurrent.futures import Future

from yade_mcp_bridge.execution.codeExecutor import CodeExecutor
from yade_mcp_bridge.execution.taskExecutor import TaskExecutor


class TestCodeExecutor:
    def test_submit_and_process(self):
        codeExecutor = CodeExecutor()
        future = codeExecutor.submit(lambda: 42)
        assert isinstance(future, Future)
        assert codeExecutor.queueSize() == 1

        assert codeExecutor.runNext() is True
        assert future.result(timeout=1) == 42

    def test_process_empty_queue(self):
        codeExecutor = CodeExecutor()
        assert codeExecutor.runNext() is False

    def test_processes_one_call_per_invocation(self):
        codeExecutor = CodeExecutor()
        codeExecutor.submit(lambda: 1)
        codeExecutor.submit(lambda: 2)
        codeExecutor.submit(lambda: 3)

        # Each call drains exactly one callable; draining the queue takes
        # one call per callable.
        assert codeExecutor.runNext() is True
        assert codeExecutor.queueSize() == 2
        assert codeExecutor.runNext() is True
        assert codeExecutor.queueSize() == 1
        assert codeExecutor.runNext() is True
        assert codeExecutor.queueSize() == 0
        # Queue empty now.
        assert codeExecutor.runNext() is False

    def test_exception_captured(self):
        codeExecutor = CodeExecutor()

        def fail():
            raise ValueError("boom")

        future = codeExecutor.submit(fail)
        codeExecutor.runNext()

        assert future.done()
        with __import__("pytest").raises(ValueError, match="boom"):
            future.result()

    def test_submit_with_args(self):
        codeExecutor = CodeExecutor()
        future = codeExecutor.submit(lambda x, y: x + y, 3, 4)
        codeExecutor.runNext()
        assert future.result(timeout=1) == 7

    def test_submit_with_kwargs(self):
        codeExecutor = CodeExecutor()
        future = codeExecutor.submit(lambda x, y=10: x + y, 5, y=20)
        codeExecutor.runNext()
        assert future.result(timeout=1) == 25

    def test_queue_size(self):
        codeExecutor = CodeExecutor()
        assert codeExecutor.queueSize() == 0
        codeExecutor.submit(lambda: None)
        assert codeExecutor.queueSize() == 1
        codeExecutor.submit(lambda: None)
        assert codeExecutor.queueSize() == 2
        codeExecutor.runNext()
        assert codeExecutor.queueSize() == 1
        codeExecutor.runNext()
        assert codeExecutor.queueSize() == 0

    def test_cross_thread_submit(self):
        codeExecutor = CodeExecutor()
        future = None

        def background():
            nonlocal future
            future = codeExecutor.submit(lambda: threading.current_thread().name)

        t = threading.Thread(target=background)
        t.start()
        t.join()

        assert future is not None
        codeExecutor.runNext()
        # The callable runs in the thread where runNext is called.
        result = future.result(timeout=1)
        assert result == threading.current_thread().name


class TestTaskExecutor:
    def test_runs_tasks_in_submit_order(self):
        taskExecutor = TaskExecutor()
        order = []
        done = threading.Event()

        def make(n, last=False):
            def run():
                order.append(n)
                if last:
                    done.set()

            return run

        taskExecutor.submit("t1", make(1))
        taskExecutor.submit("t2", make(2))
        taskExecutor.submit("t3", make(3, last=True))
        taskExecutor.start()

        assert done.wait(5)
        assert order == [1, 2, 3]

    def test_one_task_at_a_time(self):
        """The second task must not start while the first is still running."""
        taskExecutor = TaskExecutor()
        firstRunning = threading.Event()
        release = threading.Event()
        secondStarted = threading.Event()

        def first():
            firstRunning.set()
            release.wait(5)

        taskExecutor.submit("a", first)
        taskExecutor.submit("b", secondStarted.set)
        taskExecutor.start()

        assert firstRunning.wait(5)
        assert not secondStarted.wait(0.2)
        release.set()
        assert secondStarted.wait(5)

    def test_each_task_gets_its_own_named_thread(self):
        taskExecutor = TaskExecutor()
        names = []
        done = threading.Event()

        def record():
            names.append(threading.current_thread().name)
            done.set()

        taskExecutor.submit("abc123", record)
        taskExecutor.start()

        assert done.wait(5)
        assert names == ["script-abc123"]

    def test_start_twice_keeps_single_worker(self):
        taskExecutor = TaskExecutor()
        taskExecutor.start()
        worker = taskExecutor._worker
        taskExecutor.start()
        assert taskExecutor._worker is worker
