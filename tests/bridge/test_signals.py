"""Tests for bridge interrupt signal mechanism."""

import threading

from yade_mcp_bridge.signals import (
    clear_current_task,
    clear_interrupt,
    get_exec_thread,
    is_interrupt_requested,
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
        from yade_mcp_bridge.signals import _exec_thread_ids, _interrupt_requested

        _interrupt_requested.clear()
        _exec_thread_ids.clear()

    def test_set_and_clear_current_task(self):
        set_current_task("task-1")
        from yade_mcp_bridge.signals import _current_task_id
        assert _current_task_id == "task-1"

        clear_current_task()
        from yade_mcp_bridge.signals import _current_task_id
        assert _current_task_id is None

    def test_request_and_check_interrupt_by_id(self):
        assert not is_interrupt_requested("task-1")
        request_interrupt("task-1")
        assert is_interrupt_requested("task-1")
        assert not is_interrupt_requested("task-2")

    def test_is_interrupt_requested_uses_current_task(self):
        set_current_task("task-1")
        assert not is_interrupt_requested()

        request_interrupt("task-1")
        assert is_interrupt_requested()

    def test_is_interrupt_requested_no_current_task(self):
        assert not is_interrupt_requested()

    def test_clear_interrupt(self):
        request_interrupt("task-1")
        assert is_interrupt_requested("task-1")

        clear_interrupt("task-1")
        assert not is_interrupt_requested("task-1")

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
        from yade_mcp_bridge.signals import _exec_thread_ids

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
        from yade_mcp_bridge.signals import _exec_thread_ids, _exec_thread_lock

        with _exec_thread_lock:
            _exec_thread_ids["stale-req"] = 0xDEADBEEF

        # New register triggers scrub.
        register_exec_thread("new-req", live_ident)

        assert get_exec_thread("stale-req") is None  # scrubbed
        assert get_exec_thread("live-req") == live_ident  # preserved
        assert get_exec_thread("new-req") == live_ident  # just added
