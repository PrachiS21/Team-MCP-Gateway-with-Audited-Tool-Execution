import io
import json
import logging
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from gateway.audit.service import AuditLog
from gateway.cli import main as cli_main
from gateway.config import Settings
from gateway.incidents import IncidentLog
from gateway.registry.crypto import SecretBox, generate_key
from gateway.registry.registry import ServerRegistry, safe_url
from gateway.seed import SeedError, apply_seed, load_seed
from gateway.server import create_gateway
from gateway.service import Caller
from tests.fixtures.fake_upstream import FakeUpstream

KEY = generate_key()
URL = "http://10.43.0.12:8000/mcp"  # an IP: a *.local hostname makes macOS wait seconds for multicast DNS
SECRET_1, SECRET_2 = "TOKEN-ONE-s3cret", "TOKEN-TWO-r0tated"


def entry(**kw):
    base = {"slug": "woo", "name": "WooCommerce", "url": URL, "credential_type": "bearer",
            "credential_env": "WOO_TOKEN", "timeout": 10}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


class Env:
    pass


@pytest.fixture
def env(tmp_path):
    e = Env()
    e.db = f"sqlite:///{tmp_path / 'gw.db'}"
    e.seed = tmp_path / "seed.json"
    e.incident_file = tmp_path / "incidents.jsonl"
    e.registry = ServerRegistry(e.db, SecretBox(KEY), allow_loopback=True)
    e.audit = AuditLog(e.db)
    e.incidents = IncidentLog(e.incident_file)
    e.vars = {"WOO_TOKEN": SECRET_1}

    def write(*servers):
        e.seed.write_text(json.dumps({"servers": list(servers)}))

    def run():
        return apply_seed(e.seed, e.vars, e.registry, e.audit, e.incidents)

    e.write, e.run = write, run
    return e


# -- the matching rule: slug, then sync ------------------------------------------------------------------

def test_first_run_creates_the_server_and_records_an_admin_event(env, caplog):
    env.write(entry())
    with caplog.at_level(logging.WARNING, logger="gateway.incident"):
        report = env.run()
    assert [(c.slug, c.action) for c in report.changes] == [("woo", "created")] and report.unchanged == []
    server = env.registry.get_by_slug("woo")
    assert server.enabled and server.url == URL and server.has_credential
    assert env.registry.upstream_config(server.id).credential == SECRET_1
    [ev] = env.audit.events()
    assert (ev.actor, ev.action, ev.server_slug) == ("system:seed", "server_created", "woo")
    assert "server_created_from_seed" in caplog.text  # the admin-visible log line
    assert "server_created_from_seed" in env.incident_file.read_text()  # and the fallback file


def test_a_restart_with_nothing_changed_is_still_audited_but_not_noisy(env, caplog):
    env.write(entry())
    env.run()
    caplog.clear()
    with caplog.at_level(logging.INFO):  # one call: nested at_level() would raise the handler level back
        report = env.run()
    assert report.changes == [] and report.unchanged == ["woo"]
    events = env.audit.events()
    assert [e.action for e in events] == ["server_unchanged", "server_created"]  # the restart left a trail
    assert events[0].details == {"changed": []} and events[0].server_slug == "woo"
    assert any("already matches" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)  # quiet INFO, not an alert
    assert not [r for r in caplog.records if r.name == "gateway.incident"]  # no WARNING for an unchanged start
    assert len(env.incident_file.read_text().splitlines()) == 1  # the fallback file holds only the real change


def test_every_restart_adds_another_audit_row(env):
    env.write(entry())
    for _ in range(4):
        env.run()
    assert [e.action for e in env.audit.events()] == ["server_unchanged"] * 3 + ["server_created"]


