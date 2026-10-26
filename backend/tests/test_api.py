"""The dashboard API (/api), over real HTTP against a running gateway with two fake upstream servers."""

import json
import re

import httpx
import pytest
from fastmcp import Client

from gateway.api import COOKIE
from gateway.config import Settings
from gateway.registry.crypto import generate_key
from gateway.server import create_gateway
from tests.fixtures.fake_upstream import FakeUpstream
from tests.test_endpoint import Served, add_server

UP_SECRET = "UP-SECRET-TOKEN"


class World:
    pass


@pytest.fixture
async def world(tmp_path):
    w = World()
    w.settings = Settings(secret_key=generate_key(), db_url=f"sqlite:///{tmp_path / 'gw.db'}",
                          jobs_db_url=f"sqlite:///{tmp_path / 'jobs.db'}", allow_loopback=True, worker_interval=0.2)
    w.shop = FakeUpstream("shop", tools=("echo", "only_shop"), token=UP_SECRET)
    w.docs = FakeUpstream("docs", tools=("echo",))
    await w.shop.start()
    await w.docs.start()
    w.gw = create_gateway(w.settings)
    w.shop_v = add_server(w.gw, "shop", w.shop, token=UP_SECRET)
    w.docs_v = add_server(w.gw, "docs", w.docs)
    w.admin = w.gw.auth.create_user("admin@example.com", "admin")
    w.alice = w.gw.auth.create_user("alice@example.com")
    w.k_admin = w.gw.auth.issue_key(w.admin.id, "admin-key")
    w.k_alice = w.gw.auth.issue_key(w.alice.id, "alice-key")
    w.served = Served(w.gw)
    await w.served.start()
    w.base = w.served.base
    yield w
    await w.served.stop()
    await w.shop.stop()
    await w.docs.stop()


async def session(world, key) -> httpx.AsyncClient:
    """A browser-like client, logged in with `key` (the session cookie is kept by the client)."""
    h = httpx.AsyncClient(base_url=world.base)
    r = await h.post("/api/login", json={"api_key": key})
    assert r.status_code == 200, r.text
    return h


def everything_said(world, *responses) -> str:
    return "".join(r.text + json.dumps(dict(r.headers)) for r in responses)


# -- login ----------------------------------------------------------------------------------------------------

async def test_login_sets_a_safe_cookie_and_never_returns_the_key(world):
    async with httpx.AsyncClient(base_url=world.base) as h:
        r = await h.post("/api/login", json={"api_key": world.k_alice.key})
        me = await h.get("/api/me")
    assert r.status_code == 200 and me.json()["user"]["email"] == "alice@example.com"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api" in cookie
    assert world.k_alice.key not in r.text + me.text


@pytest.mark.parametrize("body", [{"api_key": "wmcp_12345678_" + "a" * 64}, {"api_key": "nonsense"}, {}, {"api_key": 5}])
async def test_a_bad_key_cannot_log_in_and_the_attempt_is_audited(world, body):
    async with httpx.AsyncClient(base_url=world.base) as h:
        r = await h.post("/api/login", json=body)
        me = await h.get("/api/me")
    assert r.status_code == 401 and r.json()["error"] == "invalid_key" and me.status_code == 401
    (row,) = world.gw.audit.query(outcome="auth_failed")
    looks_like_a_key = isinstance(body.get("api_key"), str) and body["api_key"].startswith("wmcp_")
    assert row.source == "api" and row.key_id == ("12345678" if looks_like_a_key else None)


async def test_logout_ends_the_session(world):
    h = await session(world, world.k_alice.key)
    cookie = h.cookies[COOKIE]
    assert (await h.post("/api/logout", json={})).status_code == 200
    async with httpx.AsyncClient(base_url=world.base, cookies={COOKIE: cookie}) as replay:
        assert (await replay.get("/api/me")).status_code == 401  # the old cookie is dead, not just forgotten
    await h.aclose()


async def test_revoking_the_login_key_or_deactivating_the_user_ends_the_session_at_once(world):
    h = await session(world, world.k_alice.key)
    assert (await h.get("/api/me")).status_code == 200
    world.gw.auth.revoke_key(world.k_alice.key_id)
    assert (await h.get("/api/me")).status_code == 401
    k2 = world.gw.auth.issue_key(world.alice.id, "second")
    h2 = await session(world, k2.key)
    world.gw.auth.set_active(world.alice.id, False)
    assert (await h2.get("/api/me")).status_code == 401
    await h.aclose()
    await h2.aclose()


async def test_an_expired_session_is_refused(world):
    from datetime import timedelta
    from gateway.auth.service import SESSION_TTL, _now
    token, _ = world.gw.auth.start_session(world.k_alice.key)
    assert world.gw.auth.session_user(token) is not None
    assert world.gw.auth.session_user(token, now=_now() + SESSION_TTL + timedelta(seconds=1)) is None


