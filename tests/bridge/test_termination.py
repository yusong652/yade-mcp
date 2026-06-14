"""Tests for the async-exception termination helpers."""

import threading
import time
from unittest.mock import patch

import pytest
from yade_mcp_bridge.execution.errors import BridgeTimeout
from yade_mcp_bridge.execution.termination import (
    fire_async_exception,
    is_safe_to_async_raise,
)


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


class TestIsSafeToAsyncRaise:
    def test_normal_worker_thread_is_safe(self):
        started = threading.Event()
        stop = threading.Event()

        def target():
            started.set()
            stop.wait()

        t = threading.Thread(target=target, name="mcp-exec-pump", daemon=True)
        t.start()
        started.wait(timeout=1.0)
        try:
            ok, reason = is_safe_to_async_raise(t.ident)
            assert ok is True
            assert reason == "ok"
        finally:
            stop.set()
            t.join(timeout=1.0)

    def test_dummy_thread_is_rejected(self):
        """Synthesize a Dummy-N by patching _find_thread to return a
        mock with that name — avoids needing boost::python to run."""

        class _FakeThread:
            ident = 12345
            name = "Dummy-7"

            def is_alive(self):
                return True

        with patch(
            "yade_mcp_bridge.execution.termination._find_thread",
            return_value=_FakeThread(),
        ):
            ok, reason = is_safe_to_async_raise(12345)

        assert ok is False
        assert reason == "nested_boost_python_callback"

    def test_main_thread_is_accepted(self):
        """MainThread must be accepted: in Qt mode the pump tick runs
        on MainThread, so refusing injection there would strand the
        pump forever. See termination.py module docstring for the
        safety argument."""
        main_tid = threading.main_thread().ident
        ok, reason = is_safe_to_async_raise(main_tid)
        assert ok is True
        assert reason == "ok"

    def test_dead_thread_is_rejected(self):
        done = threading.Event()

        def target():
            done.set()

        t = threading.Thread(target=target, daemon=True)
        t.start()
        done.wait(timeout=1.0)
        t.join(timeout=1.0)
        assert not t.is_alive()

        ok, reason = is_safe_to_async_raise(t.ident)
        assert ok is False
        assert reason == "thread_not_alive"

    def test_unknown_thread_id_is_rejected(self):
        ok, reason = is_safe_to_async_raise(0xDEADBEEF)
        assert ok is False
        assert reason == "thread_not_alive"


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
