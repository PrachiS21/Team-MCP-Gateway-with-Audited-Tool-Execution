import io
import json
import logging
from contextlib import contextmanager

import httpx
import pytest

from gateway.audit.service import AuditLog
from gateway.config import Settings
from gateway.logs import TRACE_FIELD, JsonFormatter, _trace_id, trace_id_from_headers
from gateway.registry.crypto import generate_key
from gateway.server import create_gateway

TRACE = "105445aa7843bc8bf206b12000100000"
KEY = "wmcp_ab12cd34_" + "f" * 64  # well-formed, never issued


@contextmanager
def json_log_lines(project: str | None = None):
    """Collects what the gateway's loggers write, as JSON, into the yielded list (filled when the block ends)."""
    stream, lines = io.StringIO(), []
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(project))
    logger = logging.getLogger("gateway")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        lines.extend(json.loads(line) for line in stream.getvalue().splitlines())


# -- the line format ---------------------------------------------------------------

def test_a_json_line_has_the_fields_cloud_logging_reads():
    with json_log_lines() as lines:
        logging.getLogger("gateway").warning("upstream %s is slow", "woo",
                                             extra={"json_fields": {"server": "woo", "tool": None, "severity": "x"}})
    [line] = lines
    assert line["severity"] == "WARNING" and line["message"] == "upstream woo is slow"
    assert line["logger"] == "gateway" and line["time"].endswith("+00:00")
    assert line["server"] == "woo"
    assert "tool" not in line  # empty fields are left out
    assert TRACE_FIELD not in line  # not inside a request


def test_an_exception_stack_trace_is_part_of_the_message():
    with json_log_lines() as lines:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logging.getLogger("gateway").exception("sync failed")
    [line] = lines
    assert line["severity"] == "ERROR"
    assert line["message"].startswith("sync failed\nTraceback") and "RuntimeError: boom" in line["message"]


def test_the_trace_link_needs_a_trace_id_and_a_project():
    token = _trace_id.set(TRACE)
    try:
        with json_log_lines(project="demo-project") as with_project, json_log_lines() as without:
            logging.getLogger("gateway").info("inside a request")
    finally:
        _trace_id.reset(token)
    assert with_project[0][TRACE_FIELD] == f"projects/demo-project/traces/{TRACE}"
    assert TRACE_FIELD not in without[0]


@pytest.mark.parametrize("headers, expected", [
    ({"x-cloud-trace-context": f"{TRACE}/1;o=1"}, TRACE),
    ({"x-cloud-trace-context": TRACE.upper()}, TRACE),
    ({"traceparent": f"00-{TRACE}-b7ad6b7169203331-01"}, TRACE),
    ({"x-cloud-trace-context": "not-a-trace", "traceparent": f"00-{TRACE}-b7ad6b7169203331-01"}, TRACE),
    ({"x-cloud-trace-context": 'abc", "severity": "CRITICAL'}, None),  # client-controlled: junk is dropped
    ({"x-cloud-trace-context": "0" * 32}, None),  # all zeros means "no trace"
    ({"traceparent": "00-xyz"}, None),
    ({}, None),
])
def test_only_well_formed_trace_ids_are_kept(headers, expected):
    assert trace_id_from_headers(headers) == expected


# -- audit rows become log lines ---------------------------------------------------

@pytest.fixture
def audit(tmp_path):
    return AuditLog(f"sqlite:///{tmp_path / 'audit.db'}")


def test_a_tool_call_is_logged_once_when_it_finishes_with_ids_only(audit):
    with json_log_lines() as lines:
        entry = audit.begin(user_id="u1", user_email="alice@example.com", key_id="ab12cd34",
                            server_slug="woo", tool="list_orders", source="chat")
        audit.finish(entry.id, outcome="success", duration_ms=412)
    [line] = lines  # nothing for the "started" row
    assert line["event"] == "tool_call" and line["severity"] == "INFO"
    assert (line["server"], line["tool"], line["outcome"], line["duration_ms"], line["source"]) == \
        ("woo", "list_orders", "success", 412, "chat")
    assert (line["user_id"], line["key_id"], line["audit_id"]) == ("u1", "ab12cd34", entry.id)
    assert "alice@example.com" not in json.dumps(line)


def test_a_failed_call_is_a_warning(audit):
    with json_log_lines() as lines:
        entry = audit.begin(user_id="u1", server_slug="woo", tool="list_orders")
        audit.finish(entry.id, outcome="error", duration_ms=10_000, error_kind="timeout")
    [line] = lines
    assert line["severity"] == "WARNING" and line["error_kind"] == "timeout"


def test_a_one_step_outcome_is_logged_by_record(audit):
    with json_log_lines() as lines:
        audit.record(outcome="auth_failed", key_id="ab12cd34", error_kind="invalid_key")
    [line] = lines
    assert (line["event"], line["outcome"], line["severity"]) == ("tool_call", "auth_failed", "WARNING")


def test_admin_events_never_log_an_email(audit):
    with json_log_lines() as lines:
        audit.record_event(actor="admin@example.com", action="user_created", details={"email": "bob@example.com"})
        audit.record_event(actor="system:seed", action="server_created", server_slug="woo")
    text = json.dumps(lines)
    assert "@example.com" not in text
    assert [(l["action"], l["actor"]) for l in lines] == [("user_created", "user"), ("server_created", "system:seed")]
    assert lines[1]["server"] == "woo"


# -- end to end: a request's trace id reaches the audit line ------------------------

async def test_a_requests_trace_id_reaches_its_audit_line(tmp_path):
    gw = create_gateway(Settings(secret_key=generate_key(), db_url=f"sqlite:///{tmp_path / 'gw.db'}",
                                 jobs_db_url=f"sqlite:///{tmp_path / 'jobs.db'}"))
    with json_log_lines(project="demo-project") as lines:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.http_app()), base_url="http://gw") as h:
            r = await h.post("/api/login", json={"api_key": KEY}, headers={"X-Cloud-Trace-Context": f"{TRACE}/7;o=1"})
    assert r.status_code == 401
    [line] = [l for l in lines if l.get("event") == "tool_call"]
    assert line["outcome"] == "auth_failed" and line["source"] == "api" and line["key_id"] == "ab12cd34"
    assert line[TRACE_FIELD] == f"projects/demo-project/traces/{TRACE}"
    assert KEY not in json.dumps(lines)  # the key itself is never logged


async def test_a_rejected_mcp_key_is_logged_once_as_mcp_with_its_trace(tmp_path):
    """The key check runs inside FastMCP's auth middleware; the trace and path middleware wrap the whole app, so the
    refusal line carries the trace, and it is recorded once, as an MCP call (not as a call to another route)."""
    gw = create_gateway(Settings(secret_key=generate_key(), db_url=f"sqlite:///{tmp_path / 'gw.db'}",
                                 jobs_db_url=f"sqlite:///{tmp_path / 'jobs.db'}"))
    with json_log_lines(project="demo-project") as lines:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.http_app()), base_url="http://gw") as h:
            r = await h.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={
                "Authorization": f"Bearer {KEY}", "X-Cloud-Trace-Context": f"{TRACE}/7;o=1",
                "Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    [line] = [l for l in lines if l.get("event") == "tool_call"]
    assert line["outcome"] == "auth_failed" and line["source"] == "mcp" and line["key_id"] == "ab12cd34"
    assert line[TRACE_FIELD] == f"projects/demo-project/traces/{TRACE}"
    assert [row.source for row in gw.audit.query(outcome="auth_failed")] == ["mcp"]
    assert KEY not in json.dumps(lines)
