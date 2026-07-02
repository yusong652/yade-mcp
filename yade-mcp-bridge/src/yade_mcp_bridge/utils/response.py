# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Task Response Builder - Unified response data construction."""


class TaskDataBuilder:
    """Builder for task response data dictionaries."""

    def __init__(self, taskId, taskType, scriptPath, description):
        self._data = {
            "task_id": taskId,
            "task_type": taskType,
            "script_path": scriptPath,
            "description": description,
        }

    def withStatus(self, status):
        # status: pending / running / completed / failed / interrupted
        self._data["status"] = status
        return self

    def withTiming(self, startTime, endTime=None, elapsedTime=None):
        self._data["start_time"] = startTime
        if endTime is not None:
            self._data["end_time"] = endTime
        if elapsedTime is not None:
            self._data["elapsed_time"] = elapsedTime
        return self

    def withOutput(self, output):
        if output is not None:
            self._data["output"] = output
        return self

    def withPagination(self, pagination):
        if pagination is not None:
            self._data["pagination"] = pagination
        return self

    def withResult(self, result):
        if result is not None:
            self._data["result"] = result
        return self

    def withError(self, error):
        if error is not None:
            self._data["error"] = error
        return self

    def build(self):
        return self._data.copy()


def okBody(data=None):
    """Build the business half of a success envelope: ``{ok: True, data?}``.

    ``data`` is the handler's business payload, omitted when there is none.
    """
    body = {"ok": True}
    if data is not None:
        body["data"] = data
    return body


def errorBody(code, message, *, details=None, data=None):
    """Build the business half of an error envelope.

    ``{ok: False, error: {code, message, details?}, data?}``
    """
    # The error *kind* is the machine-readable ``code`` (not a parallel
    # ``status`` string); free-form diagnostics go in ``details``.
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    body = {"ok": False, "error": error}
    if data is not None:
        body["data"] = data
    return body


def okResponse(responseType, requestId, data=None):
    """Full success wire message: ``{type, request_id, ok: True, data?}``.

    Transport header (``type`` + ``request_id``) wrapping an ``okBody``.
    """
    return {"type": responseType, "request_id": requestId, **okBody(data=data)}


def errorResponse(responseType, requestId, code, message, *, details=None, data=None):
    """Full failure wire message: transport header wrapping an ``errorBody``."""
    return {
        "type": responseType,
        "request_id": requestId,
        **errorBody(code, message, details=details, data=data),
    }
