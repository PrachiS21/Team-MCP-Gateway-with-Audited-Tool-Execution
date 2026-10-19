"""Durable store for pending undo jobs.

Lives in its OWN SQLite file, not the audit database: undo jobs are created exactly
when the audit database has failed, so they cannot depend on it. Each job has a unique
id. Workers claim a job with a version check (UPDATE ... WHERE attempts = N), so two
workers can never both take it, and a crashed worker's claim expires (lease) and the
job is picked up again. The payload (arguments, results, captured state) may hold
customer data, so it is encrypted.

States: pending -> running -> done | conflict | failed   (running -> pending on retry)
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, and_, create_engine, or_, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from gateway.registry.crypto import SecretBox

STATES = ("pending", "running", "done", "conflict", "failed")
TERMINAL = ("done", "conflict", "failed")


class _Base(DeclarativeBase):
    pass


class _JobRow(_Base):
    __tablename__ = "undo_jobs"
    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    audit_entry_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    source: Mapped[str] = mapped_column(String(8), default="mcp")
    server_slug: Mapped[str] = mapped_column(String(63))
    tool: Mapped[str] = mapped_column(String(200))
    lock_key: Mapped[str] = mapped_column(String(300))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    payload_enc: Mapped[str] = mapped_column(Text)  # encrypted JSON
    state: Mapped[str] = mapped_column(String(10), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    final_error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)  # what the audit row will say
    audit_done: Mapped[bool] = mapped_column(Boolean, default=False)  # audit row closed yet?
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@dataclass(frozen=True)
class UndoJob:
    job_id: str
    audit_entry_id: int | None
    user_id: str | None
    user_email: str | None
    source: str
    server_slug: str
    tool: str
    lock_key: str
    duration_ms: int
    state: str
    attempts: int
    next_run_at: datetime
    last_error: str | None
    final_error_kind: str | None
    audit_done: bool
    created_at: datetime
    payload: dict[str, Any]  # args, result, before, undo_args


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)  # SQLite drops tz info


class UndoJobStore:
    def __init__(self, db_url: str, box: SecretBox, lease_seconds: float = 120.0, clock=None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))  # injectable so tests control "now"
        self._engine = create_engine(db_url)
        _Base.metadata.create_all(self._engine)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        self._box = box
        self._lease = timedelta(seconds=lease_seconds)

    def _job(self, r: _JobRow) -> UndoJob:
        return UndoJob(r.job_id, r.audit_entry_id, r.user_id, r.user_email, r.source, r.server_slug, r.tool,
                       r.lock_key, r.duration_ms, r.state, r.attempts, _aware(r.next_run_at), r.last_error,
                       r.final_error_kind, r.audit_done, _aware(r.created_at),
                       json.loads(self._box.decrypt(r.payload_enc)))

    def enqueue(self, *, audit_entry_id: int | None, user_id: str | None, user_email: str | None, source: str,
                server_slug: str, tool: str, lock_key: str, duration_ms: int, payload: dict[str, Any],
                now: datetime | None = None) -> str:
        now = now or self._clock()
        row = _JobRow(job_id=uuid.uuid4().hex, audit_entry_id=audit_entry_id, user_id=user_id,
                      user_email=user_email, source=source, server_slug=server_slug, tool=tool,
                      lock_key=lock_key, duration_ms=duration_ms,
                      payload_enc=self._box.encrypt(json.dumps(payload)), state="pending", attempts=0,
                      next_run_at=now, audit_done=False, created_at=now, updated_at=now)
        with self._sessions() as s:
            s.add(row)
            s.commit()
        return row.job_id

    def claim_next(self, now: datetime) -> UndoJob | None:
        """Take the oldest due job: pending and due, or running with an expired lease
        (its worker died). Optimistic: the UPDATE only succeeds if nobody changed the
        job since we read it."""
        due = or_(and_(_JobRow.state == "pending", _JobRow.next_run_at <= now),
                  and_(_JobRow.state == "running", _JobRow.lease_until < now))
        with self._sessions() as s:
            row = s.scalars(select(_JobRow).where(due).order_by(_JobRow.next_run_at).limit(1)).first()
            if row is None:
                return None
            res = s.execute(update(_JobRow)
                            .where(_JobRow.job_id == row.job_id, _JobRow.attempts == row.attempts,
                                   _JobRow.state == row.state)
                            .values(state="running", attempts=row.attempts + 1,
                                    lease_until=now + self._lease, updated_at=now))
            s.commit()
            if res.rowcount != 1:
                return None  # another worker got it first
            s.refresh(row)
            return self._job(row)

    def reschedule(self, job_id: str, next_run_at: datetime, error: str) -> None:
        self._set(job_id, state="pending", next_run_at=next_run_at, last_error=error[:300], lease_until=None)

    def finish(self, job_id: str, state: str, final_error_kind: str, error: str | None = None) -> None:
        if state not in TERMINAL:
            raise ValueError(f"finish() needs one of {TERMINAL}")
        self._set(job_id, state=state, final_error_kind=final_error_kind, last_error=(error or "")[:300] or None,
                  lease_until=None)

    def mark_audit_done(self, job_id: str) -> None:
        self._set(job_id, audit_done=True)

    def _set(self, job_id: str, **values: Any) -> None:
        with self._sessions() as s:
            s.execute(update(_JobRow).where(_JobRow.job_id == job_id)
                      .values(updated_at=datetime.now(timezone.utc), **values))
            s.commit()

    def has_open_job(self, lock_key: str) -> bool:
        """True while an undo for this resource is queued or running. GatewayService
        refuses new changes to the resource meanwhile, so nothing builds on a state
        that is about to be reversed."""
        with self._sessions() as s:
            return s.scalars(select(_JobRow.job_id).where(
                _JobRow.lock_key == lock_key, _JobRow.state.in_(("pending", "running"))).limit(1)).first() is not None

    def unaudited(self) -> list[UndoJob]:
        """Finished jobs whose audit row has not been closed yet (the audit DB was down)."""
        with self._sessions() as s:
            rows = s.scalars(select(_JobRow).where(_JobRow.state.in_(TERMINAL), _JobRow.audit_done.is_(False)))
            return [self._job(r) for r in rows]

    def get(self, job_id: str) -> UndoJob:
        with self._sessions() as s:
            row = s.get(_JobRow, job_id)
            if row is None:
                raise KeyError(job_id)
            return self._job(row)

    def list(self, state: str | None = None) -> list[UndoJob]:
        with self._sessions() as s:
            stmt = select(_JobRow).order_by(_JobRow.created_at)
            if state:
                stmt = stmt.where(_JobRow.state == state)
            return [self._job(r) for r in s.scalars(stmt)]
