"""Tests for bridge interrupt signal mechanism."""

import threading
import time

import pytest
from yade_mcp_bridge.runtime.signals import (
    clear_current_task,
    clear_interrupt,
    get_current_task,
    get_exec_thread,
    is_task_interrupt_requested,
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
        assert get_current_task() is None

    def test_peek_returns_current_task(self):
        set_current_task("task-A")
        assert get_current_task() == "task-A"

    def test_save_and_restore_pattern(self):
        """Simulates execute_code nested inside a running task:
        outer sets A, inner saves/sets B, inner clears + restores A."""
        set_current_task("task-A")
        assert get_current_task() == "task-A"

        # Enter inner execute_code
        prev = get_current_task()
        set_current_task("request-B")
        assert get_current_task() == "request-B"

        # Exit inner execute_code
        clear_current_task()
        if prev is not None:
            set_current_task(prev)

        assert get_current_task() == "task-A"


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


class TestSimHoldRendezvous:
    """The execute_code consistent-snapshot hold: a handshake between the
    snippet (pump thread) and the PyRunner tick (sim thread). Exercised here
    with a fake cycle thread — no YADE needed."""

    def setup_method(self):
        from yade_mcp_bridge.runtime.signals import (
            _cycle_held,
            _hold_local,
            _hold_wanted,
            _snippet_released,
        )

        _hold_wanted.clear()
        _cycle_held.clear()
        _snippet_released.clear()
        if getattr(_hold_local, "active", False):
            _hold_local.active = False

    def _spawn_cycle(self):
        """Fake sim-cycle thread: bumps ``state['count']`` each iteration
        and calls the cooperative brake ``hold_if_wanted``."""
        from yade_mcp_bridge.runtime.signals import hold_if_wanted

        state = {"count": 0, "stop": False}

        def _cycle():
            while not state["stop"]:
                state["count"] += 1
                hold_if_wanted()
                time.sleep(0.001)

        t = threading.Thread(target=_cycle, name="fake-cycle", daemon=True)
        t.start()
        return state, t

    def _stop_cycle(self, state, t):
        state["stop"] = True
        t.join(timeout=2.0)

    def test_hold_freezes_cycle_then_resumes(self):
        from yade_mcp_bridge.runtime.signals import hold_sim

        state, t = self._spawn_cycle()
        try:
            time.sleep(0.05)  # let the cycle advance
            with hold_sim() as held:
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
        from yade_mcp_bridge.runtime.signals import hold_sim, snippet_holds_sim

        assert snippet_holds_sim() is False
        # No cycle thread → won't hold; short acquire timeout keeps it fast.
        with hold_sim(acquire_timeout_s=0.05) as held:
            assert held is False  # nothing to hold
            assert snippet_holds_sim() is True
        assert snippet_holds_sim() is False

    def test_hold_releases_cycle_on_exception(self):
        from yade_mcp_bridge.runtime.signals import hold_sim

        state, t = self._spawn_cycle()
        try:
            time.sleep(0.05)
            with pytest.raises(ValueError), hold_sim() as held:
                assert held is True
                raise ValueError("boom")
            # hold_sim's finally must have resumed the cycle
            time.sleep(0.05)
            c = state["count"]
            time.sleep(0.05)
            assert state["count"] > c
        finally:
            self._stop_cycle(state, t)

    def test_hold_max_hold_returns_without_release(self):
        """If the snippet never releases, the brake still returns after
        ``max_hold_s`` so a hung snippet cannot freeze the sim forever."""
        from yade_mcp_bridge.runtime.signals import _hold_wanted, hold_if_wanted

        _hold_wanted.set()
        done = threading.Event()

        def _hold():
            hold_if_wanted(max_hold_s=0.05)
            done.set()

        t = threading.Thread(target=_hold, name="hold-maxhold", daemon=True)
        t.start()
        assert done.wait(timeout=2.0) is True  # returned despite no release
        _hold_wanted.clear()

    def test_hold_limit_follows_hold_sim(self):
        """The tick's hold limit follows ``hold_sim(max_hold_s=...)``: with a
        tiny limit and no release, the tick returns almost immediately."""
        from yade_mcp_bridge.runtime.signals import hold_if_wanted, hold_sim

        done = threading.Event()

        def _tick():
            hold_if_wanted()  # no explicit limit — must pick up the snippet's
            done.set()

        with hold_sim(acquire_timeout_s=0.05, max_hold_s=0.05):
            t = threading.Thread(target=_tick, name="hold-limit", daemon=True)
            t.start()
            # Snippet holds (no release) — the tick must return via the
            # snippet's 0.05s limit, far inside the 30s default.
            assert done.wait(timeout=2.0) is True
        t.join(timeout=1.0)

    def test_hold_limit_resets_after_hold_sim(self):
        """The per-hold limit must not leak into the next hold."""
        from yade_mcp_bridge.runtime import signals

        with signals.hold_sim(acquire_timeout_s=0.01, max_hold_s=0.05):
            assert signals._hold_max_s == 0.05
        assert signals._hold_max_s is None
