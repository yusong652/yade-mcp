"""Execute code message handler for YADE bridge.

Handles synchronous code snippet execution via main thread queue.
"""

import asyncio
import logging
import os
import sys
import time
from io import StringIO

from ..execution.errors import format_execution_error
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
                    "result": result
                    if isinstance(result, (str, int, float, bool, list, dict, type(None)))
                    else str(result)
                    if result is not None
                    else None,
                }
            except Exception as e:
                output_text = output_buffer.getvalue()
                # Suppress chained-exception noise. The handler tries
                # compile(..., "eval") first and falls back to exec on
                # SyntaxError; that fallback leaves a "During handling
                # of the above exception..." preamble in format_exc
                # that's pure plumbing, not user-relevant.
                e.__suppress_context__ = True

                def _overflow_writer(full_tb: str) -> str:
                    # Only called when the excerpt is truncated. Fresh
                    # timestamped file per error — execute_code is
                    # synchronous REPL, overwriting a single rolling file
                    # would race if another call lands between error and
                    # agent read.
                    log_dir = os.path.join(".yade-mcp", "logs")
                    os.makedirs(log_dir, exist_ok=True)
                    path = os.path.join(log_dir, f"exec_code_error_{int(time.time() * 1000)}.log")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(full_tb)
                    return os.path.abspath(path)

                return format_execution_error(
                    e,
                    output_text,
                    is_user_frame=lambda fn: fn == "<execute_code>",
                    display_path="<execute_code>",
                    message_prefix="Code execution failed",
                    overflow_writer=_overflow_writer,
                )
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
            details: dict = {}
            for key in ("exception_type", "traceback", "traceback_truncated", "log_file"):
                if key in result:
                    details[key] = result[key]
            return {
                "type": "execute_code_result",
                "request_id": request_id,
                "status": "error",
                "message": result.get("message", ""),
                "error": {
                    "code": "execute_code_error",
                    "message": result.get("message", ""),
                    "details": details or None,
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