# -- who may do what ---------------------------------------------------------------------------------------------

def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x0", path)


async def test_every_route_except_login_needs_a_session(world):
    async with httpx.AsyncClient(base_url=world.base) as h:
        for method, path, access in world.gw.api_routes:
            r = await h.request(method, concrete(path), json={} if method in ("POST", "PATCH") else None)
            needs_login = r.status_code == 401 and r.json()["error"] == "unauthenticated"
            assert needs_login == (access != "public"), (method, path, r.status_code)


async def test_a_member_gets_403_on_every_admin_route(world):
    admin_routes = [(m, p) for m, p, a in world.gw.api_routes if a == "admin"]
    assert len(admin_routes) >= 9  # the list really comes from the app, so a new admin route is covered automatically
    h = await session(world, world.k_alice.key)
    for method, path in admin_routes:
        r = await h.request(method, concrete(path), json={} if method in ("POST", "PATCH") else None)
        assert r.status_code == 403 and r.json()["error"] == "forbidden", (method, path)
    await h.aclose()


@pytest.mark.parametrize("content_type", [None, "application/x-www-form-urlencoded", "text/plain"])
async def test_writes_without_a_json_body_are_refused(world, content_type):
    """What a cross-site HTML form would send. It must not create a key even with a valid session."""
    h = await session(world, world.k_alice.key)
    headers = {"content-type": content_type} if content_type else {}
    r = await h.post("/api/keys", content=b"name=evil", headers=headers)
    assert r.status_code == 415 and len(world.gw.auth.list_keys(world.alice.id)) == 1
    await h.aclose()


# -- servers --------------------------------------------------------------------------------------------------------

async def test_server_list_shows_live_status_and_tools_but_never_the_credential(world):
    admin = await session(world, world.k_admin.key)
    member = await session(world, world.k_alice.key)
    ra, rm = await admin.get("/api/servers"), await member.get("/api/servers")
    by_slug = {s["slug"]: s for s in ra.json()["servers"]}
    assert by_slug["shop"]["status"] == "connected" and by_slug["shop"]["has_credential"] is True
    assert [t["name"] for t in by_slug["shop"]["tools"]] == ["shop__echo", "shop__only_shop"]
    member_view = rm.json()["servers"][0]
    assert "url" not in member_view and "has_credential" not in member_view and member_view["tools"]
    assert UP_SECRET not in everything_said(world, ra, rm)
    await world.docs.stop()  # a dead upstream: shown unreachable, the other still connected
    by_slug = {s["slug"]: s for s in (await admin.get("/api/servers")).json()["servers"]}
    assert by_slug["docs"]["status"] == "unreachable" and by_slug["docs"]["tools"] == []
    assert by_slug["docs"]["error"]["kind"] == "unreachable" and by_slug["shop"]["status"] == "connected"
    await world.docs.start()
    await admin.aclose()
    await member.aclose()


async def mcp_tools(world, key) -> list[str]:
    async with Client(world.served.url, auth=key) as c:
        return sorted(t.name for t in await c.list_tools())


async def test_admin_changes_take_effect_for_mcp_clients_immediately(world):
    admin = await session(world, world.k_admin.key)
    extra = FakeUpstream("extra", tools=("ping",), token="extra-secret")
    await extra.start()
    r = await admin.post("/api/servers", json={"slug": "extra", "url": extra.url, "credential_type": "bearer",
                                               "credential": "extra-secret", "timeout": 2})
    assert r.status_code == 201 and "extra-secret" not in r.text
    sid = r.json()["server"]["id"]
    assert "extra__ping" in await mcp_tools(world, world.k_alice.key)  # no waiting for the background sync

    assert (await admin.post(f"/api/servers/{sid}/disable", json={})).status_code == 200
    assert "extra__ping" not in await mcp_tools(world, world.k_alice.key)
    assert (await admin.post(f"/api/servers/{sid}/enable", json={})).status_code == 200
    assert "extra__ping" in await mcp_tools(world, world.k_alice.key)

    r = await admin.patch(f"/api/servers/{sid}", json={"slug": "extra2", "name": "Renamed"})
    assert r.status_code == 200 and "extra2__ping" in await mcp_tools(world, world.k_alice.key)
    assert (await admin.delete(f"/api/servers/{sid}")).status_code == 200
    assert not any(n.startswith("extra") for n in await mcp_tools(world, world.k_alice.key))

    mine = [e for e in world.gw.audit.events() if e.action != "gateway_started"]  # that one is the gateway's own
    assert {e.action for e in mine} == {"server_created", "server_disabled", "server_enabled", "server_updated",
                                        "server_removed"}
    assert all(e.actor == "admin@example.com" for e in mine)
    assert "extra-secret" not in json.dumps([e.details for e in world.gw.audit.events()])
    await extra.stop()
    await admin.aclose()


