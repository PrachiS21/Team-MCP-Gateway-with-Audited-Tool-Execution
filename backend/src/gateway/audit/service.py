"""Append-only audit log of tool calls.

record() raises if the write fails. Whether a failed audit write should fail the
call (fail closed) or only be logged (fail open) is decided by the caller, not here.

Each finished call and each admin event is also written as one structured log line
(logger "gateway.audit", see logs.py), without emails or anything secret.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from .models import AdminEventRow, AuditRow, Base

_log = logging.getLogger("gateway.audit")

OUTCOMES = ("started", "success", "tool_error", "error", "auth_failed", "denied")
# "started" is the write-ahead row; a row that stays "started" means the process died mid-call.
SOURCES = ("mcp", "chat", "api")  # api = dashboard login attempts
MAX_PAGE = 500


class AuditError(ValueError):
    pass


@dataclass(frozen=True)
class AuditEntry:
    id: int
    ts: datetime
    user_id: str | None
    user_email: str | None
    key_id: str | None
    server_id: str | None
    server_slug: str | None
    tool: str | None
    duration_ms: int
    outcome: str
    error_kind: str | None
    source: str


@dataclass(frozen=True)
class AdminEvent:
    id: int
    ts: datetime
    actor: str
    action: str
    server_slug: str | None
    details: dict


def _event(r: AdminEventRow) -> AdminEvent:
    ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=timezone.utc)
    return AdminEvent(r.id, ts, r.actor, r.action, r.server_slug, json.loads(r.details or "{}"))


def _log_call(r: AuditRow) -> None:
    """One structured line per finished call: what Cloud Monitoring counts (log-based metrics), and the copy of the
    audit trail that outlives this database. Ids only: no email, no arguments, nothing secret."""
    _log.log(logging.INFO if r.outcome == "success" else logging.WARNING,
             "%s call %s/%s %s in %d ms", r.source, r.server_slug or "-", r.tool or "-", r.outcome, r.duration_ms,
             extra={"json_fields": {
                 "event": "tool_call", "outcome": r.outcome, "source": r.source, "server": r.server_slug,
                 "tool": r.tool, "duration_ms": r.duration_ms, "error_kind": r.error_kind,
                 "user_id": r.user_id, "key_id": r.key_id, "audit_id": r.id}})


def _entry(r: AuditRow) -> AuditEntry:
    ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=timezone.utc)  # SQLite drops tz info
    return AuditEntry(r.id, ts, r.user_id, r.user_email, r.key_id, r.server_id, r.server_slug,
                      r.tool, r.duration_ms, r.outcome, r.error_kind, r.source)


class AuditLog:
    def __init__(self, db_url: str):
        self._engine = create_engine(db_url)
        Base.metadata.create_all(self._engine)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)

    def record(self, *, outcome: str, source: str = "mcp", user_id: str | None = None,
               user_email: str | None = None, key_id: str | None = None,
               server_id: str | None = None, server_slug: str | None = None,
               tool: str | None = None, duration_ms: int = 0, error_kind: str | None = None,
               ts: datetime | None = None) -> AuditEntry:
        if outcome not in OUTCOMES:
            raise AuditError(f"outcome must be one of {OUTCOMES}")
        if source not in SOURCES:
            raise AuditError(f"source must be one of {SOURCES}")
        row = AuditRow(ts=ts or datetime.now(timezone.utc), user_id=user_id, user_email=user_email,
                       key_id=key_id, server_id=server_id, server_slug=server_slug, tool=tool,
                       duration_ms=max(0, int(duration_ms)), outcome=outcome,
                       error_kind=error_kind, source=source)
        with self._sessions() as s:
            s.add(row)
            s.commit()
        if outcome != "started":  # a started row is logged once finish() knows how it ended
            _log_call(row)
        return _entry(row)

    def begin(self, **fields) -> AuditEntry:
        """Write-ahead: record that a call is about to run. If this raises, the
        caller must not run the call."""
        return self.record(outcome="started", **fields)

    def finish(self, entry_id: int, *, outcome: str, duration_ms: int, error_kind: str | None = None) -> None:
        """Turn a 'started' row into its final outcome."""
        if outcome not in OUTCOMES or outcome == "started":
            raise AuditError("finish() needs a final outcome")
        with self._sessions() as s:
            row = s.get(AuditRow, entry_id)
            if row is None:
                raise AuditError(f"unknown audit entry {entry_id}")
            row.outcome, row.duration_ms, row.error_kind = outcome, max(0, int(duration_ms)), error_kind
            s.commit()
        _log_call(row)

    def _filtered(self, stmt, user_id, server_slug, tool, outcome, since, until):
        if user_id is not None:
            stmt = stmt.where(AuditRow.user_id == user_id)
        if server_slug is not None:
            stmt = stmt.where(AuditRow.server_slug == server_slug)
        if tool is not None:
            stmt = stmt.where(AuditRow.tool == tool)
        if outcome is not None:
            stmt = stmt.where(AuditRow.outcome == outcome)
        if since is not None:
            stmt = stmt.where(AuditRow.ts >= since)
        if until is not None:
            stmt = stmt.where(AuditRow.ts < until)
        return stmt

    def query(self, *, user_id: str | None = None, server_slug: str | None = None,
              tool: str | None = None, outcome: str | None = None,
              since: datetime | None = None, until: datetime | None = None,
              limit: int = 100, offset: int = 0) -> list[AuditEntry]:
        """Newest first. `since` is inclusive, `until` exclusive."""
        limit = max(1, min(limit, MAX_PAGE))
        stmt = self._filtered(select(AuditRow), user_id, server_slug, tool, outcome, since, until)
        stmt = stmt.order_by(AuditRow.ts.desc(), AuditRow.id.desc()).limit(limit).offset(max(0, offset))
        with self._sessions() as s:
            return [_entry(r) for r in s.scalars(stmt)]

    def count(self, *, user_id: str | None = None, server_slug: str | None = None,
              tool: str | None = None, outcome: str | None = None,
              since: datetime | None = None, until: datetime | None = None) -> int:
        stmt = self._filtered(select(func.count(AuditRow.id)), user_id, server_slug, tool, outcome, since, until)
        with self._sessions() as s:
            return s.scalar(stmt) or 0

    # -- admin events (configuration changes) ------------------------------------

    def record_event(self, *, actor: str, action: str, server_slug: str | None = None,
                     details: dict | None = None, ts: datetime | None = None) -> AdminEvent:
        """Record a configuration change. `details` must never contain secret values."""
        row = AdminEventRow(ts=ts or datetime.now(timezone.utc), actor=actor, action=action,
                            server_slug=server_slug, details=json.dumps(details or {}))
        with self._sessions() as s:
            s.add(row)
            s.commit()
        _log.info("admin event %s%s", action, f" on {server_slug}" if server_slug else "", extra={"json_fields": {
            "event": "admin_event", "action": action, "server": server_slug,
            "actor": "user" if "@" in actor else actor}})  # "system", "system:seed", "cli:<name>"; emails stay in the DB
        return _event(row)

    def events(self, *, server_slug: str | None = None, action: str | None = None,
               since: datetime | None = None, limit: int = 100, offset: int = 0) -> list[AdminEvent]:
        """Newest first."""
        stmt = select(AdminEventRow)
        if server_slug is not None:
            stmt = stmt.where(AdminEventRow.server_slug == server_slug)
        if action is not None:
            stmt = stmt.where(AdminEventRow.action == action)
        if since is not None:
            stmt = stmt.where(AdminEventRow.ts >= since)
        stmt = stmt.order_by(AdminEventRow.ts.desc(), AdminEventRow.id.desc()).limit(max(1, min(limit, MAX_PAGE))).offset(max(0, offset))
        with self._sessions() as s:
            return [_event(r) for r in s.scalars(stmt)]
