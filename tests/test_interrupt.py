"""Test interrupt: submit long task, then interrupt it.

Prerequisites:
    - YADE bridge running (yade_mcp_bridge.start() in YADE console)
    - Run with: uv run python tests/test_interrupt.py
"""

import asyncio
import json

from fastmcp import Client
from yade_mcp.server import mcp


def pp(label, result):
    print("\n" + "=" * 60)
    print(label)
    print("-" * 60)
    print(json.dumps(result.data, indent=2))


async def main():
    async with Client(mcp) as client:
        # 1. Submit long-running task
        result = await client.call_tool("yade_execute_task", {
            "entry_script": "/home/yusong/yade-mcp/tests/test_long_sim.py",
            "description": "Long simulation for interrupt test",
        })
        pp("submit task", result)
        task_id = result.data["data"]["task_id"]
        print("task_id:", task_id)

        # 2. Wait a bit, check status (should be running)
        await asyncio.sleep(2)
        result = await client.call_tool("yade_check_task_status", {
            "task_id": task_id,
            "wait_seconds": 1,
        })
        pp("status after 2s (should be running)", result)

        # 3. Execute code while task is running (via PyRunner queue processing)
        result = await client.call_tool("yade_execute_code", {
            "code": "print('Query during task: iter =', O.iter)",
            "timeout": 5,
        })
        pp("execute_code during running task", result)

        # 4. Interrupt the task
        result = await client.call_tool("yade_interrupt_task", {
            "task_id": task_id,
        })
        pp("interrupt task", result)

        # 5. Wait and check final status
        await asyncio.sleep(2)
        result = await client.call_tool("yade_check_task_status", {
            "task_id": task_id,
            "wait_seconds": 1,
        })
        pp("status after interrupt", result)

        # 6. Verify YADE is still functional after interrupt
        result = await client.call_tool("yade_execute_code", {
            "code": "print('YADE still alive, bodies:', len(O.bodies), 'iter:', O.iter)",
            "timeout": 5,
        })
        pp("execute_code after interrupt (YADE should still work)", result)

        print("\n" + "=" * 60)
        print("Interrupt test complete!")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
