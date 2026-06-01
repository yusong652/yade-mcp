"""Task Response Builder - Unified response data construction."""


class TaskDataBuilder:
    """Builder for task response data dictionaries."""

    def __init__(self, task_id, task_type, entry_script, description):
        # ``entry_script`` is the single source of truth for the script path;
        # the basename (formerly a ``script_name`` wire field) is bridge-derived
        # and redundant on the wire, so it is no longer emitted here.
        self._data = {
            "task_id": task_id,
            "task_type": task_type,
            "entry_script": entry_script,
            "description": description,
        }

    def with_status(self, status):
        # Task lifecycle state (pending / running / completed / failed /
        # interrupted) lives *in* the task data, not at the envelope level:
        # it describes the task (the subject of the request), so it rides in
        # ``data`` alongside ``task_id`` rather than parallel to the
        # request-level ``ok``. Mirrors the MCP server, which already nests it
        # as ``data.task_status``.
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

    Mirrors the MCP server's ``ToolEnvelope`` (contracts.py). Use this when a
    handler builds its business payload separately from the transport header
    (``type``/``request_id``) — e.g. ``TaskManager`` returns a business dict
    that the message handler spreads into ``{type, request_id, **body}``.
    Coherent by construction: a success body never carries an ``error``.
    """
    body = {"ok": True}
    if data is not None:
        body["data"] = data
    return body


def error_body(code, message, *, details=None, data=None):
    """Build the business half of a failure envelope.

    ``{ok: False, error: {code, message, details?}, data?}``

    The nested ``error`` is already in the MCP ``ToolError`` shape, so the
    server lifts it through verbatim. Coherent by construction: a failure
    body always carries an ``error``. The error *kind* lives in
    machine-readable ``code`` (not a parallel ``status`` string); free-form
    diagnostics go in ``details``.
    """
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    body = {"ok": False, "error": error}
    if data is not None:
        body["data"] = data
    return body


def ok_response(response_type, request_id, data=None):
    """Full success wire message: ``{type, request_id, ok: True, data?}``.

    A *response* = transport header (``type`` + ``request_id``) wrapping an
    ``ok_body`` — same relationship as an HTTP response wrapping its body. Use
    this when a handler builds the whole message in one place (e.g.
    ``execute_code``); use bare ``ok_body`` when the handler returns just the
    body for a caller to frame (e.g. ``TaskManager`` → message handler).
    """
    return {"type": response_type, "request_id": request_id, **ok_body(data=data)}


def error_response(response_type, request_id, code, message, *, details=None, data=None):
    """Full failure wire message: transport header wrapping an ``error_body``."""
    return {
        "type": response_type,
        "request_id": request_id,
        **error_body(code, message, details=details, data=data),
    }
