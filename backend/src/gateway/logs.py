"""Log output: readable text (default) or one JSON object per line for Google Cloud Logging.

Cloud Run sends everything the container writes to Cloud Logging. A plain text line arrives as one opaque string; a
JSON line is parsed, and some fields have a special meaning:
  severity                      the entry's level (DEBUG, INFO, WARNING, ERROR, CRITICAL), so "errors only" works
  message                       the text shown in the log list (stack traces here are picked up by Error Reporting)
  time                          when it happened
  logging.googleapis.com/trace  links the line to its request: the console shows it nested under that request
Every other field is kept as data (jsonPayload.<field>) for queries and log-based metrics.

Extra fields come from log.info(..., extra={"json_fields": {...}}). The text format ignores them.
"""

import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Mapping

from starlette.datastructures import Headers

LOG_FORMATS = ("text", "json")
TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
TRACE_FIELD = "logging.googleapis.com/trace"

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)  # of the request being handled, if any
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


def trace_id_from_headers(headers: Mapping[str, str]) -> str | None:
    """From X-Cloud-Trace-Context (TRACE_ID/SPAN_ID;o=1) or W3C traceparent (00-TRACE_ID-SPAN_ID-01).
    The header comes from the client, so only a well-formed id is kept: no junk or huge values in our logs."""
    cloud = (headers.get("x-cloud-trace-context") or "").split("/")[0]
    w3c = (headers.get("traceparent") or "").split("-")
    for candidate in (cloud, w3c[1] if len(w3c) == 4 else ""):
        candidate = candidate.strip().lower()
        if _TRACE_ID.match(candidate) and candidate != "0" * 32:
            return candidate
    return None


class TraceMiddleware:
    """Remembers the request's trace id while the request is handled, for JsonFormatter.
    Plain ASGI on purpose: Starlette's BaseHTTPMiddleware buffers responses and can break MCP's streamed replies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _trace_id.set(trace_id_from_headers(Headers(scope=scope)))
        try:
            await self.app(scope, receive, send)
        finally:
            _trace_id.reset(token)


class JsonFormatter(logging.Formatter):
    def __init__(self, project: str | None = None):
        super().__init__()
        self.project = project  # the trace link needs it: projects/<project>/traces/<id>

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        entry = {"severity": record.levelname, "message": message, "logger": record.name,
                 "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat()}
        trace = _trace_id.get()
        if trace and self.project:
            entry[TRACE_FIELD] = f"projects/{self.project}/traces/{trace}"
        for key, value in (getattr(record, "json_fields", None) or {}).items():
            entry.setdefault(key, value)  # an extra field never replaces one of the fields above
        return json.dumps({k: v for k, v in entry.items() if v is not None}, ensure_ascii=False, default=str)


def setup_logging(fmt: str = "text", project: str | None = None) -> None:
    """All loggers write to stderr, as before; only the line format changes."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(project) if fmt == "json" else logging.Formatter(TEXT_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
