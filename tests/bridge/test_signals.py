"""Tests for bridge interrupt signal mechanism."""

import threading
import time

import pytest
from yade_mcp_bridge.runtime.signals import (
    clear_current_task,
    clear_interrupt,
    get_exec_thread,
    is_current_interrupt_requested,
    is_task_interrupt_requested,
    peek_current_task,
    register_exec_thread,
    request_interrupt,
    set_current_task,
    unregister_exec_thread,
)


class TestSignals:
    def setup_method(self):
        """Reset global state between tests."""
        clear_current_task()
        # Clear any leftover interrupt flags
        from yade_mcp_bridge.runtime.signals import _exec_thread_ids, _interrupt_requested

        _interrupt_requested.clear()
        _exec_thread_ids.clear()

    def test_set_and_clear_current_task(self):
        set_current_task("task-1")
        from yade_mcp_bridge.runtime.signals import _current_task_id

        assert _current_task_id == "task-1"

        clear_current_task()
        from yade_mcp_bridge.runtime.signals import _current_task_id

        assert _current_task_id is None

    def test_request_and_check_interrupt_by_id(self):
        assert not is_task_interrupt_requested("task-1")
        request_interrupt("task-1")
        assert is_task_interrupt_requested("task-1")
        assert not is_task_interrupt_requested("task-2")

    def test_is_current_interrupt_requested_uses_current_task(self):
        set_current_task("task-1")
        assert not is_current_interrupt_requested()

        request_interrupt("task-1")
        assert is_current_interrupt_requested()

    def test_is_current_interrupt_requested_no_current_task(self):
        assert not is_current_interrupt_requested()

    def test_clear_interrupt(self):
        request_interrupt("task-1")
        assert is_task_interrupt_requested("task-1")

        clear_interrupt("task-1")
        assert not is_task_interrupt_requested("task-1")

    def test_clear_interrupt_nonexistent(self):
        clear_interrupt("nonexistent")  # should not raise


class TestPeekCurrentTask:
    def setup_method(self):
        clear_current_task()

    def test_peek_returns_none_when_unset(self):
        assert peek_current_task() is None

    def test_peek_returns_current_task(self):
        set_current_task("task-A")
        assert peek_current_task() == "task-A"

    def test_save_and_restore_pattern(self):
        """Simulates execute_code nested inside a running task:
        outer sets A, inner saves/sets B, inner clears + restores A."""
        set_current_task("task-A")
        assert peek_current_task() == "task-A"

        # Enter inner execute_code
        prev = peek_current_task()
        set_current_task("request-B")
        assert peek_current_task() == "request-B"

        # Exit inner execute_code
        clear_current_task()
        if prev is not None:
            set_current_task(prev)

        assert peek_current_task() == "task-A"


class TestExecThreadRegistry:
    def setup_method(self):
        from yade_mcp_bridge.runtime.signals import _exec_thread_ids

        _exec_thread_ids.clear()

    def test_register_and_get(self):
        assert get_exec_thread("req-1") is None
        register_exec_thread("req-1", 42)
        assert get_exec_thread("req-1") == 42

    def test_unregister_is_idempotent(self):
        register_exec_thread("req-1", 42)
        unregister_exec_thread("req-1")
        assert get_exec_thread("req-1") is None
        unregister_exec_thread("req-1")  # second call: no raise
        assert get_exec_thread("req-1") is None

    def test_multiple_requests_coexist(self):
        # Use live thread ids so the scrub-on-register doesn't drop them.
        tid = threading.main_thread().ident
        register_exec_thread("req-1", tid)
        register_exec_thread("req-2", tid)
        assert get_exec_thread("req-1") == tid
        assert get_exec_thread("req-2") == tid

    def test_scrub_removes_dead_thread_entries(self):
        """When we register a new entry, stale entries pointing to
        dead threads should be dropped — cheap leak defense."""
        # Record a live thread's id — this one stays.
        live_ident = threading.main_thread().ident
        register_exec_thread("live-req", live_ident)

        # Inject a synthetic stale entry bypassing the scrub.
        from yade_mcp_bridge.runtime.signals import _exec_thread_ids, _exec_thread_lock

        with _exec_thread_lock:
            _exec_thread_ids["stale-req"] = 0xDEADBEEF

        # New register triggers scrub.
        register_exec_thread("new-req", live_ident)

        assert get_exec_thread("stale-req") is None  # scrubbed
        assert get_exec_thread("live-req") == live_ident  # preserved
        assert get_exec_thread("new-req") == live_ident  # just added


