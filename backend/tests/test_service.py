import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from gateway.audit.service import AuditLog
from gateway.compensation import Compensation, CompensationRegistry
from gateway.incidents import IncidentLog
from gateway.jobs import UndoJobStore
from gateway.locks import KeyedLocks
from gateway.mcp.manager import UpstreamManager
from gateway.mcp.upstream_client import UpstreamConfig
from gateway.registry.crypto import SecretBox, generate_key
from gateway.service import Caller, GatewayService
from gateway.undo_worker import UndoWorker, backoff_seconds
from tests.fixtures.fake_upstream import FakeUpstream

ALICE = Caller("u-alice", "alice@example.com", "aaaaaaaa")
BOB = Caller("u-bob", "bob@example.com", "bbbbbbbb", source="chat")


class FlakyAudit(AuditLog):
    """An audit log that can be told to fail on the write-ahead or the final write."""
    fail_begin = False
    fail_finish = False

    def begin(self, **kw):
        if self.fail_begin:
            raise RuntimeError("db down (begin)")
        return super().begin(**kw)

    def finish(self, *a, **kw):
        if self.fail_finish:
            raise RuntimeError("db down (finish)")
        return super().finish(*a, **kw)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Shop:
    """A stateful fake upstream: items and an order status we can inspect and reverse."""

    def __init__(self):
        self.items: dict[str, str] = {}
        self.status = "processing"
        self.delete_calls = 0
        self.running = 0
        self.max_running = 0
        self.fake = FakeUpstream("shop", tools=("echo",))
        m = self.fake.mcp

        def create_item(name: str) -> dict:
            self.items[name] = name
            return {"id": name}

        def delete_item(id: str) -> dict:  # idempotent
            self.delete_calls += 1
            self.items.pop(id, None)
            return {"deleted": id}

        def get_item(id: str) -> dict:
            return {"exists": id in self.items}

        def get_status() -> dict:
            return {"status": self.status}

        def set_status(status: str) -> dict:
            self.status = status
            return {"status": status}

        def set_note(note: str) -> str:  # a changing tool with NO registered undo
            self.items["note"] = note
            return "ok"

        def boom() -> str:
            raise ValueError("kaput")

        async def slow_touch(order_id: str) -> str:  # measures overlap of calls on the same resource
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            await asyncio.sleep(0.15)
            self.running -= 1
            return order_id

        for fn in (create_item, delete_item, get_item, get_status, set_status, set_note, boom, slow_touch):
            m.tool(fn, name=fn.__name__)


class Env:
    pass


@pytest.fixture
async def env(tmp_path):
    e = Env()
    e.shop = Shop()
    await e.shop.fake.start()
    e.manager = UpstreamManager()
    e.manager.add("sid", "shop", UpstreamConfig(e.shop.fake.url, timeout=2))
    e.audit = FlakyAudit(f"sqlite:///{tmp_path / 'a.db'}")
    e.jobs_path = tmp_path / "jobs.db"
    e.clock = Clock()
    e.jobs = UndoJobStore(f"sqlite:///{e.jobs_path}", SecretBox(generate_key()), clock=e.clock)
    e.fallback = tmp_path / "incidents.jsonl"
    e.incidents = IncidentLog(e.fallback)
    e.locks = KeyedLocks()
    e.comps = CompensationRegistry()
    e.comps.register("shop", "create_item", Compensation(
        undo_tool="delete_item", build_undo_args=lambda args, result, before: {"id": result["id"]},
        verify_tool="get_item", build_verify_args=lambda args, result, before: {"id": result["id"]},
        still_as_we_left_it=lambda current, args, result, before: current["exists"] is True))
    e.comps.register("shop", "set_status", Compensation(
        before_tool="get_status", build_before_args=lambda args: {},
        undo_tool="set_status", build_undo_args=lambda args, result, before: {"status": before["status"]},
        verify_tool="get_status", build_verify_args=lambda args, result, before: {},
        still_as_we_left_it=lambda current, args, result, before: current["status"] == args["status"]))
    e.comps.register("shop", "slow_touch", Compensation(
        undo_tool="delete_item", build_undo_args=lambda a, r, b: None,
        lock_key=lambda args: f"order:{args['order_id']}"))
    e.svc = GatewayService(e.manager, e.audit, compensations=e.comps, jobs=e.jobs, locks=e.locks, incidents=e.incidents)
    e.worker = UndoWorker(e.manager, e.jobs, e.audit, e.comps, e.locks, e.incidents, max_attempts=3, clock=e.clock)
    yield e
    await e.manager.close()
    await e.shop.fake.stop()