async def test_editing_a_credential_records_that_it_changed_but_not_the_value(world):
    admin = await session(world, world.k_admin.key)
    r = await admin.patch(f"/api/servers/{world.shop_v.id}", json={"credential": "new-secret-value"})
    assert r.status_code == 200
    (e,) = world.gw.audit.events(action="server_updated")
    assert e.details == {"changed": ["credential"]} and "new-secret-value" not in json.dumps(e.details)
    await admin.aclose()


@pytest.mark.parametrize("body, status, kind", [
    ({"slug": "Bad Slug", "url": "http://127.0.0.1:1/mcp"}, 400, "invalid"),
    ({"slug": "meta", "url": "http://169.254.169.254/latest"}, 400, "invalid"),  # SSRF guard: cloud metadata
    ({"slug": "shop", "url": "http://127.0.0.1:1/mcp"}, 409, "conflict"),
    ({"slug": "x", "url": "http://127.0.0.1:1/mcp", "credential_type": "bearer"}, 400, "invalid"),
    ({"slug": "x", "url": "http://127.0.0.1:1/mcp", "password": "p"}, 400, "invalid"),
    ({"url": "http://127.0.0.1:1/mcp"}, 400, "invalid"),
])
async def test_bad_server_input_gets_a_clear_error(world, body, status, kind):
    admin = await session(world, world.k_admin.key)
    r = await admin.post("/api/servers", json=body)
    assert (r.status_code, r.json()["error"]) == (status, kind), r.text
    await admin.aclose()


async def test_unknown_ids_are_404(world):
    admin = await session(world, world.k_admin.key)
    for method, path in [("PATCH", "/api/servers/nope"), ("DELETE", "/api/servers/nope"),
                         ("POST", "/api/servers/nope/disable"), ("POST", "/api/users/nope/deactivate"),
                         ("DELETE", "/api/keys/nope")]:
        r = await admin.request(method, path, json={} if method != "DELETE" else None)
        assert r.status_code == 404, (method, path, r.text)
    await admin.aclose()


# -- users and keys -------------------------------------------------------------------------------------------------

async def test_invite_creates_a_member_whose_first_key_works_once_shown(world):
    admin = await session(world, world.k_admin.key)
    r = await admin.post("/api/users", json={"email": "Bob@Example.com"})
    assert r.status_code == 201 and r.json()["user"]["role"] == "member"
    key = r.json()["api_key"]
    assert sorted(await mcp_tools(world, key)) == ["docs__echo", "shop__echo", "shop__only_shop"]
    users = await admin.get("/api/users")
    assert key not in users.text and "bob@example.com" in users.text
    assert (await admin.post("/api/users", json={"email": "bob@example.com"})).status_code == 409
    assert (await admin.post("/api/users", json={"email": "c@example.com", "role": "owner"})).status_code == 400
    await admin.aclose()


async def test_a_member_generates_and_revokes_their_own_keys(world):
    h = await session(world, world.k_alice.key)
    r = await h.post("/api/keys", json={"name": "cursor"})
    assert r.status_code == 201
    new = r.json()
    assert await mcp_tools(world, new["api_key"])  # the new key works for MCP clients
    listed = await h.get("/api/keys")
    assert new["api_key"] not in listed.text and {k["name"] for k in listed.json()["keys"]} == {"alice-key", "cursor"}
    assert (await h.delete(f"/api/keys/{new['key_id']}")).status_code == 200
    with pytest.raises(Exception):
        await mcp_tools(world, new["api_key"])
    await h.aclose()


async def test_a_member_cannot_revoke_someone_elses_key_but_an_admin_can(world):
    member = await session(world, world.k_alice.key)
    r = await member.delete(f"/api/keys/{world.k_admin.key_id}")
    assert r.status_code == 404 and world.gw.auth.get_key(world.k_admin.key_id).revoked_at is None
    admin = await session(world, world.k_admin.key)
    assert (await admin.delete(f"/api/keys/{world.k_alice.key_id}")).status_code == 200
    assert (await member.get("/api/me")).status_code == 401  # alice logged in with that key
    await member.aclose()
    await admin.aclose()


