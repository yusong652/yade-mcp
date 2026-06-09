"""Tests for execution tools: response envelopes and error branches.

The bridge client is mocked, so these run with no YADE runtime and no
``yade_mcp_bridge`` package. They cover what each tool does with a
bridge reply — envelope shape, status mapping, error handling — not
wire transport (that's ``test_bridge_client.py``) nor the real bridge
protocol (that's the bridge's own ``tests/bridge`` suite).

Each tool is invoked via its registered ``FunctionTool.fn`` — the real
``with_context``-wrapped coroutine — so the envelope path runs exactly
as in production, without the MCP transport or argument-schema layers
in the way.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yade_mcp.server import mcp

TOOL_MODULES = (
    "yade_mcp.tools.execute_code",
    "yade_mcp.tools.execute_task",
    "yade_mcp.tools.check_task_status",
    "yade_mcp.tools.list_tasks",
    "yade_mcp.tools.interrupt_task",
)


@pytest.fixture
def bridge():
    """Yield an AsyncMock bridge client wired into every tool module.

    ``get_bridge_client`` is patched in each tool's namespace to return
    this mock, and context injection is stubbed so envelopes carry no
    ``_context``. Configure replies per test, e.g.
    ``bridge.execute_code.return_value = {...}``.
    """
    client = AsyncMock()
    # These are synchronous on the real client; keep them sync here so
    # the check_task_status listener calls don't leave unawaited coroutines.
    client.listen_for_task = MagicMock()
    client.unlisten_task = MagicMock()

    async def _get_client():
        return client

    with ExitStack() as stack:
        for module in TOOL_MODULES:
            stack.enter_context(patch(f"{module}.get_bridge_client", _get_client))
        stack.enter_context(patch("yade_mcp.bridge.context.fetch_bridge_context", AsyncMock(return_value=None)))
        yield client


async def _call(tool_name, **kwargs):
    """Invoke a registered tool's underlying coroutine."""
    tool = await mcp.get_tool(tool_name)
    return await tool.fn(**kwargs)


# =========================================================================
# execute_code
# =========================================================================


class TestExecuteCode:
    async def test_success_returns_output(self, bridge):
        bridge.execute_code.return_value = {"ok": True, "data": {"output": "hello\n"}}
        data = await _call("yade_execute_code", code="print('hello')")
        assert data["ok"] is True
        assert data["data"]["output"] == "hello\n"

    async def test_success_includes_eval_result(self, bridge):
        bridge.execute_code.return_value = {"ok": True, "data": {"output": "", "result": 3}}
        data = await _call("yade_execute_code", code="1 + 2")
        assert data["ok"] is True
        assert data["data"]["result"] == 3

    async def test_error_maps_to_error_envelope(self, bridge):
        bridge.execute_code.return_value = {
            "ok": False,
            "error": {"code": "execute_code_error", "message": "boom"},
        }
        data = await _call("yade_execute_code", code="1/0")
        assert data["ok"] is False
        assert data["error"]["code"] == "execute_code_error"

    async def test_terminated_maps_to_error_code(self, bridge):
        bridge.execute_code.return_value = {
            "ok": False,
            "error": {"code": "terminated", "message": "aborted after 1000ms", "details": {"method": "async_exc"}},
            "data": {"output": "partial"},
        }
        data = await _call("yade_execute_code", code="while True: pass")
        assert data["ok"] is False
        assert data["error"]["code"] == "terminated"
        # the bridge's long message is preserved as the reason
        assert "aborted" in data["error"]["details"]["reason"]
        assert data["data"]["output"] == "partial"

    async def test_legacy_status_envelope_still_handled(self, bridge):
        # Backward-compat: a bridge that predates the ok envelope sends a
        # bare ``status`` string. The tool must still classify it correctly.
        bridge.execute_code.return_value = {"status": "success", "data": {"output": "ok\n"}}
        data = await _call("yade_execute_code", code="print('ok')")
        assert data["ok"] is True
        assert data["data"]["output"] == "ok\n"

    async def test_connectivity_error_is_handled(self, bridge):
        bridge.execute_code.side_effect = ConnectionError("connection refused")
        data = await _call("yade_execute_code", code="print('x')")
        assert data["ok"] is False
        assert data["error"]["code"] == "bridge_unavailable"


# =========================================================================
# execute_task
# =========================================================================


