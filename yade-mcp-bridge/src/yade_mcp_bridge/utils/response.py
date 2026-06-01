"""Task Response Builder - Unified response data construction."""


class TaskDataBuilder:
    """Builder for task response data dictionaries."""

    def __init__(self, task_id, task_type, script_name, entry_script, description):
        self._data = {
            "task_id": task_id,
            "task_type": task_type,
            "script_name": script_name,
            "entry_script": entry_script,
            "description": description,
        }

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
        self._data["result"] = result
        return self

    def with_error(self, error):
        if error is not None:
            self._data["error"] = error
        return self

    def build(self):
        return self._data.copy()


def build_response(status, message, data):
    return {
        "status": status,
        "message": message,
        "data": data,
    }


def ok_result(response_type, request_id, data=None):
    """Build a success wire envelope: ``{type, request_id, ok: True, data?}``.

    Mirrors the MCP server's ``ToolEnvelope`` (contracts.py) on the wire so
    the server stops re-deriving success/failure from a free-form ``status``
    string. Coherent by construction: a success envelope never carries an
    ``error`` object.
    """
    resp = {"type": response_type, "request_id": request_id, "ok": True}
    if data is not None:
        resp["data"] = data
    return resp


def error_result(response_type, request_id, code, message, *, details=None, data=None):
    """Build a failure wire envelope.

    ``{type, request_id, ok: False, error: {code, message, details?}, data?}``

    The nested ``error`` is already in the MCP ``ToolError`` shape, so the
    server lifts it through verbatim. Coherent by construction: a failure
    envelope always carries an ``error``. The error *kind* lives in
    machine-readable ``code`` (not a parallel ``status`` string); free-form
    diagnostics go in ``details``.
    """
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    resp = {"type": response_type, "request_id": request_id, "ok": False, "error": error}
    if data is not None:
        resp["data"] = data
    return resp