class TestSimPauseRendezvous:
    """The execute_code consistent-snapshot window: a handshake between the
    REPL (pump thread) and the PyRunner tick (sim thread). Exercised here
    with a fake cycle thread — no YADE needed."""

    def setup_method(self):
        from yade_mcp_bridge.runtime.signals import (
            _cycle_parked,
            _pause_wanted,
            _repl_released,
            _window_local,
        )

        _pause_wanted.clear()
        _cycle_parked.clear()
        _repl_released.clear()
        if getattr(_window_local, "active", False):
            _window_local.active = False

    def _spawn_cycle(self):
        """Fake sim-cycle thread: bumps ``state['count']`` each iteration
        and calls the cooperative brake ``park_if_pause_wanted``."""
        from yade_mcp_bridge.runtime.signals import park_if_pause_wanted

        state = {"count": 0, "stop": False}

        def _cycle():
            while not state["stop"]:
                state["count"] += 1
                park_if_pause_wanted()
                time.sleep(0.001)

        t = threading.Thread(target=_cycle, name="fake-cycle", daemon=True)
        t.start()
        return state, t

    def _stop_cycle(self, state, t):
        state["stop"] = True
        t.join(timeout=2.0)

    def test_window_freezes_cycle_then_resumes(self):
        from yade_mcp_bridge.runtime.signals import sim_paused_window

        state, t = self._spawn_cycle()
        try:
            time.sleep(0.05)  # let the cycle advance
            with sim_paused_window() as parked:
                assert parked is True
                c1 = state["count"]
                time.sleep(0.05)  # cycle is parked → count must NOT advance
                c2 = state["count"]
                assert c1 == c2, f"cycle advanced while parked: {c1} -> {c2}"
            time.sleep(0.05)  # released → cycle advances again
            assert state["count"] > c2
        finally:
            self._stop_cycle(state, t)

    def test_repl_holds_sim_only_inside_window(self):
        from yade_mcp_bridge.runtime.signals import repl_holds_sim, sim_paused_window

        assert repl_holds_sim() is False
        # No cycle thread → won't park; short acquire timeout keeps it fast.
        with sim_paused_window(acquire_timeout_s=0.05) as parked:
            assert parked is False  # nothing to park
            assert repl_holds_sim() is True
        assert repl_holds_sim() is False

    def test_window_releases_cycle_on_exception(self):
        from yade_mcp_bridge.runtime.signals import sim_paused_window

        state, t = self._spawn_cycle()
        try:
            time.sleep(0.05)
            with pytest.raises(ValueError), sim_paused_window() as parked:
                assert parked is True
                raise ValueError("boom")
            # the window's finally must have resumed the cycle
            time.sleep(0.05)
            c = state["count"]
            time.sleep(0.05)
            assert state["count"] > c
        finally:
            self._stop_cycle(state, t)

    def test_park_max_hold_returns_without_release(self):
        """If the REPL never releases, the brake still returns after
        ``max_hold_s`` so a hung REPL cannot freeze the sim forever."""
        from yade_mcp_bridge.runtime.signals import _pause_wanted, park_if_pause_wanted

        _pause_wanted.set()
        done = threading.Event()

        def _park():
            park_if_pause_wanted(max_hold_s=0.05)
            done.set()

        t = threading.Thread(target=_park, name="park-maxhold", daemon=True)
        t.start()
        assert done.wait(timeout=2.0) is True  # returned despite no release
        _pause_wanted.clear()
        t.join(timeout=1.0)
