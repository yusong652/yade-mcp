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
from ..runtime.backgroundRun import waitForBackgroundRun
from ..runtime.signals import (
    clearCurrentTask,
    clearInterrupt,
    isTaskInterruptRequested,
    registerExecThread,
    setCurrentTask,
    unregisterExecThread,
)
from ..utils import FileBuffer, TaskDataBuilder, TeeBuffer, errorBody, okBody, pathToLlmFormat
from .errors import formatExecutionError, logExecuteTaskOverflow, scriptFrameFilter
from .termination import AsyncAbort, CycleInterrupt

logger = logging.getLogger("MCP-Bridge")


class ScriptRunner:
    """Run user scripts as background tasks, each on its own thread."""

    def __init__(self, taskManager):
        self.taskManager = taskManager

    def _execute(self, scriptPath, scriptContent, outputBuffer, taskId):
        """Execute the script on the task's own thread.

        Captures stdout during execution for progress tracking.
        Supports interruption via interrupt flag.
        """
        task = self.taskManager.tasks.get(taskId)
        if task:
            task.status = "running"

        oldStdout = sys.stdout
        terminal = sys.__stdout__ if sys.__stdout__ is not None else oldStdout
        sys.stdout = TeeBuffer(terminal, outputBuffer)

        setCurrentTask(taskId)
        # Advertise this thread to handleInterruptTask so it can
        # async-inject AsyncAbort for the pure-Python infinite-loop case.
        registerExecThread(taskId, threading.get_ident())

        try:
            # Use __main__ namespace so YADE modules are available
            # and variables persist between executions
            import __main__

            execGlobals = __main__.__dict__

            execGlobals["__file__"] = scriptPath
            execGlobals.pop("result", None)

            # Try eval first (single expression), fall back to exec
            try:
                codeObj = compile(scriptContent, scriptPath, "eval")
                result = eval(codeObj, execGlobals, execGlobals)
            except SyntaxError:
                codeObj = compile(scriptContent, scriptPath, "exec")
                exec(codeObj, execGlobals, execGlobals)
                result = execGlobals.get("result", None)

            waitForBackgroundRun()

            if isTaskInterruptRequested(taskId):
                raise CycleInterrupt("Interrupted by MCP bridge")

            outputText = outputBuffer.getvalue()
            serializedResult = self._serializeResult(result)

            scriptName = os.path.basename(scriptPath)
            if serializedResult is not None:
                message = f"Script executed: {scriptName}\nResult: {serializedResult}"
            else:
                message = f"Script executed: {scriptName}"

            return {
                "status": "success",
                "message": message,
                "result": serializedResult,
                "output": outputText,
            }

        except CycleInterrupt as e:
            outputText = outputBuffer.getvalue()
            logger.info(f"Script interrupted: {scriptPath} - {str(e)}")
            return {
                "status": "interrupted",
                "message": f"Script interrupted by user: {str(e)}",
                "result": None,
                "output": outputText,
            }

        except AsyncAbort:
            # Last-resort abort injected by handleInterruptTask for a
            # pure-Python infinite loop that never hit a PyRunner tick.
            outputText = outputBuffer.getvalue()
            logger.info(f"Script force-interrupted (async_exc): {scriptPath}")
            # Best-effort: pause the sim and wait for it to stop, so a live
            # O.run doesn't leak O.running=True into the next task.
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
                "output": outputText,
            }

        except (SystemExit, KeyboardInterrupt):
            raise

        except BaseException as e:
            outputText = outputBuffer.getvalue()
            # Suppress the compile(eval)->compile(exec) fallback's chained
            # SyntaxError preamble from the traceback.
            e.__suppress_context__ = True

            taskLogPath = outputBuffer.getPath() if hasattr(outputBuffer, "getPath") else None

            payload = formatExecutionError(
                e,
                outputText,
                keepFrame=scriptFrameFilter(scriptPath),
                displayPath=pathToLlmFormat(scriptPath),
                overflowWriter=partial(logExecuteTaskOverflow, taskLogPath=taskLogPath, taskId=taskId),
            )
            logger.error(f"Script execution failed:\n{payload['message']}")
            payload["result"] = None
            return payload

        finally:
            sys.stdout = oldStdout
            clearCurrentTask()
            clearInterrupt(taskId)
            # Idempotent — handler may have already unregistered to
            # guard against double-injection.
            unregisterExecThread(taskId)
            # Release the log fd; check_task_status re-opens by path. Last so a
            # close error can't skip the signal cleanup above.
            outputBuffer.close()

    def run(self, scriptPath, description):
        """Start the script on its own daemon thread and return immediately.

        Assigns the task_id; the caller gets it back in the response data.
        """
        taskId = uuid.uuid4().hex[:8]

        scriptName = os.path.basename(scriptPath)

        try:
            with open(scriptPath, encoding="utf-8") as f:
                # Read script file content here in the handler thread,
                # not the daemon thread, to handle file read errors
                scriptContent = f.read()
        except FileNotFoundError:
            return errorBody("script_not_found", f"Script file not found: {scriptPath}")
        except OSError as e:
            return errorBody("script_read_error", f"Failed to read script file: {str(e)}")

        try:
            logPath = os.path.join(LOGS_DIR, f"task_{taskId}.log")
            outputBuffer = FileBuffer(logPath)

            # Own thread, not the shared executor pump (unlike execute_code):
            # a long O.run(wait=True) here would otherwise block the pump and
            # starve execute_code for the task's whole lifetime.
            future = Future()

            def _scriptRunner():
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    result = self._execute(scriptPath, scriptContent, outputBuffer, taskId)
                    future.set_result(result)
                except BaseException as exc:  # noqa: BLE001 — surface every failure to the future
                    future.set_exception(exc)

            scriptThread = threading.Thread(
                target=_scriptRunner,
                name=f"script-{taskId}",
                daemon=True,
            )
            scriptThread.start()

            submitTime = time.time()
            # pending -> running is promoted by _execute / the task manager.
            self.taskManager.createScriptTask(future, scriptName, scriptPath, outputBuffer, description, taskId)

            data = (
                TaskDataBuilder(taskId, "script", scriptPath, description)
                .withStatus("pending")
                .withTiming(submitTime)
                .build()
            )
            return okBody(data=data)

        except Exception as e:
            logger.error(f"Script execution failed: {e}")
            return errorBody("submit_failed", f"Script execution failed: {str(e)}")

    def _serializeResult(self, result):
        if result is None:
            return None
        elif isinstance(result, (str, int, float, bool)):
            return result
        elif isinstance(result, (list, tuple)):
            return [self._serializeResult(item) for item in result]
        elif isinstance(result, dict):
            return {k: self._serializeResult(v) for k, v in result.items()}
        else:
            return str(result)
