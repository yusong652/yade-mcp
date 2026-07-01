# encoding: utf-8
# 2026 © Yusong Han <yusong.han.652@gmail.com>
"""Task Response Builder - Unified response data construction."""


class TaskDataBuilder:
    """Builder for task response data dictionaries."""

    def __init__(self, task_id, task_type, script_path, description):
        self._data = {
            "task_id": task_id,
            "task_type": task_type,
            "script_path": script_path,
            "description": description,
        }

    def with_status(self, status):
        # status: pending / running / completed / failed / interrupted
        self._data["status"] = status
        return self

    def with_timing(self, start_time, end_time=None, elapsed_time=None):
        self._data["start_time"] = start_time
        if end_time is not None:
            self._data["end_time"] = end_time
        if elapsed_time is not None:
            self._data["elapsed_time"] = elapsed_time
        return self

    def with_output(self, output):
        if output is not None:
            self._data["output"] = output
        return self

    def with_pagination(self, pagination):
        if pagination is not None:
            self._data["pagination"] = pagination
        return self

    def with_result(self, result):
        if result is not None:
            self._data["result"] = result
        return self

    def with_error(self, error):
        if error is not None:
            self._data["error"] = error
        return self

    def build(self):
        return self._data.copy()


def ok_body(data=None):
    """Build the business half of a success envelope: ``{ok: True, data?}``.

    ``data`` is the handler's business payload, omitted when there is none.
    """
    body = {"ok": True}
    if data is not None:
        body["data"] = data
    return body


def error_body(code, message, *, details=None, data=None):
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


def ok_response(response_type, request_id, data=None):
    """Full success wire message: ``{type, request_id, ok: True, data?}``.

    Transport header (``type`` + ``request_id``) wrapping an ``ok_body``.
    """
    return {"type": response_type, "request_id": request_id, **ok_body(data=data)}


def error_response(response_type, request_id, code, message, *, details=None, data=None):
    """Full failure wire message: transport header wrapping an ``error_body``."""
    return {
        "type": response_type,
        "request_id": request_id,
        **error_body(code, message, details=details, data=data),
    }
