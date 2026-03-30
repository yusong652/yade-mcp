"""End-to-end test: MCP Client → MCP Server → Bridge → YADE.

Prerequisites:
    - YADE bridge running (yade_mcp_bridge.start() in YADE console)
    - Run with: uv run python tests/test_e2e.py
"""

import asyncio
import json

from fastmcp import Client
from yade_mcp.server import mcp


def pp(label, result):
    """Pretty-print a tool result."""
    print("\n" + "=" * 60)
    print(label)
    print("-" * 60)
    print(json.dumps(result.data, indent=2))


async def main():
    async with Client(mcp) as client:
        # 1. List available tools
        tools = await client.list_tools()
        print("Available tools:")
        for t in tools:
            print("  - {}".format(t.name))

        # 2. execute_code: query scene
        result = await client.call_tool("yade_execute_code", {
            "code": "print('Bodies:', len(O.bodies))\nprint('Iter:', O.iter)",
            "timeout": 5,
        })
        pp("execute_code: query scene", result)

        # 3. execute_code: create bodies
        result = await client.call_tool("yade_execute_code", {
            "code": (
                "from yade import utils\n"
                "O.reset()\n"
                "O.materials.append(FrictMat(young=1e6, poisson=0.3, density=2600, frictionAngle=0.5))\n"
                "for i in range(10):\n"
                "    O.bodies.append(utils.sphere((i*0.05, 0, 0), radius=0.01))\n"
                "print('Created', len(O.bodies), 'bodies')"
            ),
            "timeout": 10,
        })
        pp("execute_code: create bodies", result)

        # 4. execute_task: submit simulation script
        result = await client.call_tool("yade_execute_task", {
            "entry_script": "/home/yusong/yade-mcp/tests/test_simple_sim.py",
            "description": "E2E test: simple sphere simulation",
        })
        pp("execute_task: submit", result)

        # Extract task_id
        task_id = result.data.get("data", {}).get("task_id", "")
        print("task_id:", task_id)

        # 5. check_task_status (wait for completion)
        result = await client.call_tool("yade_check_task_status", {
            "task_id": task_id,
            "wait_seconds": 2,
        })
        pp("check_task_status", result)

        # 6. list_tasks
        result = await client.call_tool("yade_list_tasks", {
            "limit": 10,
        })
        pp("list_tasks", result)

        print("\n" + "=" * 60)
        print("E2E test complete!")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