def test_a_changed_url_updates_the_same_server_and_is_recorded_with_old_and_new_values(env):
    env.write(entry())
    env.run()
    server_id = env.registry.get_by_slug("woo").id
    env.write(entry(url="http://10.43.0.13:8000/mcp"))
    [change] = env.run().changes
    assert change.action == "updated" and change.changed == ("url",)
    updated = env.registry.get_by_slug("woo")
    assert updated.id == server_id and "10.43.0.13" in updated.url  # same server, new URL: not a second server
    ev = env.audit.events(action="server_updated")[0]
    assert ev.details["changed"] == ["url"]
    assert ev.details["url"] == {"from": URL, "to": "http://10.43.0.13:8000/mcp"}


def test_a_rotated_token_is_applied_and_its_value_is_recorded_nowhere(env, caplog, tmp_path):
    env.write(entry())
    env.run()
    env.vars["WOO_TOKEN"] = SECRET_2
    with caplog.at_level(logging.INFO):
        [change] = env.run().changes
    assert change.changed == ("credential",)
    assert env.registry.upstream_config(env.registry.get_by_slug("woo").id).credential == SECRET_2
    ev = env.audit.events(action="server_updated")[0]
    assert ev.details == {"changed": ["credential"]}  # says THAT it changed, nothing more

    dump = " ".join(str(r) for r in sqlite3.connect(tmp_path / "gw.db").execute("select * from admin_events"))
    everywhere = dump + caplog.text + env.incident_file.read_text() + repr(change) + repr(ev)
    for secret in (SECRET_1, SECRET_2):
        assert secret not in everywhere


def test_the_admins_disabled_switch_survives_a_sync(env):
    env.write(entry())
    env.run()
    env.registry.set_enabled(env.registry.get_by_slug("woo").id, False)
    env.write(entry(url="http://10.43.0.14:8000/mcp"))
    env.run()
    server = env.registry.get_by_slug("woo")
    assert server.enabled is False and "10.43.0.14" in server.url


def test_several_changed_fields_are_all_listed(env):
    env.write(entry())
    env.run()
    env.write(entry(name="Main Store", timeout=3, credential_type="header", credential_header="X-Api-Key"))
    [change] = env.run().changes
    assert set(change.changed) == {"name", "timeout", "credential_type", "credential_header"}


def test_urls_in_events_have_no_password_or_query_string(env):
    assert safe_url("http://user:hunter2@host.svc:8000/mcp?token=abc#frag") == "http://host.svc:8000/mcp"
    env.write(entry(url="http://user:hunter2@10.43.0.15:8000/mcp?token=abc"))
    env.run()
    env.write(entry(url="http://user:hunter2@10.43.0.15:8000/mcp?token=def"))
    env.run()
    text = json.dumps([e.details for e in env.audit.events()]) + env.incident_file.read_text()
    assert "hunter2" not in text and "token=abc" not in text and "token=def" not in text


def test_servers_not_in_the_file_are_left_alone_and_never_deleted(env):
    env.registry.create(name="Hand made", slug="docs", url="http://10.0.0.9:8000/mcp")
    env.write(entry())
    env.run()
    env.write()  # the file no longer mentions woo or docs
    report = env.run()
    assert report.changes == [] and report.unchanged == []
    assert {s.slug for s in env.registry.list()} == {"woo", "docs"}


def test_changing_the_slug_creates_a_second_server(env):
    env.write(entry())
    env.run()
    env.write(entry(slug="woo2"))
    [change] = env.run().changes
    assert change.action == "created"
    assert {s.slug for s in env.registry.list()} == {"woo", "woo2"}  # documented: slugs are permanent


def test_an_existing_server_with_the_same_slug_is_adopted_and_overwritten(env):
    env.registry.create(name="Hand made", slug="woo", url="http://10.0.0.9:8000/mcp")
    env.write(entry())
    [change] = env.run().changes
    assert change.action == "updated" and env.registry.get_by_slug("woo").url == URL


