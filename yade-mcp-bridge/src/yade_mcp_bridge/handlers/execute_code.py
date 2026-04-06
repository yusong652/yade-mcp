"""Execute code message handler for YADE bridge.

Handles synchronous code snippet execution via main thread queue.
"""

import asyncio
import logging
import sys
from io import StringIO

from ..utils import TeeBuffer
from .helpers import require_field

logger = logging.getLogger("YADE-Bridge")


async def handle_execute_code(ctx, data):
    """Handle execute_code message - run code synchronously in YADE."""

    request_id = data.get("request_id", "unknown")

    code, err = require_field(data, "code", request_id, "execute_code_result")
    if err:
        return err

    timeout_ms = data.get("timeout_ms", 10000)
    timeout_s = timeout_ms / 1000.0

    try:
        def _execute_code(code_str):
            """Execute code in main thread, capturing stdout."""
            old_stdout = sys.stdout
            output_buffer = StringIO()
            terminal = sys.__stdout__ if sys.__stdout__ is not None else old_stdout
            sys.stdout = TeeBuffer(terminal, output_buffer)

            try:
                import __main__
                exec_globals = __main__.__dict__
                exec_globals.pop("result", None)

                try:
                    code_obj = compile(code_str, "<execute_code>", "eval")
                    result = eval(code_obj, exec_globals, exec_globals)
                except SyntaxError:
                    code_obj = compile(code_str, "<execute_code>", "exec")
                    exec(code_obj, exec_globals, exec_globals)
                    result = exec_globals.get("result", None)

                output_text = output_buffer.getvalue()
                return {
                    "status": "success",
                    "output": output_text,
                    "result": result if isinstance(result, (str, int, float, bool, list, dict, type(None))) else str(result) if result is not None else None,
                }
            except Exception as e:
                output_text = output_buffer.getvalue()
                return {
                    "status": "error",
                    "output": output_text,
                    "message": f"{type(e).__name__}: {str(e)}",
                }
            finally:
                sys.stdout = old_stdout

        # Submit to main thread and wait
        future = ctx.main_executor.submit(_execute_code, code)

        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, future.result, timeout_s),
            timeout=timeout_s + 2.0,
        )

        status = result.get("status", "unknown")
        if status == "error":
            return {
                "type": "execute_code_result",
                "request_id": request_id,
                "status": "error",
                "message": result.get("message", ""),
                "error": {
                    "code": "execute_code_error",
                    "message": result.get("message", ""),
                },
                "data": {
                    "output": result.get("output", ""),
                },
            }

        return {
            "type": "execute_code_result",
            "request_id": request_id,
            "status": "success",
            "message": "Code executed successfully",
            "data": {
                "output": result.get("output", ""),
                "result": result.get("result"),
            },
        }

    except (asyncio.TimeoutError, TimeoutError):
        return {
            "type": "execute_code_result",
            "request_id": request_id,
            "status": "timeout",
            "message": f"Execution timed out after {timeout_ms}ms",
            "error": {
                "code": "timeout",
                "message": f"Execution timed out after {timeout_ms}ms",
            },
            "data": None,
        }

    except Exception as e:
        logger.error(f"Code execution failed: {e}")
        return {
            "type": "execute_code_result",
            "request_id": request_id,
            "status": "error",
            "message": str(e),
            "error": {
                "code": "execute_code_failed",
                "message": str(e),
            },
            "data": None,
        }