def events(e):
    return [json.loads(line)["event"] for line in e.fallback.read_text().splitlines()] if e.fallback.exists() else []


# -- normal operation -----------------------------------------------------------

async def test_list_tools_is_namespaced(env):
    names = {t.name for t in (await env.svc.list_tools(ALICE)).tools}
    assert {"shop__echo", "shop__create_item", "shop__set_status"} <= names


async def test_success_is_audited_with_user_server_tool_and_duration(env):
    r = await env.svc.call_tool(ALICE, "shop__echo", {"text": "hi"})
    assert r.ok and r.content.data == "shop:echo:hi"
    [row] = env.audit.query()
    assert (row.outcome, row.user_email, row.key_id, row.server_slug, row.tool, row.source) == \
           ("success", "alice@example.com", "aaaaaaaa", "shop", "echo", "mcp")
    assert row.duration_ms >= 0 and row.error_kind is None


async def test_two_users_are_attributed_separately(env):
    await env.svc.call_tool(ALICE, "shop__echo", {})
    await env.svc.call_tool(BOB, "shop__echo", {})
    assert {(r.user_email, r.source) for r in env.audit.query()} == {("alice@example.com", "mcp"), ("bob@example.com", "chat")}
    assert env.audit.count(user_id="u-bob") == 1


async def test_tool_reporting_an_error_is_tool_error(env):
    r = await env.svc.call_tool(ALICE, "shop__boom", {})
    assert r.is_error and not r.ok and r.error_kind is None
    assert env.audit.query()[0].outcome == "tool_error"


async def test_unknown_tool_is_audited_and_safe(env):
    r = await env.svc.call_tool(ALICE, "nope__x", {})
    assert r.error_kind == "unknown_tool" and r.message
    row = env.audit.query()[0]
    assert (row.outcome, row.error_kind, row.server_slug) == ("error", "unknown_tool", None)


async def test_dead_upstream_is_an_audited_error_without_leaking_internals(env):
    await env.shop.fake.stop()
    r = await env.svc.call_tool(ALICE, "shop__echo", {})
    assert r.error_kind == "unreachable"
    assert "127.0.0.1" not in r.message and "http" not in r.message
    row = env.audit.query()[0]
    assert (row.outcome, row.error_kind) == ("error", "unreachable")
    await env.shop.fake.start()


async def test_no_row_is_left_in_started_after_normal_calls(env):
    for name in ("shop__echo", "shop__boom", "nope__x"):
        await env.svc.call_tool(ALICE, name, {})
    assert env.audit.count(outcome="started") == 0 and env.audit.count() == 3


# -- write-ahead: no record, no call --------------------------------------------

async def test_call_is_refused_and_not_made_when_the_write_ahead_fails(env):
    env.audit.fail_begin = True
    r = await env.svc.call_tool(ALICE, "shop__create_item", {"name": "x"})
    assert r.error_kind == "audit_unavailable"
    assert env.shop.items == {}  # the upstream was never called


async def test_failed_pre_read_refuses_the_call(env):
    env.comps.register("shop", "set_status", Compensation(
        before_tool="no_such_tool", build_before_args=lambda a: {},
        undo_tool="set_status", build_undo_args=lambda a, r, b: {"status": "x"}))
    r = await env.svc.call_tool(ALICE, "shop__set_status", {"status": "completed"})
    assert r.error_kind == "precondition_failed"
    assert env.shop.status == "processing"
    assert env.audit.query()[0].error_kind == "precondition_failed"


# -- audit fails after the call: queue an undo ----------------------------------------

async def test_failed_final_audit_write_queues_an_undo_job_without_undoing_inline(env):
    env.audit.fail_finish = True
    r = await env.svc.call_tool(ALICE, "shop__create_item", {"name": "widget"})
    assert r.error_kind == "audit_failed_reversal_queued" and "reversed automatically" in r.message
    assert "widget" in env.shop.items  # not undone on the request path
    [job] = env.jobs.list()
    assert job.state == "pending" and len(job.job_id) == 32 and job.lock_key == "shop:create_item"
    assert job.payload["undo_args"] == {"id": "widget"}
    assert env.audit.query()[0].outcome == "started"  # audit DB never got the final write
    assert events(env) == ["undo_queued"]


