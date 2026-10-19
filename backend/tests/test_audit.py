import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from gateway.audit.service import AuditError, AuditLog


@pytest.fixture
def db(tmp_path):
    return tmp_path / "audit.db"


@pytest.fixture
def log(db):
    return AuditLog(f"sqlite:///{db}")


T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def seed(log):
    log.record(outcome="success", user_id="u1", user_email="a@x.com", key_id="aaaaaaaa", server_id="s1",
               server_slug="woo", tool="list_orders", duration_ms=120, ts=T0)
    log.record(outcome="error", user_id="u2", user_email="b@x.com", key_id="bbbbbbbb", server_id="s2",
               server_slug="docs", tool="search", duration_ms=5000, error_kind="timeout", ts=T0 + timedelta(minutes=1))
    log.record(outcome="tool_error", user_id="u1", user_email="a@x.com", server_slug="woo", tool="get_order",
               duration_ms=40, source="chat", ts=T0 + timedelta(minutes=2))
    log.record(outcome="auth_failed", key_id="cccccccc", error_kind="revoked", ts=T0 + timedelta(minutes=3))


def test_record_returns_what_was_stored(log):
    e = log.record(outcome="success", user_id="u1", user_email="a@x.com", server_slug="woo",
                   tool="list_orders", duration_ms=12)
    assert e.id and e.source == "mcp" and e.ts.tzinfo is not None
    assert log.query()[0] == e


def test_query_is_newest_first_and_attributes_each_row(log):
    seed(log)
    rows = log.query()
    assert [r.outcome for r in rows] == ["auth_failed", "tool_error", "error", "success"]
    by = {r.outcome: r for r in rows}
    assert by["success"].user_email == "a@x.com" and by["error"].user_email == "b@x.com"
    assert by["auth_failed"].user_id is None and by["auth_failed"].key_id == "cccccccc"
    assert by["tool_error"].source == "chat"


def test_filters(log):
    seed(log)
    assert {r.outcome for r in log.query(user_id="u1")} == {"success", "tool_error"}
    assert [r.tool for r in log.query(server_slug="docs")] == ["search"]
    assert [r.tool for r in log.query(tool="get_order")] == ["get_order"]
    assert [r.outcome for r in log.query(outcome="auth_failed")] == ["auth_failed"]
    assert [r.outcome for r in log.query(user_id="u1", outcome="success")] == ["success"]
    # since is inclusive, until exclusive
    assert len(log.query(since=T0 + timedelta(minutes=2))) == 2
    assert len(log.query(until=T0 + timedelta(minutes=2))) == 2
    assert len(log.query(since=T0 + timedelta(minutes=1), until=T0 + timedelta(minutes=3))) == 2
    assert log.query(user_id="nobody") == []


def test_paging_and_count(log):
    seed(log)
    assert log.count() == 4 and log.count(user_id="u1") == 2 and log.count(outcome="error") == 1
    page1, page2 = log.query(limit=3), log.query(limit=3, offset=3)
    assert len(page1) == 3 and len(page2) == 1
    assert [r.id for r in page1 + page2] == sorted((r.id for r in page1 + page2), reverse=True)
    assert len(log.query(limit=10_000)) == 4  # clamped, not an error


def test_validation(log):
    with pytest.raises(AuditError):
        log.record(outcome="maybe")
    with pytest.raises(AuditError):
        log.record(outcome="success", source="smoke-signal")
    assert log.record(outcome="success", duration_ms=-5).duration_ms == 0  # clock skew can't make negatives


def test_denied_outcome_is_reserved_for_permissions(log):
    assert log.record(outcome="denied", user_id="u1", tool="create_product", error_kind="forbidden").outcome == "denied"


def test_schema_has_no_place_to_store_arguments_or_results(db, log):
    cols = {r[1] for r in sqlite3.connect(db).execute("pragma table_info(audit_log)")}
    assert not ({"arguments", "args", "result", "output", "params"} & cols)


def test_rows_survive_a_new_instance_and_keep_history_without_users_or_servers(db, log):
    seed(log)
    fresh = AuditLog(f"sqlite:///{db}")  # no users/servers tables are consulted at all
    assert fresh.count() == 4 and fresh.query(server_slug="woo")[0].user_email == "a@x.com"
