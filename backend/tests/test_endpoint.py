import asyncio
import socket

import httpx
import pytest
import uvicorn
from fastmcp import Client

from gateway.config import Settings
from gateway.registry.crypto import generate_key
from gateway.server import create_gateway
from tests.fixtures.fake_upstream import FakeUpstream

UP_SECRET = "UP-SECRET-TOKEN"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Served:
    """Run a gateway's HTTP app on a real port, like uvicorn would in the cluster."""

    def __init__(self, gw):
        self.gw, self.port = gw, free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.base = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(uvicorn.Config(gw.http_app(), host="127.0.0.1", port=self.port, log_level="error"))
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.02)

    async def stop(self):
        self._server.should_exit = True
        await self._task


def add_server(gw, slug, fake, token=None):
    v = gw.registry.create(name=slug, slug=slug, url=fake.url, timeout=2,
                           credential_type="bearer" if token else "none", credential=token)
    gw.manager.add(v.id, slug, gw.registry.upstream_config(v.id), enabled=v.enabled)
    return v


class World:
    pass


@pytest.fixture
async def world(tmp_path):
    w = World()
    w.settings = Settings(secret_key=generate_key(), db_url=f"sqlite:///{tmp_path / 'gw.db'}",
                          jobs_db_url=f"sqlite:///{tmp_path / 'jobs.db'}", allow_loopback=True, worker_interval=0.2)
    w.shop = FakeUpstream("shop", tools=("echo", "only_shop"), token=UP_SECRET)  # needs the GATEWAY'S credential
    w.docs = FakeUpstream("docs", tools=("echo",))  # same tool name as shop: a collision
    await w.shop.start()
    await w.docs.start()
    w.gw = create_gateway(w.settings)
    w.shop_v = add_server(w.gw, "shop", w.shop, token=UP_SECRET)
    w.docs_v = add_server(w.gw, "docs", w.docs)
    w.alice = w.gw.auth.create_user("alice@example.com")
    w.bob = w.gw.auth.create_user("bob@example.com")
    w.ka = w.gw.auth.issue_key(w.alice.id, "alice-laptop")
    w.kb = w.gw.auth.issue_key(w.bob.id, "bob-cursor")
    w.served = Served(w.gw)
    await w.served.start()
    w.url = w.served.url
    yield w
    await w.served.stop()
    await w.shop.stop()
    await w.docs.stop()


async def names(url, key):
    async with Client(url, auth=key) as c:
        return sorted(t.name for t in await c.list_tools())


# -- the happy path ------------------------------------------------------------

async def test_member_sees_every_enabled_servers_tools_with_collisions_resolved(world):
    assert await names(world.url, world.ka.key) == ["docs__echo", "shop__echo", "shop__only_shop"]


async def test_call_is_routed_and_audited_to_the_right_user(world):
    async with Client(world.url, auth=world.ka.key) as a, Client(world.url, auth=world.kb.key) as b:
        ra = await a.call_tool("shop__echo", {"text": "hi"})
        rb = await b.call_tool("docs__echo", {"text": "yo"})
    assert ra.data == "shop:echo:hi" and rb.data == "docs:echo:yo"
    rows = {r.user_email: r for r in world.gw.audit.query()}
    assert (rows["alice@example.com"].server_slug, rows["alice@example.com"].tool, rows["alice@example.com"].key_id) == \
           ("shop", "echo", world.ka.key_id)
    assert (rows["bob@example.com"].server_slug, rows["bob@example.com"].key_id) == ("docs", world.kb.key_id)
    assert all(r.outcome == "success" and r.source == "mcp" for r in rows.values())


# -- keys ----------------------------------------------------------------------

@pytest.mark.parametrize("token", [None, "garbage", "wmcp_12345678_" + "a" * 64])
async def test_missing_malformed_and_unknown_keys_are_rejected(world, token):
    with pytest.raises(Exception):
        async with Client(world.url, auth=token) as c:
            await c.list_tools()


