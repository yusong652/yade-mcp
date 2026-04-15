"""YADE Script Executor - Executes Python scripts in YADE environment.

Runs scripts in YADE's main thread via queue, with stdout capture
and interrupt support.
"""

import logging
import os
import sys
import time

from ..signals import clear_current_task, clear_interrupt, is_interrupt_requested, set_current_task
from ..utils import FileBuffer, TaskDataBuilder, TeeBuffer, build_response, path_to_llm_format
from .errors import format_execution_error

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
        terminal = sys.__stdout__ if sys.__stdout__ is not None else old_stdout
        sys.stdout = TeeBuffer(terminal, output_buffer)

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

            # Drain any fire-and-forget cycling (O.run wait=False) before
            # reporting task success. Aligns task lifetime with cycling
            # lifetime, matching PFC's synchronous SDK semantics. Without
            # this, a wait=False O.run creates an orphan cycling session
            # that outlives the task, hiding errors and evading interrupts.
            try:
                from yade import O as _O

                # Give sim thread a beat to pick up just-dispatched cycling.
                time.sleep(0.05)
                if _O.running:
                    _O.wait()  # blocks; re-raises cycling errors as RuntimeError

                if is_interrupt_requested(task_id):
                    raise InterruptedError("Interrupted by MCP bridge")
            except ImportError:
                pass

            output_text = output_buffer.getvalue()
            serialized_result = self._serialize_result(result)

            script_name = os.path.basename(script_path)
            if serialized_result is not None:
                message = f"Script executed: {script_name}\nResult: {serialized_result}"
            else:
                message = f"Script executed: {script_name}"

            return {
                "status": "success",
                "message": message,
                "result": serialized_result,
                "output": output_text,
            }

        except InterruptedError as e:
            output_text = output_buffer.getvalue()
            logger.info(f"Script interrupted: {script_path} - {str(e)}")
            return {
                "status": "interrupted",
                "message": f"Script interrupted by user: {str(e)}",
                "result": None,
                "output": output_text,
            }

        except (SystemExit, KeyboardInterrupt):
            raise

        except BaseException as e:
            output_text = output_buffer.getvalue()
            # Mirror execute_code: suppress the compile(eval)->compile(exec)
            # fallback chain that otherwise prepends a misleading
            # "SyntaxError / During handling of the above" preamble to
            # the raw traceback.
            e.__suppress_context__ = True

            # Detect InterruptedError wrapped by YADE's PyRunner as RuntimeError
            error_str = str(e)
            if "InterruptedError" in error_str and "Interrupted by MCP bridge" in error_str:
                logger.info(f"Script interrupted (via PyRunner): {script_path}")
                return {
                    "status": "interrupted",
                    "message": "Script interrupted by user",
                    "result": None,
                    "output": output_text,
                }

            normalized_script_path = os.path.normpath(script_path)

            def _is_user_frame(filename: str) -> bool:
                return os.path.normpath(filename) == normalized_script_path or filename == "<string>"

            task_log_path = output_buffer.get_path() if hasattr(output_buffer, "get_path") else None

            def _overflow_writer(full_tb: str) -> str:
                # Append the full traceback to the task's own log so the
                # LLM can retrieve it via check_task_status pagination or
                # direct file read. Returns the abs path so the response
                # can point the agent at it.
                if task_log_path and os.path.isfile(task_log_path):
                    with open(task_log_path, "a", encoding="utf-8") as f:
                        f.write("\n--- traceback ---\n")
                        f.write(full_tb)
                    return os.path.abspath(task_log_path)
                # Fallback: dedicated task error log next to the task log.
                fallback = os.path.join(".yade-mcp", "logs", f"task_{task_id}_error.log")
                os.makedirs(os.path.dirname(fallback), exist_ok=True)
                with open(fallback, "w", encoding="utf-8") as f:
                    f.write(full_tb)
                return os.path.abspath(fallback)

            payload = format_execution_error(
                e,
                output_text,
                is_user_frame=_is_user_frame,
                display_path=path_to_llm_format(script_path),
                message_prefix="Script execution failed",
                overflow_writer=_overflow_writer,
            )
            logger.error(f"Script execution failed:\n{payload['message']}")
            payload["result"] = None
            return payload

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
            with open(script_path, encoding="utf-8") as f:
                script_content = f.read()
        except FileNotFoundError:
            return {"status": "error", "message": f"Script file not found: {script_path}", "data": None}
        except OSError as e:
            return {"status": "error", "message": f"Failed to read script file: {str(e)}", "data": None}

        try:
            log_dir = os.path.join(".yade-mcp", "logs")
            log_path = os.path.join(log_dir, f"task_{task_id}.log")
            output_buffer = FileBuffer(log_path)

            future = self.main_executor.submit(self._execute, script_path, script_content, output_buffer, task_id)

            submit_time = time.time()
            self.task_manager.create_script_task(future, script_name, script_path, output_buffer, description, task_id)

            task = self.task_manager.tasks.get(task_id)
            if task and task.status == "pending":
                try:
                    if future.running():
                        task.status = "running"
                        if task.on_status_change:
                            task.on_status_change(task)
                except RuntimeError:
                    pass

            data = (
                TaskDataBuilder(task_id, "script", script_name, script_path, description)
                .with_timing(submit_time)
                .build()
            )
            return build_response("pending", f"Script submitted: {script_name}", data)

        except Exception as e:
            logger.error(f"Script execution failed: {e}")
            error_message = f"Script execution failed: {str(e)}"
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
