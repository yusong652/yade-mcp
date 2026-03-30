"""YADE Script Executor - Executes Python scripts in YADE environment.

Runs scripts in YADE's main thread via queue, with stdout capture
and interrupt support.
"""

import logging
import os
import sys
import time
import traceback

from .main_thread import MainThreadExecutor
from ..utils import path_to_llm_format, FileBuffer, TaskDataBuilder, build_response
from ..signals import set_current_task, clear_current_task, clear_interrupt

logger = logging.getLogger("YADE-Bridge")


class ScriptRunner:
    """Run Python scripts via YADE main thread queue."""

    def __init__(self, main_executor, task_manager):
        self.main_executor = main_executor
        self.task_manager = task_manager

    def _execute(self, script_path, script_content, output_buffer, task_id):
        """Execute script in main thread (called via queue).

        Captures stdout during execution for progress tracking.
        Supports interruption via interrupt flag.
        """
        task = self.task_manager.tasks.get(task_id)
        if task:
            task.status = "running"

        old_stdout = sys.stdout
        sys.stdout = output_buffer

        set_current_task(task_id)

        try:
            # Use __main__ namespace so YADE modules are available
            # and variables persist between executions
            import __main__
            exec_globals = __main__.__dict__

            exec_globals["__file__"] = script_path
            exec_globals.pop("result", None)

            # Try eval first (single expression), fall back to exec
            try:
                code_obj = compile(script_content, script_path, "eval")
                result = eval(code_obj, exec_globals, exec_globals)
            except SyntaxError:
                code_obj = compile(script_content, script_path, "exec")
                exec(code_obj, exec_globals, exec_globals)
                result = exec_globals.get("result", None)

            output_text = output_buffer.getvalue()
            serialized_result = self._serialize_result(result)

            script_name = os.path.basename(script_path)
            if serialized_result is not None:
                message = "Script executed: {}\nResult: {}".format(script_name, serialized_result)
            else:
                message = "Script executed: {}".format(script_name)

            return {
                "status": "success",
                "message": message,
                "result": serialized_result,
                "output": output_text,
            }

        except InterruptedError as e:
            output_text = output_buffer.getvalue()
            logger.info("Script interrupted: {} - {}".format(script_path, str(e)))
            return {
                "status": "interrupted",
                "message": "Script interrupted by user: {}".format(str(e)),
                "result": None,
                "output": output_text,
            }

        except BaseException as e:
            output_text = output_buffer.getvalue()

            # Detect InterruptedError wrapped by YADE's PyRunner as RuntimeError
            error_str = str(e)
            if "InterruptedError" in error_str and "Interrupted by MCP bridge" in error_str:
                logger.info("Script interrupted (via PyRunner): {}".format(script_path))
                return {
                    "status": "interrupted",
                    "message": "Script interrupted by user",
                    "result": None,
                    "output": output_text,
                }

            full_traceback = traceback.format_exc()
            logger.error("Script execution failed with traceback:\n{}".format(full_traceback))

            # Extract user script frames only
            tb = sys.exc_info()[2]
            user_frames = []
            normalized_script_path = os.path.normpath(script_path)

            while tb is not None:
                frame = tb.tb_frame
                filename = frame.f_code.co_filename
                normalized_filename = os.path.normpath(filename)
                if normalized_filename == normalized_script_path or filename == "<string>":
                    user_frames.append((filename, tb.tb_lineno, frame.f_code.co_name))
                tb = tb.tb_next

            display_path = path_to_llm_format(script_path)

            if user_frames:
                error_parts = ["Script execution failed:\n"]
                for filename, lineno, name in user_frames:
                    error_parts.append('  File "{}", line {}, in {}\n'.format(display_path, lineno, name))
                error_parts.append("{}: {}".format(type(e).__name__, str(e)))
                error_message = "".join(error_parts)
            else:
                error_message = "Script execution failed: {}: {}".format(type(e).__name__, str(e))

            return {
                "status": "error",
                "message": error_message,
                "result": None,
                "output": output_text,
            }

        finally:
            sys.stdout = old_stdout
            clear_current_task()
            clear_interrupt(task_id)

    async def run(self, script_path, description, task_id=None):
        """Submit script to main thread queue and return immediately."""
        if not task_id:
            return {"status": "error", "message": "task_id is required", "data": None}

        script_name = os.path.basename(script_path)

        try:
            with open(script_path, "r", encoding="utf-8") as f:
                script_content = f.read()
        except FileNotFoundError:
            return {"status": "error", "message": "Script file not found: {}".format(script_path), "data": None}
        except Exception as e:
            return {"status": "error", "message": "Failed to read script file: {}".format(str(e)), "data": None}

        try:
            log_dir = os.path.join(".yade-mcp", "logs")
            log_path = os.path.join(log_dir, "task_{}.log".format(task_id))
            output_buffer = FileBuffer(log_path)

            future = self.main_executor.submit(self._execute, script_path, script_content, output_buffer, task_id)

            submit_time = time.time()
            self.task_manager.create_script_task(
                future, script_name, script_path, output_buffer, description, task_id
            )

            task = self.task_manager.tasks.get(task_id)
            if task and task.status == "pending":
                try:
                    if future.running():
                        task.status = "running"
                        if task.on_status_change:
                            task.on_status_change(task)
                except Exception:
                    pass

            data = (
                TaskDataBuilder(task_id, "script", script_name, script_path, description)
                .with_timing(submit_time)
                .build()
            )
            return build_response("pending", "Script submitted: {}".format(script_name), data)

        except Exception as e:
            logger.error("Script execution failed: {}".format(e))
            error_message = "Script execution failed: {}".format(str(e))
            data = (
                TaskDataBuilder(task_id, "script", script_name, script_path, description)
                .with_error(error_message)
                .build()
            )
            return build_response("error", error_message, data)

    def _serialize_result(self, result):
        if result is None:
            return None
        elif isinstance(result, (str, int, float, bool)):
            return result
        elif isinstance(result, (list, tuple)):
            return [self._serialize_result(item) for item in result]
        elif isinstance(result, dict):
            return {k: self._serialize_result(v) for k, v in result.items()}
        else:
            return str(result)