async def test_a_rejected_request_is_a_real_401_and_is_audited_without_the_key(world):
    bad = "wmcp_12345678_" + "b" * 64
    async with httpx.AsyncClient() as h:
        r = await h.post(world.url, json={}, headers={"Authorization": f"Bearer {bad}"})
        none = await h.post(world.url, json={})
    assert r.status_code == 401 and none.status_code == 401
    # Only attempts that PRESENT a key are audited. A request with no key at all is rejected before
    # our verifier runs, and logging those would let anyone flood the audit table.
    rows = world.gw.audit.query(outcome="auth_failed")
    assert len(rows) == 1 and rows[0].key_id == "12345678" and rows[0].user_id is None
    assert bad not in repr(rows) and "b" * 64 not in repr(rows)  # the public id is stored, never the key


async def test_revoking_a_key_rejects_the_very_next_call_even_in_an_open_session(world):
    async with Client(world.url, auth=world.kb.key) as b:
        assert (await b.call_tool("docs__echo", {"text": "1"})).data == "docs:echo:1"
        world.gw.auth.revoke_key(world.kb.key_id)
        with pytest.raises(Exception):
            await b.call_tool("docs__echo", {"text": "2"})
    assert await names(world.url, world.ka.key)  # alice is unaffected


async def test_deactivated_user_is_rejected(world):
    world.gw.auth.set_active(world.bob.id, False)
    with pytest.raises(Exception):
        await names(world.url, world.kb.key)


# -- control and failure isolation ---------------------------------------------------

async def test_disabling_an_upstream_removes_its_tools_for_everyone(world):
    await world.gw.manager.disable(world.docs_v.id)
    assert await names(world.url, world.ka.key) == ["shop__echo", "shop__only_shop"]
    assert await names(world.url, world.kb.key) == ["shop__echo", "shop__only_shop"]
    async with Client(world.url, auth=world.ka.key) as a:
        r = await a.call_tool("docs__echo", {"text": "x"}, raise_on_error=False)
    assert r.is_error


async def test_a_dead_upstream_does_not_affect_the_others_and_leaks_nothing(world):
    await world.docs.stop()
    assert await names(world.url, world.ka.key) == ["shop__echo", "shop__only_shop"]
    async with Client(world.url, auth=world.ka.key) as a:
        ok = await a.call_tool("shop__echo", {"text": "alive"})
        dead = await a.call_tool("docs__echo", {"text": "x"}, raise_on_error=False)
    assert ok.data == "shop:echo:alive"
    assert dead.is_error and "127.0.0.1" not in dead.content[0].text and "http" not in dead.content[0].text
    states = {s.slug: s.status.value for s in world.gw.manager.servers()}
    assert states["docs"] == "unreachable"
    await world.docs.start()


async def test_unknown_tool_is_an_error_result_and_audited(world):
    async with Client(world.url, auth=world.ka.key) as a:
        r = await a.call_tool("nope__x", {}, raise_on_error=False)
    assert r.is_error
    assert world.gw.audit.query()[0].error_kind == "unknown_tool"


# -- credentials -----------------------------------------------------------------

async def test_gateway_uses_its_own_upstream_credential_never_the_clients_key(world):
    # `shop` accepts only UP_SECRET. The call works, so the gateway sent its stored credential,
    # not alice's key. And neither secret ever appears in anything the client receives.
    async with Client(world.url, auth=world.ka.key) as a:
        tools = await a.list_tools()
        res = await a.call_tool("shop__echo", {"text": "t"})
    seen = repr(tools) + repr(res)
    assert res.data == "shop:echo:t"
    assert UP_SECRET not in seen and world.ka.key not in seen


# -- operations ----------------------------------------------------------------------

async def test_health_endpoints_need_no_key(world):
    async with httpx.AsyncClient() as h:
        assert (await h.get(world.served.base + "/healthz")).json() == {"status": "ok"}
        assert (await h.get(world.served.base + "/readyz")).json() == {"status": "ready"}


async def test_two_stateless_replicas_accept_the_same_key_and_see_revocation(world):
    gw2 = create_gateway(world.settings)  # a second replica over the same databases
    s2 = Served(gw2)
    await s2.start()
    try:
        assert await names(world.url, world.ka.key) == await names(s2.url, world.ka.key)
        world.gw.auth.revoke_key(world.ka.key_id)  # revoked via replica 1 ...
        with pytest.raises(Exception):
            await names(s2.url, world.ka.key)  # ... rejected by replica 2 at once
    finally:
        await s2.stop()
