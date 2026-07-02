"""Tests for bridge interrupt signal mechanism."""

import threading
import time

import pytest
from yade_mcp_bridge.runtime.signals import (
    clearCurrentTask,
    clearInterrupt,
    getCurrentTask,
    getExecThread,
    isTaskInterruptRequested,
    registerExecThread,
    requestInterrupt,
    setCurrentTask,
    unregisterExecThread,
)


class TestSignals:
    def setup_method(self):
        """Reset global state between tests."""
        clearCurrentTask()
        # Clear any leftover interrupt flags
        from yade_mcp_bridge.runtime.signals import _execThreadIds, _interruptRequested

        _interruptRequested.clear()
        _execThreadIds.clear()

    def test_set_and_clear_current_task(self):
        setCurrentTask("task-1")
        from yade_mcp_bridge.runtime.signals import _currentTaskId

        assert _currentTaskId == "task-1"

        clearCurrentTask()
        from yade_mcp_bridge.runtime.signals import _currentTaskId

        assert _currentTaskId is None

    def test_request_and_check_interrupt_by_id(self):
        assert not isTaskInterruptRequested("task-1")
        requestInterrupt("task-1")
        assert isTaskInterruptRequested("task-1")
        assert not isTaskInterruptRequested("task-2")

    def test_clear_interrupt(self):
        requestInterrupt("task-1")
        assert isTaskInterruptRequested("task-1")

        clearInterrupt("task-1")
        assert not isTaskInterruptRequested("task-1")

    def test_clear_interrupt_nonexistent(self):
        clearInterrupt("nonexistent")  # should not raise


class TestPeekCurrentTask:
    def setup_method(self):
        clearCurrentTask()

    def test_peek_returns_none_when_unset(self):
        assert getCurrentTask() is None

    def test_peek_returns_current_task(self):
        setCurrentTask("task-A")
        assert getCurrentTask() == "task-A"

    def test_save_and_restore_pattern(self):
        """Simulates execute_code nested inside a running task:
        outer sets A, inner saves/sets B, inner clears + restores A."""
        setCurrentTask("task-A")
        assert getCurrentTask() == "task-A"

        # Enter inner execute_code
        prev = getCurrentTask()
        setCurrentTask("request-B")
        assert getCurrentTask() == "request-B"

        # Exit inner execute_code
        clearCurrentTask()
        if prev is not None:
            setCurrentTask(prev)

        assert getCurrentTask() == "task-A"


class TestExecThreadRegistry:
    def setup_method(self):
        from yade_mcp_bridge.runtime.signals import _execThreadIds

        _execThreadIds.clear()

    def test_register_and_get(self):
        assert getExecThread("req-1") is None
        registerExecThread("req-1", 42)
        assert getExecThread("req-1") == 42

    def test_unregister_is_idempotent(self):
        registerExecThread("req-1", 42)
        unregisterExecThread("req-1")
        assert getExecThread("req-1") is None
        unregisterExecThread("req-1")  # second call: no raise
        assert getExecThread("req-1") is None

    def test_multiple_requests_coexist(self):
        # Use live thread ids so the scrub-on-register doesn't drop them.
        tid = threading.main_thread().ident
        registerExecThread("req-1", tid)
        registerExecThread("req-2", tid)
        assert getExecThread("req-1") == tid
        assert getExecThread("req-2") == tid

    def test_scrub_removes_dead_thread_entries(self):
        """When we register a new entry, stale entries pointing to
        dead threads should be dropped — cheap leak defense."""
        # Record a live thread's id — this one stays.
        live_ident = threading.main_thread().ident
        registerExecThread("live-req", live_ident)

        # Inject a synthetic stale entry bypassing the scrub.
        from yade_mcp_bridge.runtime.signals import _execThreadIds, _execThreadLock

        with _execThreadLock:
            _execThreadIds["stale-req"] = 0xDEADBEEF

        # New register triggers scrub.
        registerExecThread("new-req", live_ident)

        assert getExecThread("stale-req") is None  # scrubbed
        assert getExecThread("live-req") == live_ident  # preserved
        assert getExecThread("new-req") == live_ident  # just added


