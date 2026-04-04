"""Tests for bridge interrupt signal mechanism."""

from yade_mcp_bridge.signals import (
    clear_current_task,
    clear_interrupt,
    is_interrupt_requested,
    request_interrupt,
    set_current_task,
)


class TestSignals:
    def setup_method(self):
        """Reset global state between tests."""
        clear_current_task()
        # Clear any leftover interrupt flags
        from yade_mcp_bridge.signals import _interrupt_requested
        _interrupt_requested.clear()

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
