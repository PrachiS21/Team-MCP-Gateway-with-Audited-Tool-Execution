"""GatewayService: the one place where a tool call is recorded and routed.

Both the MCP endpoint and the chat agent call this, so every call is audited the
same way no matter who made it.

Order of a call:
  1. write-ahead audit row ("started"); if this fails the call is REFUSED
  2. for tools with a registered Compensation: take the resource lock (so nothing else
     touches that resource meanwhile) and run the optional read-only "before" call
  3. the call itself, through the UpstreamManager
  4. final audit write. If it fails after a successful call:
       - tool has a Compensation  -> enqueue an undo job (own durable store); the
         UndoWorker reverses it later, with retries, under the same lock
       - no Compensation          -> fail open: return the result, log loudly
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

from gateway.audit.service import AuditLog
from gateway.auth.service import User
from gateway.compensation import CompensationRegistry, payload
from gateway.incidents import IncidentLog
from gateway.jobs import UndoJobStore
from gateway.locks import KeyedLocks
from gateway.mcp.manager import CatalogueResult, UpstreamManager, namespaced, split_name
from gateway.mcp.upstream_client import CallResult, ErrorKind

log = logging.getLogger("gateway")

# What clients are told. Fixed strings: raw upstream errors can contain URLs or other internals.
_MESSAGES = {
    ErrorKind.TIMEOUT.value: "The server did not answer in time.",
    ErrorKind.UNREACHABLE.value: "The server is currently unreachable.",
    ErrorKind.AUTH.value: "The gateway could not authenticate to the server. Ask an admin to check its credential.",
    ErrorKind.UPSTREAM.value: "The server returned an error.",
    ErrorKind.DISABLED.value: "This server is disabled.",
    ErrorKind.UNKNOWN_TOOL.value: "No such tool.",
    "audit_unavailable": "The call was not made because it could not be recorded. Try again shortly.",
    "precondition_failed": "The call was not made because the current state could not be read first.",
    "resource_busy": "A previous action on this resource is being reversed automatically. Try again shortly.",
    "audit_failed_reversal_queued": "The action could not be recorded, so it is being reversed automatically. "
                                    "Do not rely on its result.",
    "audit_failed_not_rolled_back": "The action could not be recorded and could not be queued for reversal. "
                                    "An admin has been alerted.",
}


@dataclass(frozen=True)
class Caller:
    user_id: str
    email: str
    key_id: str | None = None  # public id of the API key used, if any
    source: str = "mcp"  # "mcp" | "chat"

    @classmethod
    def from_user(cls, user: User, key_id: str | None = None, source: str = "mcp") -> "Caller":
        return cls(user.id, user.email, key_id, source)


@dataclass(frozen=True)
class GatewayResult:
    content: Any = None  # the upstream's CallToolResult (also for tool_error)
    is_error: bool = False  # the tool ran and reported an error itself
    error_kind: str | None = None  # set when the gateway/upstream failed
    message: str | None = None  # safe text for the client when error_kind is set

    @property
    def ok(self) -> bool:
        return self.error_kind is None and not self.is_error


def _failure(kind: str) -> GatewayResult:
    return GatewayResult(error_kind=kind, message=_MESSAGES.get(kind, "The call failed."))


class GatewayService:
    def __init__(self, manager: UpstreamManager, audit: AuditLog, *,
                 compensations: CompensationRegistry | None = None, jobs: UndoJobStore | None = None,
                 locks: KeyedLocks | None = None, incidents: IncidentLog | None = None):
        self.manager = manager
        self.audit = audit
        self.compensations = compensations or CompensationRegistry()
        self.jobs = jobs
        self.locks = locks or KeyedLocks()  # share this object with the UndoWorker
        self.incidents = incidents or IncidentLog()

    # -- listing ----------------------------------------------------------

    async def list_tools(self, caller: Caller) -> CatalogueResult:
        """Merged, namespaced tools of every enabled server (live, not cached), without internal tools."""
        catalogue = await self.manager.list_all_tools()
        catalogue.tools = [t for t in catalogue.tools if not self.compensations.is_hidden(t.slug, t.original_name)]
        return catalogue
        # Per-user permission filtering will plug in here.

    # -- calling ----------------------------------------------------------

    async def call_tool(self, caller: Caller, name: str, arguments: dict[str, Any] | None = None) -> GatewayResult:
        args = arguments or {}
        target = self.manager.resolve(name)
        server_id = slug = None
        tool = name
        if target is not None:
            server_id, _, tool = target
            slug = split_name(name)[0]

        if slug and self.compensations.is_hidden(slug, tool):
            # Internal tool (used only to undo things): refuse with the generic "No such tool." and leave a
            # trail for admins. (A nonexistent tool on a real server is answered by the upstream instead, so
            # the two replies are not byte-identical; accepted, low impact.)
            self._audit_denied(caller, server_id, slug, tool)
            return _failure("unknown_tool")

        started = time.perf_counter()

        # 1. write-ahead: no record, no call
        try:
            entry = self.audit.begin(user_id=caller.user_id, user_email=caller.email, key_id=caller.key_id,
                                     server_id=server_id, server_slug=slug, tool=tool, source=caller.source)
        except Exception:  # noqa: BLE001
            log.exception("audit write-ahead failed; refusing call to %s", name)
            return _failure("audit_unavailable")

        comp = self.compensations.get(slug, tool) if slug else None
        if comp is None:
            return await self._run(caller, name, args, slug, tool, entry.id, started, None, None)

        # 2. compensable tool: nothing else may touch this resource until we are done
        lock_key = comp.lock_key(args) if comp.lock_key else f"{slug}:{tool}"
        async with self.locks.hold(lock_key):
            # An earlier action on this resource is waiting to be reversed: refuse, so this
            # change does not build on a state that is about to disappear (and cancel the undo).
            if self.jobs is not None and self.jobs.has_open_job(lock_key):
                self._finish(entry.id, "error", int((time.perf_counter() - started) * 1000), "resource_busy")
                return _failure("resource_busy")
            return await self._run(caller, name, args, slug, tool, entry.id, started, comp, lock_key)

    async def _run(self, caller, name, args, slug, tool, entry_id, started, comp, lock_key) -> GatewayResult:
        ms = lambda: int((time.perf_counter() - started) * 1000)  # noqa: E731

        before: Any = None
        if comp is not None and comp.before_tool:
            try:
                pre = await self.manager.call_tool(namespaced(slug, comp.before_tool), comp.build_before_args(args))
            except Exception:  # noqa: BLE001 - e.g. the args builder failed
                log.exception("could not prepare pre-read for %s", name)
                pre = None
            if pre is None or not pre.ok:
                self._finish(entry_id, "error", ms(), "precondition_failed")
                return _failure("precondition_failed")
            before = payload(pre.content)

        # 3. the call
        res: CallResult = await self.manager.call_tool(name, args)
        if res.error is not None:
            outcome, kind = "error", res.error.kind.value
            result = _failure(kind)
        elif res.is_error:
            outcome, kind = "tool_error", None
            result = GatewayResult(content=res.content, is_error=True)
        else:
            outcome, kind = "success", None
            result = GatewayResult(content=res.content)

        # 4. final audit write
        try:
            self.audit.finish(entry_id, outcome=outcome, duration_ms=ms(), error_kind=kind)
        except Exception as exc:  # noqa: BLE001
            return self._audit_write_failed(caller, slug, tool, args, res, result, comp, lock_key, before,
                                            entry_id, ms(), exc)
        return result

    def _audit_denied(self, caller: Caller, server_id, slug, tool) -> None:
        try:
            self.audit.record(outcome="denied", error_kind="internal_tool", source=caller.source,
                              user_id=caller.user_id, user_email=caller.email, key_id=caller.key_id,
                              server_id=server_id, server_slug=slug, tool=tool)
        except Exception:  # noqa: BLE001 - nothing ran, so a missing row is not dangerous
            log.exception("could not record a refused internal-tool call")

    def _finish(self, entry_id: int, outcome: str, duration_ms: int, kind: str | None) -> None:
        try:
            self.audit.finish(entry_id, outcome=outcome, duration_ms=duration_ms, error_kind=kind)
        except Exception:  # noqa: BLE001 - nothing ran, so there is nothing to reverse
            log.exception("could not finish audit entry %s", entry_id)

    # -- audit failed after the call ---------------------------------------------

    def _audit_write_failed(self, caller, slug, tool, args, res: CallResult, result: GatewayResult, comp,
                            lock_key, before, entry_id, duration_ms, exc: Exception) -> GatewayResult:
        base = {"user": caller.email, "server": slug, "tool": tool, "audit_error": f"{type(exc).__name__}: {exc}"}
        if not res.ok:
            self.incidents.emit("audit_write_failed_call_had_failed", base)  # nothing happened, nothing to undo
            return result
        if comp is None:
            self.incidents.emit("UNRECORDED_ACTION_no_undo_registered", base)  # fail open
            return result
        try:
            result_payload = payload(res.content)
            undo_args = comp.build_undo_args(args, result_payload, before)
            if undo_args is None:
                self.incidents.emit("audit_write_failed_nothing_to_undo", base)
                return result
            if self.jobs is None:
                raise RuntimeError("no undo job store configured")
            job_id = self.jobs.enqueue(
                audit_entry_id=entry_id, user_id=caller.user_id, user_email=caller.email, source=caller.source,
                server_slug=slug, tool=tool, lock_key=lock_key, duration_ms=duration_ms,
                payload={"args": args, "result": result_payload, "before": before, "undo_args": undo_args})
        except Exception:  # noqa: BLE001
            log.exception("could not queue the undo for %s", tool)
            self.incidents.emit("COMPENSATION_NOT_QUEUED_manual_reconciliation_needed", base)
            return _failure("audit_failed_not_rolled_back")
        self.incidents.emit("undo_queued", {**base, "job_id": job_id})
        return _failure("audit_failed_reversal_queued")
