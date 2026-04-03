"""Integration tests for MCP tools with live bridge.

Prerequisites:
    - YADE bridge running (yade_mcp_bridge.start() in YADE console)
    - Run with: uv run pytest tests/test_tools_integration.py -v
"""

import asyncio
import json

import pytest
from fastmcp import Client

from yade_mcp.server import mcp

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    """Shared MCP client for all integration tests."""
    async def _run():
        async with Client(mcp) as c:
            yield c
    # Use event loop to manage the async context manager
    loop = asyncio.new_event_loop()
    c = loop.run_until_complete(Client(mcp).__aenter__())
    yield c
    loop.run_until_complete(c.__aexit__(None, None, None))
    loop.close()


def _get_data(result):
    """Extract data dict from tool result."""
    payload = result.data if hasattr(result, "data") else result
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, list) and len(payload) > 0:
        payload = payload[0]
        if hasattr(payload, "text"):
            payload = json.loads(payload.text)
    return payload


# =========================================================================
# Documentation tools (no bridge needed)
# =========================================================================

class TestBrowseApi:
    @pytest.mark.asyncio
    async def test_browse_root(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_browse_api", {"path": ""})
            data = _get_data(result)
            assert data["ok"] is True
            assert len(data["data"]["entries"]) > 0

    @pytest.mark.asyncio
    async def test_browse_category(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_browse_api", {"path": ""})
            data = _get_data(result)
            # Get first category and browse into it
            first = data["data"]["entries"][0]["name"]
            result2 = await client.call_tool("yade_browse_api", {"path": first})
            data2 = _get_data(result2)
            assert data2["ok"] is True

    @pytest.mark.asyncio
    async def test_browse_invalid_path(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_browse_api", {"path": "nonexistent/path"})
            data = _get_data(result)
            # Should return empty or error gracefully
            assert "ok" in data


class TestQueryApi:
    @pytest.mark.asyncio
    async def test_search_sphere(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_query_api", {"query": "sphere"})
            data = _get_data(result)
            assert data["ok"] is True
            assert len(data["data"]["entries"]) > 0

    @pytest.mark.asyncio
    async def test_search_no_results(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_query_api", {
                "query": "xyznonexistentterm123",
            })
            data = _get_data(result)
            assert data["ok"] is True


# =========================================================================
# Execution tools (bridge required)
# =========================================================================

class TestExecuteCode:
    @pytest.mark.asyncio
    async def test_simple_print(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_execute_code", {
                "code": "print('hello from yade')",
                "timeout": 5,
            })
            data = _get_data(result)
            assert data["ok"] is True
            assert "hello from yade" in data["data"]["output"]

    @pytest.mark.asyncio
    async def test_query_scene(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_execute_code", {
                "code": "print('bodies:', len(O.bodies))",
                "timeout": 5,
            })
            data = _get_data(result)
            assert data["ok"] is True
            assert "bodies:" in data["data"]["output"]

    @pytest.mark.asyncio
    async def test_syntax_error(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_execute_code", {
                "code": "def bad syntax here!!!",
                "timeout": 5,
            })
            data = _get_data(result)
            # Should return error info, not crash
            assert "ok" in data

    @pytest.mark.asyncio
    async def test_multiline_code(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_execute_code", {
                "code": "x = 42\ny = 58\nprint('sum =', x + y)",
                "timeout": 5,
            })
            data = _get_data(result)
            assert data["ok"] is True
            assert "sum = 100" in data["data"]["output"]


class TestExecuteTask:
    @pytest.mark.asyncio
    async def test_submit_inline_task(self):
        async with Client(mcp) as client:
            # Create a simple inline simulation script
            result = await client.call_tool("yade_execute_code", {
                "code": (
                    "import tempfile, os\n"
                    "script = 'from yade import O\\nprint(\"task done\", len(O.bodies))\\n'\n"
                    "f = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False)\n"
                    "f.write(script)\n"
                    "f.close()\n"
                    "print(f.name)"
                ),
                "timeout": 5,
            })
            data = _get_data(result)
            script_path = data["data"]["output"].strip()

            # Submit task
            result = await client.call_tool("yade_execute_task", {
                "entry_script": script_path,
                "description": "integration test task",
            })
            data = _get_data(result)
            assert data["ok"] is True
            task_id = data["data"]["task_id"]
            assert task_id

            # Wait and check status
            await asyncio.sleep(2)
            result = await client.call_tool("yade_check_task_status", {
                "task_id": task_id,
                "wait_seconds": 1,
            })
            data = _get_data(result)
            assert data["ok"] is True
            assert data["data"]["task_status"] in ("completed", "running", "failed")


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_list_tasks", {"limit": 5})
            data = _get_data(result)
            assert data["ok"] is True
            assert "tasks" in data["data"]


class TestInterruptTask:
    @pytest.mark.asyncio
    async def test_interrupt_nonexistent(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_interrupt_task", {
                "task_id": "nonexistent-task-id",
            })
            data = _get_data(result)
            # Should return not_found error, not crash
            assert "ok" in data


class TestCheckTaskStatus:
    @pytest.mark.asyncio
    async def test_nonexistent_task(self):
        async with Client(mcp) as client:
            result = await client.call_tool("yade_check_task_status", {
                "task_id": "nonexistent-task-id",
                "wait_seconds": 1,
            })
            data = _get_data(result)
            assert data["ok"] is False
            assert data["error"]["code"] == "not_found"