class TestSimHoldRendezvous:
    """The execute_code consistent-snapshot hold: a handshake between the
    snippet (pump thread) and the PyRunner tick (sim thread). Exercised here
    with a fake cycle thread — no YADE needed."""

    def setup_method(self):
        from yade_mcp_bridge.runtime.signals import (
            _cycleHeld,
            _holdLocal,
            _holdWanted,
            _snippetReleased,
        )

        _holdWanted.clear()
        _cycleHeld.clear()
        _snippetReleased.clear()
        if getattr(_holdLocal, "active", False):
            _holdLocal.active = False

    def _spawn_cycle(self):
        """Fake sim-cycle thread: bumps ``state['count']`` each iteration
        and calls the cooperative brake ``holdIfWanted``."""
        from yade_mcp_bridge.runtime.signals import holdIfWanted

        state = {"count": 0, "stop": False}

        def _cycle():
            while not state["stop"]:
                state["count"] += 1
                holdIfWanted()
                time.sleep(0.001)

        t = threading.Thread(target=_cycle, name="fake-cycle", daemon=True)
        t.start()
        return state, t

    def _stop_cycle(self, state, t):
        state["stop"] = True
        t.join(timeout=2.0)

    def test_hold_freezes_cycle_then_resumes(self):
        from yade_mcp_bridge.runtime.signals import holdSim

        state, t = self._spawn_cycle()
        try:
            time.sleep(0.05)  # let the cycle advance
            with holdSim() as held:
                assert held is True
                c1 = state["count"]
                time.sleep(0.05)  # cycle is held → count must NOT advance
                c2 = state["count"]
                assert c1 == c2, f"cycle advanced while held: {c1} -> {c2}"
            time.sleep(0.05)  # released → cycle advances again
            assert state["count"] > c2
        finally:
            self._stop_cycle(state, t)

    def test_snippet_holds_sim_only_while_holding(self):
        from yade_mcp_bridge.runtime.signals import holdSim, snippetHoldsSim

        assert snippetHoldsSim() is False
        # No cycle thread → won't hold; short acquire timeout keeps it fast.
        with holdSim(acquireTimeoutS=0.05) as held:
            assert held is False  # nothing to hold
            assert snippetHoldsSim() is True
        assert snippetHoldsSim() is False

    def test_hold_releases_cycle_on_exception(self):
        from yade_mcp_bridge.runtime.signals import holdSim

        state, t = self._spawn_cycle()
        try:
            time.sleep(0.05)
            with pytest.raises(ValueError), holdSim() as held:
                assert held is True
                raise ValueError("boom")
            # holdSim's finally must have resumed the cycle
            time.sleep(0.05)
            c = state["count"]
            time.sleep(0.05)
            assert state["count"] > c
        finally:
            self._stop_cycle(state, t)

    def test_hold_max_hold_returns_without_release(self):
        """If the snippet never releases, the brake still returns after
        ``max_hold_s`` so a hung snippet cannot freeze the sim forever."""
        from yade_mcp_bridge.runtime.signals import _holdWanted, holdIfWanted

        _holdWanted.set()
        done = threading.Event()

        def _hold():
            holdIfWanted(maxHoldS=0.05)
            done.set()

        t = threading.Thread(target=_hold, name="hold-maxhold", daemon=True)
        t.start()
        assert done.wait(timeout=2.0) is True  # returned despite no release
        _holdWanted.clear()

    def test_hold_limit_follows_hold_sim(self):
        """The tick's hold limit follows ``holdSim(max_hold_s=...)``: with a
        tiny limit and no release, the tick returns almost immediately."""
        from yade_mcp_bridge.runtime.signals import holdIfWanted, holdSim

        done = threading.Event()

        def _tick():
            holdIfWanted()  # no explicit limit — must pick up the snippet's
            done.set()

        with holdSim(acquireTimeoutS=0.05, maxHoldS=0.05):
            t = threading.Thread(target=_tick, name="hold-limit", daemon=True)
            t.start()
            # Snippet holds (no release) — the tick must return via the
            # snippet's 0.05s limit, far inside the 30s default.
            assert done.wait(timeout=2.0) is True
        t.join(timeout=1.0)

    def test_hold_limit_resets_after_hold_sim(self):
        """The per-hold limit must not leak into the next hold."""
        from yade_mcp_bridge.runtime import signals

        with signals.holdSim(acquireTimeoutS=0.01, maxHoldS=0.05):
            assert signals._holdMaxS == 0.05
        assert signals._holdMaxS is None