async def test_job_payload_is_encrypted_at_rest(env):
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__create_item", {"name": "SECRET-CUSTOMER-NAME"})
    raw = " ".join(str(r) for r in sqlite3.connect(env.jobs_path).execute("select * from undo_jobs"))
    assert "SECRET-CUSTOMER-NAME" not in raw
    assert "SECRET-CUSTOMER-NAME" not in env.fallback.read_text()  # incidents carry no arguments either


async def test_worker_reverses_the_create_and_closes_the_audit_row_when_the_db_returns(env):
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__create_item", {"name": "widget"})
    assert await env.worker.run_once() == 1
    assert env.shop.items == {}
    [job] = env.jobs.list()
    assert job.state == "done" and job.audit_done is False  # audit DB is still down
    assert env.audit.query()[0].outcome == "started"
    env.audit.fail_finish = False  # database recovers
    await env.worker.run_once()
    row = env.audit.query()[0]
    assert (row.outcome, row.error_kind) == ("error", "reversed")
    assert env.jobs.list()[0].audit_done is True
    assert events(env) == ["undo_queued", "compensated"]


async def test_status_is_restored_from_the_captured_previous_value(env):
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__set_status", {"status": "completed"})
    assert env.shop.status == "completed"
    await env.worker.run_once()
    assert env.shop.status == "processing"


async def test_status_change_stays_when_audit_is_fine(env):
    r = await env.svc.call_tool(ALICE, "shop__set_status", {"status": "completed"})
    assert r.ok and env.shop.status == "completed" and env.jobs.list() == []


async def test_optimistic_check_refuses_to_undo_when_someone_else_changed_the_resource(env):
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__set_status", {"status": "completed"})
    env.shop.status = "cancelled"  # an admin edits the order in WordPress meanwhile
    await env.worker.run_once()
    assert env.shop.status == "cancelled"  # NOT overwritten
    assert env.jobs.list()[0].state == "conflict"
    assert "UNDO_CONFLICT_manual_reconciliation_needed" in events(env)
    env.audit.fail_finish = False
    await env.worker.run_once()
    assert env.audit.query()[0].error_kind == "reversal_conflict"


async def test_unreachable_server_is_retried_with_backoff_until_it_succeeds(env):
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__create_item", {"name": "widget"})
    await env.shop.fake.stop()
    assert await env.worker.run_once() == 1
    job = env.jobs.list()[0]
    assert job.state == "pending" and job.attempts == 1 and job.next_run_at > env.clock()
    assert await env.worker.run_once() == 0  # not due yet: no busy retrying
    await env.shop.fake.start()
    env.clock.advance(backoff_seconds(1) + 1)
    assert await env.worker.run_once() == 1
    job = env.jobs.list()[0]
    assert job.state == "done" and job.attempts == 2 and env.shop.items == {}


async def test_worker_gives_up_after_max_attempts_and_raises_an_incident(env):
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__create_item", {"name": "widget"})
    await env.shop.fake.stop()
    for _ in range(3):
        await env.worker.run_once()
        env.clock.advance(1000)
    job = env.jobs.list()[0]
    assert job.state == "failed" and job.attempts == 3
    assert "UNDO_FAILED_manual_reconciliation_needed" in events(env)
    await env.shop.fake.start()


async def test_undo_tool_that_keeps_reporting_an_error_ends_as_failed_with_an_incident(env):
    # A missing undo tool comes back from the server as an ordinary tool error, which the
    # worker cannot tell from a temporary one, so it retries up to max_attempts, then gives up.
    env.comps.register("shop", "create_item", Compensation(
        undo_tool="no_such_tool", build_undo_args=lambda a, r, b: {"id": r["id"]}))
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__create_item", {"name": "widget"})
    for _ in range(3):
        await env.worker.run_once()
        env.clock.advance(1000)
    job = env.jobs.list()[0]
    assert job.state == "failed" and job.attempts == 3
    assert "widget" in env.shop.items
    assert "UNDO_FAILED_manual_reconciliation_needed" in events(env)


