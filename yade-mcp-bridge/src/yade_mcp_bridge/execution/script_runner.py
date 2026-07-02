# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""YADE Script Executor - Executes Python scripts in YADE environment.

Runs each script as a background task on its own thread, with stdout
capture and interrupt support.
"""

import logging
import os
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from functools import partial

from ..paths import LOGS_DIR
from ..runtime.background_run import wait_for_background_run
from ..runtime.signals import (
    clear_current_task,
    clear_interrupt,
    is_task_interrupt_requested,
    register_exec_thread,
    set_current_task,
    unregister_exec_thread,
)
from ..utils import FileBuffer, TaskDataBuilder, TeeBuffer, error_body, ok_body, path_to_llm_format
from .errors import format_execution_error, log_execute_task_overflow, script_frame_filter
from .termination import AsyncAbort, CycleInterrupt

logger = logging.getLogger("MCP-Bridge")


class ScriptRunner:
    """Run user scripts as background tasks, each on its own thread."""

    def __init__(self, task_manager):
        self.task_manager = task_manager

    def _execute(self, script_path, script_content, output_buffer, task_id):
        """Execute the script on the task's own thread.

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
        # Advertise this thread to handle_interrupt_task so it can
        # async-inject AsyncAbort for the pure-Python deadloop case.
        register_exec_thread(task_id, threading.get_ident())

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

            wait_for_background_run()

            if is_task_interrupt_requested(task_id):
                raise CycleInterrupt("Interrupted by MCP bridge")

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

        except CycleInterrupt as e:
            output_text = output_buffer.getvalue()
            logger.info(f"Script interrupted: {script_path} - {str(e)}")
            return {
                "status": "interrupted",
                "message": f"Script interrupted by user: {str(e)}",
                "result": None,
                "output": output_text,
            }

        except AsyncAbort:
            # Last-resort abort injected by handle_interrupt_task for a
            # pure-Python deadloop that never hit a PyRunner tick.
            output_text = output_buffer.getvalue()
            logger.info(f"Script force-interrupted (async_exc): {script_path}")
            # Best-effort: pause and drain the sim so a live O.run doesn't
            # leak O.running=True into the next task.
            try:
                from yade import O as _O

                if _O.running:
                    _O.pause()
                    _O.wait()
            except ImportError:
                pass
            except Exception as cleanup_exc:  # noqa: BLE001
                logger.warning("Sim cleanup after AsyncAbort failed: %s", cleanup_exc)
            return {
                "status": "interrupted",
                "message": "Task force-interrupted by user (async abort)",
                "result": None,
                "output": output_text,
            }

        except (SystemExit, KeyboardInterrupt):
            raise

        except BaseException as e:
            output_text = output_buffer.getvalue()
            # Suppress the compile(eval)->compile(exec) fallback's chained
            # SyntaxError preamble from the traceback.
            e.__suppress_context__ = True

            task_log_path = output_buffer.get_path() if hasattr(output_buffer, "get_path") else None

            payload = format_execution_error(
                e,
                output_text,
                keep_frame=script_frame_filter(script_path),
                display_path=path_to_llm_format(script_path),
                overflow_writer=partial(log_execute_task_overflow, task_log_path=task_log_path, task_id=task_id),
            )
            logger.error(f"Script execution failed:\n{payload['message']}")
            payload["result"] = None
            return payload

        finally:
            sys.stdout = old_stdout
            clear_current_task()
            clear_interrupt(task_id)
            # Idempotent — handler may have already unregistered to
            # guard against double-injection.
            unregister_exec_thread(task_id)
            # Release the log fd; check_task_status re-opens by path. Last so a
            # close error can't skip the signal cleanup above.
            output_buffer.close()

    def run(self, script_path, description):
        """Start the script on its own daemon thread and return immediately.

        Assigns the task_id; the caller gets it back in the response data.
        """
        task_id = uuid.uuid4().hex[:8]

        script_name = os.path.basename(script_path)

        try:
            with open(script_path, encoding="utf-8") as f:
                # Read script file content here in the handler thread,
                # not the daemon thread, to handle file read errors
                script_content = f.read()
        except FileNotFoundError:
            return error_body("script_not_found", f"Script file not found: {script_path}")
        except OSError as e:
            return error_body("script_read_error", f"Failed to read script file: {str(e)}")

        try:
            log_path = os.path.join(LOGS_DIR, f"task_{task_id}.log")
            output_buffer = FileBuffer(log_path)

            # Own thread, not the shared executor pump (unlike execute_code):
            # a long O.run(wait=True) here would otherwise block the pump and
            # starve execute_code for the task's whole lifetime.
            future = Future()

            def _script_runner():
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    result = self._execute(script_path, script_content, output_buffer, task_id)
                    future.set_result(result)
                except BaseException as exc:  # noqa: BLE001 — surface every failure to the future
                    future.set_exception(exc)

            script_thread = threading.Thread(
                target=_script_runner,
                name=f"script-{task_id}",
                daemon=True,
            )
            script_thread.start()

            submit_time = time.time()
            # pending -> running is promoted by _execute / the task manager.
            self.task_manager.create_script_task(future, script_name, script_path, output_buffer, description, task_id)

            data = (
                TaskDataBuilder(task_id, "script", script_path, description)
                .with_status("pending")
                .with_timing(submit_time)
                .build()
            )
            return ok_body(data=data)

        except Exception as e:
            logger.error(f"Script execution failed: {e}")
            return error_body("submit_failed", f"Script execution failed: {str(e)}")

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
