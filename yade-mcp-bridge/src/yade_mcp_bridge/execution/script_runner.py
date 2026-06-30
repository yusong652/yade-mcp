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
from concurrent.futures import Future

from ..paths import LOGS_DIR
from ..runtime.pyrunner import async_cycling_pending
from ..runtime.signals import (
    clear_current_task,
    clear_interrupt,
    is_task_interrupt_requested,
    register_exec_thread,
    set_current_task,
    unregister_exec_thread,
)
from ..utils import FileBuffer, TaskDataBuilder, TeeBuffer, error_body, ok_body, path_to_llm_format
from .errors import AsyncAbort, CycleInterrupt, format_execution_error

logger = logging.getLogger("MCP-Bridge")

_ASYNC_CYCLING_PICKUP_TIMEOUT_S = 0.5  # max wait for the cycle to start (pickup gap)


def _drain_async_cycling() -> None:
    """Block until any ``O.run(wait=False)`` cycling the task dispatched
    finishes, so it never reports success while the sim still cycles (an orphan
    session hides errors and evades interrupts). Defensive: agents rarely call
    ``O.run(wait=False)``.

    ``wait=False`` returns before ``O.running`` flips True, so poll for the
    cycle to start (bounded) then ``O.wait()`` for the run itself (unbounded).
    The bound caps only the pickup gap, not run length; ``wait=True`` drains
    inside YADE and never reaches here.
    """
    try:
        from yade import O as _O
    except ImportError:
        return
    if not async_cycling_pending():
        return
    deadline = time.monotonic() + _ASYNC_CYCLING_PICKUP_TIMEOUT_S
    while not _O.running and time.monotonic() < deadline:
        time.sleep(0.005)
    if _O.running:
        _O.wait()  # blocks; re-raises cycling errors as RuntimeError


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

            _drain_async_cycling()

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
            # Async-injected by handle_interrupt_task as a last-resort
            # abort for pure-Python deadloops that never hit a PyRunner
            # tick. The handler already ``unregister_exec_thread(task_id)``
            # before injecting, so any re-injection attempt is a no-op —
            # this cleanup path runs without risk of being re-interrupted.
            output_text = output_buffer.getvalue()
            logger.info(f"Script force-interrupted (async_exc): {script_path}")
            # Best-effort sim cleanup: if the interrupt fired while the
            # user code had started an O.run, make sure the sim thread is
            # paused and fully drained before returning. Without this,
            # ``O.running`` may stay True and the NEXT task's ``_O.wait()``
            # drain (see below) will block on a zombie cycling session,
            # preventing recovery.
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
            # Mirror execute_code: suppress the compile(eval)->compile(exec)
            # fallback chain that otherwise prepends a misleading
            # "SyntaxError / During handling of the above" preamble to
            # the raw traceback.
            e.__suppress_context__ = True

            # Detect a CycleInterrupt wrapped by YADE's PyRunner as RuntimeError
            error_str = str(e)
            if "CycleInterrupt" in error_str:
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
                fallback = os.path.join(LOGS_DIR, f"task_{task_id}_error.log")
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
            # Idempotent — handler may have already unregistered to
            # guard against double-injection.
            unregister_exec_thread(task_id)
            # Release the log file handle now that execution is terminal
            # and stdout is restored (no more writes land here). Later
            # check_task_status reads re-open the log by path, so closing
            # frees the fd without losing readability. Last in finally so
            # a close error can't skip the signal cleanup above.
            output_buffer.close()

    def run(self, script_path, description, task_id):
        """Spawn the script on its own daemon thread and return immediately."""
        if not task_id:
            # task_id keys the task's history, so it must be present.
            return error_body("missing_field", "task_id required", details={"field": "task_id"})

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
            future: Future = Future()

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