async def test_a_finished_job_is_never_run_twice(env):
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__create_item", {"name": "widget"})
    await env.worker.run_once()
    await env.worker.run_once()
    env.clock.advance(10_000)
    await env.worker.run_once()
    assert env.shop.delete_calls == 1


async def test_without_a_job_store_the_failure_is_reported_honestly(env):
    env.svc.jobs = None
    env.audit.fail_finish = True
    r = await env.svc.call_tool(ALICE, "shop__create_item", {"name": "widget"})
    assert r.error_kind == "audit_failed_not_rolled_back"
    assert "COMPENSATION_NOT_QUEUED_manual_reconciliation_needed" in events(env)


async def test_tool_without_a_registered_undo_fails_open_and_logs_loudly(env):
    env.audit.fail_finish = True
    r = await env.svc.call_tool(ALICE, "shop__set_note", {"note": "hello"})
    assert r.ok and env.shop.items["note"] == "hello"
    assert events(env) == ["UNRECORDED_ACTION_no_undo_registered"] and env.jobs.list() == []


async def test_call_that_failed_is_not_undone_when_the_audit_write_fails(env):
    env.audit.fail_finish = True
    await env.shop.fake.stop()
    r = await env.svc.call_tool(ALICE, "shop__create_item", {"name": "x"})
    assert r.error_kind == "unreachable"
    assert events(env) == ["audit_write_failed_call_had_failed"] and env.jobs.list() == []
    await env.shop.fake.start()


# -- no racing -----------------------------------------------------------------

async def test_undo_waits_for_the_resource_lock(env):
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__create_item", {"name": "widget"})
    job = env.jobs.list()[0]
    async with env.locks.hold(job.lock_key):  # another request is using the resource
        task = asyncio.create_task(env.worker.run_once())
        await asyncio.sleep(0.3)
        assert "widget" in env.shop.items and not task.done()  # undo is waiting, not racing
    await task
    assert env.shop.items == {}


async def test_calls_on_the_same_resource_never_overlap_but_different_ones_do(env):
    await asyncio.gather(*(env.svc.call_tool(ALICE, "shop__slow_touch", {"order_id": "7"}) for _ in range(3)))
    assert env.shop.max_running == 1
    env.shop.max_running = 0
    await asyncio.gather(*(env.svc.call_tool(ALICE, "shop__slow_touch", {"order_id": str(i)}) for i in range(3)))
    assert env.shop.max_running > 1
    assert env.locks._locks == {}  # locks are cleaned up


# -- job store ------------------------------------------------------------------

def test_claims_are_exclusive_and_a_crashed_workers_job_is_reclaimed(tmp_path):
    store = UndoJobStore(f"sqlite:///{tmp_path / 'j.db'}", SecretBox(generate_key()), lease_seconds=60)
    t0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    jid = store.enqueue(audit_entry_id=1, user_id="u", user_email="e", source="mcp", server_slug="s", tool="t",
                        lock_key="k", duration_ms=1, payload={"x": 1}, now=t0)
    assert store.claim_next(t0).job_id == jid
    assert store.claim_next(t0 + timedelta(seconds=30)) is None  # lease still valid: nobody else may take it
    again = store.claim_next(t0 + timedelta(seconds=61))  # worker died; lease expired
    assert again.job_id == jid and again.attempts == 2


def test_job_ids_are_unique_and_jobs_survive_a_restart(tmp_path):
    box = SecretBox(generate_key())
    url = f"sqlite:///{tmp_path / 'j.db'}"
    store = UndoJobStore(url, box)
    kw = dict(audit_entry_id=None, user_id="u", user_email="e", source="mcp", server_slug="s", tool="t",
              lock_key="k", duration_ms=0, payload={"a": 1})
    ids = {store.enqueue(**kw) for _ in range(20)}
    assert len(ids) == 20
    assert len(UndoJobStore(url, box).list("pending")) == 20


def test_backoff_grows_and_is_capped():
    assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert backoff_seconds(30) == 300


def test_registry_rejects_half_configured_hooks():
    reg = CompensationRegistry()
    with pytest.raises(ValueError):
        reg.register("s", "t", Compensation(undo_tool="u", build_undo_args=lambda *a: {}, before_tool="b"))
    with pytest.raises(ValueError):
        reg.register("s", "t", Compensation(undo_tool="u", build_undo_args=lambda *a: {}, verify_tool="v"))