def test_a_failing_audit_store_does_not_stop_startup_but_the_notice_still_goes_out(env, caplog, monkeypatch):
    def broken(**kw):
        raise RuntimeError("audit database is down")
    monkeypatch.setattr(env.audit, "record_event", broken)
    env.write(entry())
    with caplog.at_level(logging.WARNING, logger="gateway.incident"):
        env.run()
    assert env.registry.get_by_slug("woo")  # the server was still registered
    assert "server_created_from_seed" in caplog.text and "server_created_from_seed" in env.incident_file.read_text()


# -- a bad file refuses to start and applies nothing -----------------------------------------------------------

@pytest.mark.parametrize("servers, fragment", [
    ([entry(credential_env="NOT_SET")], "NOT_SET"),
    ([entry(credential_env=None)], "needs credential_env"),
    ([{**entry(), "token": "abc"}], "must not be in the file"),
    ([{**entry(), "credential": "abc"}], "must not be in the file"),
    ([{**entry(), "color": "red"}], "unknown key"),
    ([entry(slug=None)], "'slug' is required"),
    ([entry(url=None)], "'url' is required"),
    ([entry(credential_type="oauth")], "credential_type must be one of"),
    ([entry(credential_type="none")], "credential_env given"),
    ([entry(timeout="fast")], "timeout must be a number"),
    ([entry(timeout=-1)], "timeout must be positive"),
    ([entry(), entry()], "duplicate slug"),
    (["not an object"], "must be an object"),
])
def test_bad_entries_are_refused_with_a_clear_message(env, servers, fragment):
    env.write(*servers)
    with pytest.raises(SeedError, match=fragment):
        env.run()
    assert env.registry.list() == [] and env.audit.events() == []


def test_unsafe_urls_and_bad_slugs_are_refused_and_nothing_is_half_applied(env):
    env.write(entry(slug="good"), entry(slug="meta", url="http://169.254.169.254/latest", credential_type="none", credential_env=None))
    with pytest.raises(SeedError, match="meta"):
        env.run()
    assert env.registry.list() == []  # "good" was not created either
    env.write(entry(slug="Bad_Slug"))
    with pytest.raises(SeedError, match="invalid slug"):
        env.run()


def test_broken_files_are_refused(env, tmp_path):
    with pytest.raises(SeedError, match="does not exist"):
        load_seed(tmp_path / "missing.json", {})
    env.seed.write_text("{not json")
    with pytest.raises(SeedError, match="not valid JSON"):
        env.run()
    env.seed.write_text(json.dumps({"servers": "oops"}))
    with pytest.raises(SeedError, match=r'\{"servers"'):
        env.run()
    env.seed.write_text(json.dumps([]))
    with pytest.raises(SeedError):
        env.run()


def test_the_gateway_process_refuses_to_start_on_a_bad_seed(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"servers": [entry(credential_env="MISSING_VAR")]}))
    result = subprocess.run([sys.executable, "-m", "gateway"], capture_output=True, text=True, timeout=60, env={
        "GATEWAY_SECRET_KEY": KEY, "GATEWAY_DB_URL": f"sqlite:///{tmp_path / 'g.db'}",
        "GATEWAY_JOBS_DB_URL": f"sqlite:///{tmp_path / 'j.db'}", "GATEWAY_SEED_FILE": str(seed),
        "PATH": "/usr/bin:/bin"})
    assert result.returncode != 0
    assert "refusing to start" in result.stderr and "MISSING_VAR" in result.stderr


# -- gateway starts and restarts are audited -----------------------------------------------------------------------

def settings_for(tmp_path, seed=None):
    return Settings(secret_key=KEY, db_url=f"sqlite:///{tmp_path / 'gw.db'}",
                    jobs_db_url=f"sqlite:///{tmp_path / 'jobs.db'}", allow_loopback=True, seed_file=seed,
                    worker_interval=3600, sync_interval=3600)


