"""Tests for the async-exception termination helpers."""

import threading
import time

import pytest
from yade_mcp_bridge.execution.errors import BridgeTimeout
from yade_mcp_bridge.execution.termination import fire_async_exception


class TestFireAsyncException:
    def test_terminates_tight_python_loop(self):
        """A pure Python loop hits a bytecode edge every instruction,
        so the injected exception fires immediately. The target catches
        it to keep pytest's thread-exception warning quiet."""
        caught: list[BaseException] = []

        def target():
            try:
                while True:
                    pass  # no GIL yield, but every bytecode is an edge
            except BaseException as e:
                caught.append(e)

        t = threading.Thread(target=target, daemon=True)
        t.start()
        time.sleep(0.05)  # let the loop get running

        affected = fire_async_exception(t.ident, BridgeTimeout)
        assert affected == 1

        t.join(timeout=1.0)
        assert not t.is_alive(), "thread should have been terminated by async exc"
        assert len(caught) == 1
        assert isinstance(caught[0], BridgeTimeout)

    def test_sleep_in_c_blocks_until_return(self):
        """``time.sleep`` holds in a C call that does not poll for
        async exceptions. The injection queues; it fires at the next
        bytecode, which is after sleep returns. This is the 'stuck in
        C' limitation documented in the plan."""
        caught: list[BaseException] = []

        def target():
            try:
                time.sleep(0.5)
                _ = 1 + 1  # bytecode edge where exception fires
            except BaseException as e:
                caught.append(e)

        t = threading.Thread(target=target, daemon=True)
        t.start()
        time.sleep(0.05)

        affected = fire_async_exception(t.ident, BridgeTimeout)
        assert affected == 1

        # Must wait longer than sleep(0.5) — sleep blocks the exception.
        t.join(timeout=1.5)
        assert not t.is_alive()
        assert len(caught) == 1
        assert isinstance(caught[0], BridgeTimeout)

    def test_returns_zero_for_nonexistent_thread_id(self):
        """Invalid tids yield 0 (no matching PyThreadState)."""
        affected = fire_async_exception(0xDEADBEEF, BridgeTimeout)
        assert affected == 0

    def test_refuses_dummy_thread_target(self):
        """A ``Dummy-N`` target (a non-Python thread borrowing the GIL,
        e.g. YADE's sim thread) is refused without injecting — injecting
        would unwind into boost::python → C++ FATAL. A real thread renamed
        ``Dummy-*`` stands in for a boost::python thread so no YADE is
        needed; the guard keys purely off the name."""
        stop = threading.Event()

        def target():
            stop.wait()

        t = threading.Thread(target=target, name="Dummy-7", daemon=True)
        t.start()
        try:
            affected = fire_async_exception(t.ident, BridgeTimeout)
            assert affected == 0  # refused, no injection
        finally:
            stop.set()
            t.join(timeout=1.0)


@pytest.mark.parametrize("exc_cls", [BridgeTimeout, InterruptedError])
def test_fire_async_with_different_exception_classes(exc_cls):
    """The helper doesn't care about the exception class — confirm it
    works for both our custom ``BridgeTimeout`` and a stock exception."""

    def target():
        try:
            while True:
                pass
        except BaseException:
            pass

    t = threading.Thread(target=target, daemon=True)
    t.start()
    time.sleep(0.05)

    fire_async_exception(t.ident, exc_cls)
    t.join(timeout=1.0)
    assert not t.is_alive()