class TestExecuteTask:
    async def test_pending_new_envelope(self, bridge):
        bridge.execute_task.return_value = {"ok": True, "data": {"status": "pending", "task_id": "x"}}
        data = await _call("yade_execute_task", entry_script="/tmp/sim.py", description="drained triaxial")
        assert data["ok"] is True
        # task_id is generated tool-side (uuid), not echoed from the bridge.
        assert isinstance(data["data"]["task_id"], str) and data["data"]["task_id"]
        assert data["data"]["task_status"] == "pending"
        assert data["data"]["entry_script"] == "/tmp/sim.py"

    async def test_pending_legacy_status_envelope(self, bridge):
        # Legacy bridge: submit success signalled by a bare status:"pending".
        bridge.execute_task.return_value = {"status": "pending", "message": "submitted"}
        data = await _call("yade_execute_task", entry_script="/tmp/sim.py", description="x")
        assert data["ok"] is True
        assert data["data"]["task_status"] == "pending"

    async def test_submit_error_maps_to_error(self, bridge):
        # script.py SUBMIT errors now emit a structured error{} (point #3):
        # script_not_found / script_read_error / submit_failed.
        bridge.execute_task.return_value = {
            "ok": False,
            "error": {"code": "script_not_found", "message": "Script file not found: /tmp/missing.py"},
        }
        data = await _call("yade_execute_task", entry_script="/tmp/missing.py", description="x")
        assert data["ok"] is False
        assert data["error"]["code"] == "script_not_found"

    async def test_submit_error_legacy_status_maps_to_error(self, bridge):
        # Legacy bridge: submit failure was a bare status:"error".
        bridge.execute_task.return_value = {"status": "error", "message": "no such file"}
        data = await _call("yade_execute_task", entry_script="/tmp/missing.py", description="x")
        assert data["ok"] is False

    async def test_missing_field_error_is_lifted(self, bridge):
        bridge.execute_task.return_value = {
            "ok": False,
            "error": {"code": "missing_field", "message": "task_id required", "details": {"field": "task_id"}},
        }
        data = await _call("yade_execute_task", entry_script="/tmp/sim.py", description="x")
        assert data["ok"] is False
        assert data["error"]["code"] == "missing_field"

    async def test_connectivity_error_is_handled(self, bridge):
        bridge.execute_task.side_effect = ConnectionError("connection refused")
        data = await _call("yade_execute_task", entry_script="/tmp/sim.py", description="x")
        assert data["ok"] is False
        assert data["error"]["code"] == "bridge_unavailable"


# =========================================================================
# check_task_status
# =========================================================================


class TestCheckTaskStatus:
    async def test_not_found_maps_to_error(self, bridge):
        bridge.check_task_status.return_value = {
            "ok": False,
            "error": {"code": "not_found", "message": "Task ID not found: nope"},
        }
        data = await _call("yade_check_task_status", task_id="nope", wait_seconds=0)
        assert data["ok"] is False
        assert data["error"]["code"] == "not_found"

    async def test_legacy_not_found_status_still_handled(self, bridge):
        bridge.check_task_status.return_value = {"status": "not_found"}
        data = await _call("yade_check_task_status", task_id="nope", wait_seconds=0)
        assert data["ok"] is False
        assert data["error"]["code"] == "not_found"

    async def test_running_new_envelope(self, bridge):
        # New wire: bare ToolEnvelope (ok:true) with the lifecycle status
        # nested in data.status, not at the top level.
        bridge.check_task_status.return_value = {
            "ok": True,
            "data": {
                "status": "running",
                "output": "line 1\nline 2\n",
                "pagination": {"total_lines": 2, "line_range": "1-2"},
            },
        }
        data = await _call("yade_check_task_status", task_id="t1", wait_seconds=0)
        assert data["ok"] is True
        assert data["data"]["task_status"] == "running"
        assert data["data"]["output"] == "line 1\nline 2\n"
        assert data["data"]["pagination"]["total_lines"] == 2

    async def test_failed_task_is_ok_envelope_with_error(self, bridge):
        # A failed *task* is a successful *request*: ok:true, task_status:failed,
        # the script error surfaced as task data (not a request-level error{}).
        bridge.check_task_status.return_value = {
            "ok": True,
            "data": {"status": "failed", "output": "boom\n", "error": "RuntimeError: boom"},
        }
        data = await _call("yade_check_task_status", task_id="t1", wait_seconds=0)
        assert data["ok"] is True
        assert data["data"]["task_status"] == "failed"
        assert "boom" in data["data"]["error"]

    async def test_running_legacy_status_envelope(self, bridge):
        # Legacy bridge: lifecycle without ok, just the status string.
        bridge.check_task_status.return_value = {
            "status": "running",
            "data": {
                "output": "line 1\nline 2\n",
                "pagination": {"total_lines": 2, "line_range": "1-2"},
            },
        }
        data = await _call("yade_check_task_status", task_id="t1", wait_seconds=0)
        assert data["ok"] is True
        assert data["data"]["task_status"] == "running"

    async def test_legacy_success_status_is_normalized(self, bridge):
        bridge.check_task_status.return_value = {"status": "success", "data": {"output": "done\n"}}
        data = await _call("yade_check_task_status", task_id="t1", wait_seconds=0)
        assert data["ok"] is True
        # bridge "success" → normalized "completed"
        assert data["data"]["task_status"] == "completed"