async def test_an_admin_can_give_a_locked_out_member_a_new_key(world):
    """Seen on the HP: a member revoked the key they log in with and had no way back in."""
    admin = await session(world, world.k_admin.key)
    assert (await admin.delete(f"/api/keys/{world.k_alice.key_id}")).status_code == 200  # alice is now locked out
    r = await admin.post(f"/api/users/{world.alice.id}/keys", json={"name": "replacement"})
    assert r.status_code == 201 and r.json()["name"] == "replacement"
    fresh = r.json()["api_key"]
    member = await session(world, fresh)  # she can log in again ...
    assert (await member.get("/api/me")).json()["user"]["email"] == "alice@example.com"
    assert await mcp_tools(world, fresh)  # ... and use MCP clients
    listed = await admin.get("/api/users")
    assert fresh not in listed.text  # shown once, never listed
    (ev,) = [e for e in world.gw.audit.events() if e.action == "key_issued" and e.details.get("name") == "replacement"]
    assert ev.actor == "admin@example.com" and ev.details["email"] == "alice@example.com" and fresh not in str(ev.details)
    await member.aclose()
    await admin.aclose()


async def test_issuing_a_key_for_someone_needs_an_admin_and_an_active_user(world):
    member = await session(world, world.k_alice.key)
    assert (await member.post(f"/api/users/{world.admin.id}/keys", json={})).status_code == 403
    admin = await session(world, world.k_admin.key)
    assert (await admin.post("/api/users/nope/keys", json={})).status_code == 404
    assert (await admin.post(f"/api/users/{world.alice.id}/deactivate", json={})).status_code == 200
    assert (await admin.post(f"/api/users/{world.alice.id}/keys", json={})).status_code == 400
    assert (await admin.post(f"/api/users/{world.admin.id}/keys", json={"name": "x" * 121})).status_code == 400
    await member.aclose()
    await admin.aclose()


async def test_deactivate_and_reactivate_but_never_yourself(world):
    admin = await session(world, world.k_admin.key)
    assert (await admin.post(f"/api/users/{world.admin.id}/deactivate", json={})).status_code == 400
    assert (await admin.post(f"/api/users/{world.alice.id}/deactivate", json={})).status_code == 200
    with pytest.raises(Exception):
        await mcp_tools(world, world.k_alice.key)
    assert (await admin.post(f"/api/users/{world.alice.id}/activate", json={})).status_code == 200
    assert await mcp_tools(world, world.k_alice.key)
    await admin.aclose()


# -- audit ----------------------------------------------------------------------------------------------------------

async def test_admins_see_and_filter_all_calls_members_only_their_own(world):
    async with Client(world.served.url, auth=world.k_alice.key) as c:
        await c.call_tool("shop__echo", {"text": "a"})
    async with Client(world.served.url, auth=world.k_admin.key) as c:
        await c.call_tool("docs__echo", {"text": "b"})
    admin = await session(world, world.k_admin.key)
    member = await session(world, world.k_alice.key)
    everyone = (await admin.get("/api/audit")).json()
    assert everyone["total"] == 2 and {e["user"] for e in everyone["entries"]} == {"alice@example.com", "admin@example.com"}
    e = everyone["entries"][0]
    assert set(e) >= {"ts", "user", "server", "tool", "duration_ms", "outcome"}  # the audit log's columns
    assert (await admin.get("/api/audit", params={"user": "alice@example.com"})).json()["total"] == 1
    assert (await admin.get("/api/audit", params={"server": "docs"})).json()["entries"][0]["user"] == "admin@example.com"
    mine = (await member.get("/api/audit", params={"user": "admin@example.com"})).json()  # the filter is ignored
    assert mine["total"] == 1 and mine["entries"][0]["user"] == "alice@example.com"
    assert (await admin.get("/api/audit", params={"limit": "x"})).status_code == 400
    await admin.aclose()
    await member.aclose()


async def test_admin_events_list_who_changed_what(world):
    admin = await session(world, world.k_admin.key)
    await admin.post(f"/api/servers/{world.docs_v.id}/disable", json={})
    events = (await admin.get("/api/events")).json()["events"]
    assert any(e["action"] == "server_disabled" and e["server"] == "docs" and e["actor"] == "admin@example.com"
               for e in events)
    await admin.aclose()


async def test_no_response_ever_contains_a_secret(world):
    """Walk every GET route as an admin after a busy session and look for anything secret."""
    admin = await session(world, world.k_admin.key)
    invite = await admin.post("/api/users", json={"email": "z@example.com"})
    new_key = invite.json()["api_key"]
    session_token = admin.cookies[COOKIE]
    responses = [await admin.get(concrete(p)) for m, p, _ in world.gw.api_routes if m == "GET"]
    said = everything_said(world, *responses)
    for secret in (UP_SECRET, new_key, world.k_admin.key, world.k_alice.key, session_token,
                   world.settings.secret_key):
        assert secret not in said
    assert not re.search(r"[0-9a-f]{64}", said)  # no key hash (sha256 hex) anywhere
    await admin.aclose()
