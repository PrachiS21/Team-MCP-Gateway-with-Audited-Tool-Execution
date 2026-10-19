"""Background worker that reverses actions whose audit record could not be written.

Per job, in this order:
  1. take the resource lock (the same one GatewayService holds around compensable
     calls), so the undo never overlaps another request on that resource;
  2. optimistic check: re-read the resource; if it is no longer as our action left it,
     someone else changed it, so do NOT undo -> job ends as "conflict" for an admin;
  3. run the undo tool; an unreachable/slow server is retried with exponential backoff
     (long latency is fine, success is the goal); an unknown undo tool fails at once;
  4. close the audit row with the final result; if the audit DB is still down, the job
     keeps that step pending and it is retried on every pass.
Undo tools must be idempotent: retries are at-least-once.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from gateway.audit.service import AuditLog
from gateway.compensation import CompensationRegistry, payload
from gateway.incidents import IncidentLog
from gateway.jobs import UndoJob, UndoJobStore
from gateway.locks import KeyedLocks
from gateway.mcp.manager import UpstreamManager, namespaced
from gateway.mcp.upstream_client import ErrorKind

log = logging.getLogger("gateway")

REVERSED, CONFLICT, FAILED = "reversed", "reversal_conflict", "reversal_failed"


def backoff_seconds(attempt: int, base: float = 5.0, cap: float = 300.0) -> float:
    return min(cap, base * (2 ** max(0, attempt - 1)))


class UndoWorker:
    def __init__(self, manager: UpstreamManager, store: UndoJobStore, audit: AuditLog,
                 compensations: CompensationRegistry, locks: KeyedLocks, incidents: IncidentLog, *,
                 max_attempts: int = 50, clock: Callable[[], datetime] | None = None):
        self.manager, self.store, self.audit = manager, store, audit
        self.compensations, self.locks, self.incidents = compensations, locks, incidents
        self.max_attempts = max_attempts
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = asyncio.Event()

    async def run_once(self) -> int:
        """Process every job that is due now. Returns how many were attempted."""
        self._close_audit_rows()
        done = 0
        while (job := self.store.claim_next(self._clock())) is not None:
            await self._process(job)
            done += 1
        self._close_audit_rows()
        return done

    async def run_forever(self, interval: float = 2.0) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - the worker must outlive any one bad pass
                log.exception("undo worker pass failed")
            try:
                await asyncio.wait_for(self._stop.wait(), interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    # -- one job ----------------------------------------------------------

    async def _process(self, job: UndoJob) -> None:
        comp = self.compensations.get(job.server_slug, job.tool)
        if comp is None:
            return self._end(job, "failed", FAILED, "no compensation registered for this tool any more")
        p = job.payload
        try:
            async with self.locks.hold(job.lock_key):
                if comp.verify_tool:
                    check = await self.manager.call_tool(
                        namespaced(job.server_slug, comp.verify_tool),
                        comp.build_verify_args(p["args"], p["result"], p["before"]))
                    if not check.ok:
                        return self._retry(job, check.error.kind.value if check.error else "verify_failed")
                    if not comp.still_as_we_left_it(payload(check.content), p["args"], p["result"], p["before"]):
                        return self._end(job, "conflict", CONFLICT, "resource changed since our action")
                undo = await self.manager.call_tool(namespaced(job.server_slug, comp.undo_tool), p["undo_args"])
        except Exception as exc:  # noqa: BLE001 - e.g. a broken verify callback
            log.exception("undo job %s raised", job.job_id)
            return self._retry(job, f"{type(exc).__name__}")

        if undo.ok:
            return self._end(job, "done", REVERSED)
        if undo.error is not None and undo.error.kind is ErrorKind.UNKNOWN_TOOL:
            return self._end(job, "failed", FAILED, "undo tool does not exist on the server")
        self._retry(job, undo.error.kind.value if undo.error else "undo_tool_error")

    def _retry(self, job: UndoJob, reason: str) -> None:
        if job.attempts >= self.max_attempts:
            return self._end(job, "failed", FAILED, f"gave up after {job.attempts} attempts: {reason}")
        self.store.reschedule(job.job_id, self._clock() + timedelta(seconds=backoff_seconds(job.attempts)), reason)

    def _end(self, job: UndoJob, state: str, final_kind: str, error: str | None = None) -> None:
        self.store.finish(job.job_id, state, final_kind, error)
        if state != "done":  # done is the happy path; conflict/failed need a human
            self.incidents.emit(f"UNDO_{state.upper()}_manual_reconciliation_needed",
                                {"job_id": job.job_id, "user": job.user_email, "server": job.server_slug,
                                 "tool": job.tool, "reason": error})
        else:
            self.incidents.emit("compensated", {"job_id": job.job_id, "user": job.user_email,
                                                "server": job.server_slug, "tool": job.tool})

    # -- close the audit rows -----------------------------------------------------

    def _close_audit_rows(self) -> None:
        for job in self.store.unaudited():
            try:
                if job.audit_entry_id is not None:
                    self.audit.finish(job.audit_entry_id, outcome="error", duration_ms=job.duration_ms,
                                      error_kind=job.final_error_kind)
                self.store.mark_audit_done(job.job_id)
            except Exception:  # noqa: BLE001 - audit DB still down; try again next pass
                log.warning("could not close audit row for job %s yet", job.job_id)