# =========================================================================
# list_tasks
# =========================================================================


class TestListTasks:
    async def test_empty_list_new_envelope(self, bridge):
        bridge.list_tasks.return_value = {"ok": True, "data": []}
        data = await _call("yade_list_tasks")
        assert data["ok"] is True
        assert data["data"]["tasks"] == []
        assert data["data"]["total_count"] == 0

    async def test_empty_list_legacy_envelope(self, bridge):
        bridge.list_tasks.return_value = {"status": "success", "data": []}
        data = await _call("yade_list_tasks")
        assert data["ok"] is True
        assert data["data"]["tasks"] == []

    async def test_normalizes_task_status(self, bridge):
        bridge.list_tasks.return_value = {
            "ok": True,
            "data": [
                {"task_id": "t1", "status": "completed", "source": "agent", "entry_script": "a.py"},
            ],
            "pagination": {"total_count": 1},
        }
        data = await _call("yade_list_tasks")
        assert data["ok"] is True
        task = data["data"]["tasks"][0]
        assert task["task_id"] == "t1"
        assert task["status"] == "completed"
        assert task["entry_script"] == "a.py"

    async def test_legacy_status_is_normalized(self, bridge):
        # Legacy bridge task entry with status:"success" → normalized "completed".
        bridge.list_tasks.return_value = {
            "status": "success",
            "data": [
                {"task_id": "t1", "status": "success", "source": "agent", "entry_script": "a.py"},
            ],
            "pagination": {"total_count": 1},
        }
        data = await _call("yade_list_tasks")
        assert data["ok"] is True
        assert data["data"]["tasks"][0]["status"] == "completed"

    async def test_failure_maps_to_error(self, bridge):
        bridge.list_tasks.return_value = {"status": "error", "message": "bad"}
        data = await _call("yade_list_tasks")
        assert data["ok"] is False


# =========================================================================
# interrupt_task
# =========================================================================


class TestInterruptTask:
    async def test_success_new_envelope(self, bridge):
        bridge.interrupt_task.return_value = {
            "ok": True,
            "data": {"task_id": "t1", "interrupt_requested": True, "method": "flag_only"},
        }
        data = await _call("yade_interrupt_task", task_id="t1")
        assert data["ok"] is True
        assert data["data"]["interrupt_requested"] is True
        assert data["data"]["method"] == "flag_only"

    async def test_success_legacy_status_envelope(self, bridge):
        bridge.interrupt_task.return_value = {"status": "success", "message": "ok"}
        data = await _call("yade_interrupt_task", task_id="t1")
        assert data["ok"] is True
        assert data["data"]["interrupt_requested"] is True

    async def test_not_found_maps_to_error(self, bridge):
        bridge.interrupt_task.return_value = {
            "ok": False,
            "error": {"code": "not_found", "message": "Task not found: t1"},
        }
        data = await _call("yade_interrupt_task", task_id="t1")
        assert data["ok"] is False
        assert data["error"]["code"] == "not_found"

    async def test_already_terminal_maps_to_error(self, bridge):
        bridge.interrupt_task.return_value = {
            "ok": False,
            "error": {"code": "already_terminal", "message": "Task already in terminal state: t1 (status: completed)"},
        }
        data = await _call("yade_interrupt_task", task_id="t1")
        assert data["ok"] is False
        assert data["error"]["code"] == "already_terminal"