async def test_every_gateway_start_is_audited_even_without_a_seed_file(tmp_path):
    for _ in range(2):
        gw = create_gateway(settings_for(tmp_path))
        await gw.manager.close()
    starts = AuditLog(f"sqlite:///{tmp_path / 'gw.db'}").events(action="gateway_started")
    assert len(starts) == 2 and all(s.actor == "system" and s.details == {"seed_file": False} for s in starts)


async def test_a_rotated_token_reaches_the_gateway_after_a_restart_and_every_start_is_on_record(tmp_path):
    upstream = FakeUpstream("woo", tools=("list_orders",), token=SECRET_1)
    await upstream.start()
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"servers": [entry(url=upstream.url, timeout=2)]}))
    settings = settings_for(tmp_path, seed)
    who = Caller("u", "u@example.com")
    try:
        gw1 = create_gateway(settings, env={"WOO_TOKEN": SECRET_1})  # first start
        assert [t.name for t in (await gw1.service.list_tools(who)).tools] == ["woo__list_orders"]
        await gw1.manager.close()

        gw_same = create_gateway(settings, env={"WOO_TOKEN": SECRET_1})  # plain restart, nothing changed
        await gw_same.manager.close()

        upstream.token = SECRET_2  # the shared token is rotated (new Secret, new env var)
        gw2 = create_gateway(settings, env={"WOO_TOKEN": SECRET_2})  # restart over the same database
        assert [t.name for t in (await gw2.service.list_tools(who)).tools] == ["woo__list_orders"]
        assert (await gw2.service.call_tool(who, "woo__list_orders", {"text": "x"})).ok

        starts = gw2.audit.events(action="gateway_started")  # newest first
        assert [(s.details["created"], s.details["updated"], s.details["unchanged"]) for s in starts] == \
               [([], ["woo"], []), ([], [], ["woo"]), (["woo"], [], [])]
        server_events = gw2.audit.events(server_slug="woo")
        assert [e.action for e in server_events] == ["server_updated", "server_unchanged", "server_created"]
        assert server_events[0].details["changed"] == ["credential"]
        await gw2.manager.close()
    finally:
        await upstream.stop()


# -- admin events API and CLI -------------------------------------------------------------------------------------------

def test_events_are_newest_first_and_filterable(env):
    t0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    env.audit.record_event(actor="system:seed", action="server_created", server_slug="woo", details={"changed": []}, ts=t0)
    env.audit.record_event(actor="system:seed", action="server_updated", server_slug="woo", details={"changed": ["url"]}, ts=t0 + timedelta(minutes=1))
    env.audit.record_event(actor="system:seed", action="server_created", server_slug="docs", ts=t0 + timedelta(minutes=2))
    assert [e.server_slug for e in env.audit.events()] == ["docs", "woo", "woo"]
    assert [e.action for e in env.audit.events(server_slug="woo")] == ["server_updated", "server_created"]
    assert len(env.audit.events(action="server_created")) == 2
    assert len(env.audit.events(since=t0 + timedelta(minutes=1))) == 2
    assert len(env.audit.events(limit=1)) == 1


async def test_the_events_cli_command_shows_starts_and_what_changed(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"servers": [entry()]}))
    settings = settings_for(tmp_path, seed)
    for token in (SECRET_1, SECRET_1, SECRET_2):  # create, plain restart, rotation
        gw = create_gateway(settings, env={"WOO_TOKEN": token})
        await gw.manager.close()
    out = io.StringIO()
    assert cli_main(["events"], settings=settings, out=out, err=io.StringIO(), stdin=io.StringIO(), env={}) == 0
    text = out.getvalue()
    for word in ("gateway_started", "server_created", "server_unchanged", "server_updated", "system:seed", "credential"):
        assert word in text, word
    assert SECRET_1 not in text and SECRET_2 not in text
    out = io.StringIO()
    cli_main(["events", "--server", "nope"], settings=settings, out=out, err=io.StringIO(), stdin=io.StringIO(), env={})
    assert out.getvalue().strip() == "(none)"