# -- a second user while an undo is pending ------------------------------------------

async def test_second_user_is_refused_while_an_undo_is_pending_then_allowed_after(env):
    env.audit.fail_finish = True
    first = await env.svc.call_tool(ALICE, "shop__set_status", {"status": "completed"})  # audit fails -> undo queued
    assert first.error_kind == "audit_failed_reversal_queued"
    env.audit.fail_finish = False

    blocked = await env.svc.call_tool(BOB, "shop__set_status", {"status": "shipped"})
    assert blocked.error_kind == "resource_busy" and "reversed" in blocked.message
    assert env.shop.status == "completed"  # Bob's change was NOT applied on top of the failed action
    bob_row = env.audit.query(user_id="u-bob")[0]
    assert (bob_row.outcome, bob_row.error_kind) == ("error", "resource_busy")
    assert len(env.jobs.list()) == 1  # Bob's refused call queued nothing

    await env.worker.run_once()  # the undo runs
    assert env.shop.status == "processing"  # back to the version before the failed action
    ok = await env.svc.call_tool(BOB, "shop__set_status", {"status": "shipped"})
    assert ok.ok and env.shop.status == "shipped"  # Bob now builds on the restored version


async def test_pending_undo_blocks_only_that_resource_and_never_reads(env):
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__set_status", {"status": "completed"})
    env.audit.fail_finish = False
    assert (await env.svc.call_tool(BOB, "shop__create_item", {"name": "other"})).ok  # different resource
    assert (await env.svc.call_tool(BOB, "shop__get_status", {})).ok  # reads are not blocked
    assert (await env.svc.call_tool(BOB, "shop__echo", {"text": "x"})).ok


async def test_resource_unblocks_after_a_conflict_or_failure_too(env):
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__set_status", {"status": "completed"})
    env.audit.fail_finish = False
    env.shop.status = "cancelled"  # changed outside the gateway -> the undo will end as a conflict
    await env.worker.run_once()
    assert env.jobs.list()[0].state == "conflict"
    assert (await env.svc.call_tool(BOB, "shop__set_status", {"status": "shipped"})).ok  # no longer blocked


# -- internal (hidden) tools ------------------------------------------------------------------------

async def test_a_hidden_tool_is_not_listed_and_a_direct_call_is_refused_and_audited(env):
    env.comps.hide("shop", "delete_item")
    env.shop.items["x"] = "x"
    assert "shop__delete_item" not in {t.name for t in (await env.svc.list_tools(ALICE)).tools}
    assert "shop__create_item" in {t.name for t in (await env.svc.list_tools(ALICE)).tools}  # others unaffected

    r = await env.svc.call_tool(ALICE, "shop__delete_item", {"id": "x"})
    assert r.error_kind == "unknown_tool" and r.message == "No such tool."
    assert env.shop.delete_calls == 0 and "x" in env.shop.items  # never reached the upstream
    row = env.audit.query()[0]
    assert (row.outcome, row.error_kind, row.server_slug, row.tool, row.user_email) == \
           ("denied", "internal_tool", "shop", "delete_item", "alice@example.com")
    # Known limit: a nonexistent tool on a REAL server is forwarded and answered by the upstream itself, so a
    # caller can tell "internal" from "does not exist" by comparing replies. Low impact: still refused and audited.
    missing = await env.svc.call_tool(ALICE, "shop__no_such_tool", {})
    assert missing.error_kind is None and missing.is_error


async def test_the_undo_worker_can_still_use_a_hidden_tool(env):
    env.comps.hide("shop", "delete_item")
    env.audit.fail_finish = True
    await env.svc.call_tool(ALICE, "shop__create_item", {"name": "widget"})
    await env.worker.run_once()
    assert env.shop.items == {} and env.shop.delete_calls == 1  # undone through the hidden tool


async def test_a_refused_hidden_call_still_works_when_the_audit_store_is_down(env):
    env.comps.hide("shop", "delete_item")
    env.audit.fail_begin = True  # record() is used for the refusal, not begin(); either way it must not crash
    r = await env.svc.call_tool(ALICE, "shop__delete_item", {"id": "x"})
    assert r.error_kind == "unknown_tool"
