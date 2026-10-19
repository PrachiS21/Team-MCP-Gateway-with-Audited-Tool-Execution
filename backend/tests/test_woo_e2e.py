"""End to end over real HTTP:  MCP client -> gateway -> WooCommerce MCP server -> (fake) WooCommerce store.

Every hop is a separate server on its own port; only the store is a fake. This follows the project's end-to-end demo flow."""

import asyncio
import json
import socket
from datetime import datetime, timezone

import pytest
import uvicorn
from fastmcp import Client

from tests import e2e_support
from tests.fixtures.fake_upstream import FakeUpstream
from tests.test_endpoint import Served

from gateway.config import Settings
from gateway.registry.crypto import generate_key
from gateway.server import create_gateway

fw = e2e_support.load_fake_woocommerce()
from woo_mcp.config import Settings as WooSettings  # noqa: E402  (importable once e2e_support put woo-mcp on the path)
from woo_mcp.server import build_server  # noqa: E402

MCP_TOKEN = "woo-mcp-shared-token-abc123"
CUSTOMER_STRINGS = ["Asha", "Verma", "asha.verma@example.com", "98765", "MG Road", "Pune", "411001", "Leave at the door"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WooMcp:
    """The real woo-mcp app, served on a port."""

    def __init__(self, store_url: str, token: str = MCP_TOKEN):
        settings = WooSettings(base_url=store_url, consumer_key=fw.KEY, consumer_secret=fw.SECRET,
                               server_token=token, timeout=2.0)
        self.mcp, _ = build_server(settings)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._server = uvicorn.Server(uvicorn.Config(self.mcp.http_app(path="/mcp", stateless_http=True),
                                                     host="127.0.0.1", port=self.port, log_level="error"))
        self._task = None
        self.running = False

    async def start(self):
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.02)
        self.running = True

    async def stop(self):
        if self.running:
            self._server.should_exit = True
            await self._task
            self.running = False


class World:
    pass


@pytest.fixture
async def world(tmp_path):
    w = World()
    w.fake = fw.FakeWooCommerce()
    w.store = fw.ServedFakeWoo(w.fake, free_port())
    await w.store.start()
    w.woo = WooMcp(w.store.url)
    await w.woo.start()
    w.docs = FakeUpstream("docs", tools=("echo",))
    await w.docs.start()

    # worker/sync intervals are huge on purpose: the tests drive the undo worker by hand
    settings = Settings(secret_key=generate_key(), db_url=f"sqlite:///{tmp_path / 'gw.db'}",
                        jobs_db_url=f"sqlite:///{tmp_path / 'jobs.db'}", allow_loopback=True,
                        worker_interval=3600, sync_interval=3600)
    w.gw = create_gateway(settings)
    # how an admin would register it: URL + the shared token as a bearer credential (kept server-side only)
    v = w.gw.registry.create(name="WooCommerce", slug="woo", url=w.woo.url, credential_type="bearer",
                             credential=MCP_TOKEN, timeout=2)
    w.gw.manager.add(v.id, "woo", w.gw.registry.upstream_config(v.id))
    w.woo_id = v.id
    w.alice = w.gw.auth.create_user("alice@example.com")
    w.bob = w.gw.auth.create_user("bob@example.com")
    w.ka = w.gw.auth.issue_key(w.alice.id)
    w.kb = w.gw.auth.issue_key(w.bob.id)
    w.served = Served(w.gw)
    await w.served.start()
    w.url = w.served.url
    yield w
    await w.served.stop()
    await w.woo.stop()
    await w.docs.stop()
    await w.store.stop()


async def call(world, key, tool, args=None):
    """Return (is_error, structured data or error text) for a call made through the gateway."""
    async with Client(world.url, auth=key) as c:
        r = await c.call_tool(tool, args or {}, raise_on_error=False)
    return (True, r.content[0].text) if r.is_error else (False, r.structured_content)


def break_audit(world):
    """Make the audit store fail on the final write, as if its database went away mid-call."""
    real = world.gw.audit.finish

    def broken(*a, **k):
        raise RuntimeError("audit database is down")
    world.gw.audit.finish = broken
    return lambda: setattr(world.gw.audit, "finish", real)


# -- the end-to-end demo flow ------------------------------------------------------------------------------

async def test_tools_come_through_the_gateway_and_the_internal_one_is_hidden(world):
    async with Client(world.url, auth=world.ka.key) as c:
        names = sorted(t.name for t in await c.list_tools())
    assert names == ["woo__create_product", "woo__get_order", "woo__get_product", "woo__list_orders",
                     "woo__list_products", "woo__update_order_status"]  # no woo__delete_product


async def test_user_a_creates_a_product_and_user_b_asks_about_todays_orders(world):
    err, created = await call(world, world.ka.key, "woo__create_product", {"name": "Blue Mug", "regular_price": 499})
    assert not err and created["name"] == "Blue Mug" and created["status"] == "publish"
    assert [p["name"] for p in world.fake.products.values()] == ["Blue Mug"]  # it is really in the store

    world.fake.add_order(datetime.now(timezone.utc), items=[("Blue Mug", 2, "499")])  # "placed by hand on the storefront"
    err, orders = await call(world, world.kb.key, "woo__list_orders", {"today": True})
    assert not err and orders["count"] == 1 and orders["items"][0]["line_items"][0]["name"] == "Blue Mug"
    assert orders["store_timezone"] == "Asia/Kolkata"

    rows = {r.user_email: r for r in world.gw.audit.query()}
    assert (rows["alice@example.com"].tool, rows["alice@example.com"].outcome) == ("create_product", "success")
    assert (rows["bob@example.com"].tool, rows["bob@example.com"].server_slug) == ("list_orders", "woo")


async def test_no_customer_data_and_no_secret_ever_reaches_the_client(world):
    o = world.fake.add_order(datetime.now(timezone.utc))
    _, listing = await call(world, world.kb.key, "woo__list_orders", {})
    _, single = await call(world, world.kb.key, "woo__get_order", {"order_id": o["id"]})
    async with Client(world.url, auth=world.kb.key) as c:
        listed = repr(await c.list_tools())
    blob = json.dumps([listing, single]) + listed
    for secret in CUSTOMER_STRINGS + [MCP_TOKEN, fw.SECRET, fw.KEY, world.woo.url, world.store.url, world.kb.key]:
        assert secret not in blob, secret


async def test_the_gateway_uses_the_shared_token_never_the_clients_key(world):
    # woo-mcp accepts only MCP_TOKEN. Calls succeed, so the gateway presented its stored credential.
    err, _ = await call(world, world.ka.key, "woo__list_products")
    assert not err


async def test_update_order_status_works_through_the_whole_chain(world):
    o = world.fake.add_order(datetime.now(timezone.utc))
    err, res = await call(world, world.ka.key, "woo__update_order_status", {"order_id": o["id"], "status": "completed"})
    assert not err and res["status"] == "completed" and world.fake.orders[o["id"]]["status"] == "completed"


# -- internal tool ------------------------------------------------------------------------------------------

async def test_a_client_cannot_call_delete_product(world):
    p = world.fake.add_product("Keep me")
    err, text = await call(world, world.ka.key, "woo__delete_product", {"product_id": p["id"]})
    assert err and text == "No such tool."
    assert p["id"] in world.fake.products  # the store was never touched
    row = world.gw.audit.query()[0]
    assert (row.outcome, row.error_kind, row.tool) == ("denied", "internal_tool", "delete_product")


# -- the audit store fails after a changing call: undo ----------------------------------------------------------

async def test_a_created_product_is_deleted_again_when_the_audit_write_fails(world):
    restore = break_audit(world)
    err, text = await call(world, world.ka.key, "woo__create_product", {"name": "Ghost Mug", "regular_price": "10"})
    assert err and "being reversed automatically" in text
    assert len(world.fake.products) == 1  # not undone on the request path

    await world.gw.worker.run_once()  # the undo worker uses the hidden delete_product
    assert world.fake.products == {}

    restore()
    await world.gw.worker.run_once()  # audit store is back: the row is closed
    row = world.gw.audit.query(user_id=world.alice.id)[0]
    assert (row.outcome, row.error_kind) == ("error", "reversed")


async def test_a_status_change_is_put_back_to_the_previous_status(world):
    o = world.fake.add_order(datetime.now(timezone.utc), status="processing")
    restore = break_audit(world)
    err, text = await call(world, world.ka.key, "woo__update_order_status", {"order_id": o["id"], "status": "completed"})
    assert err and world.fake.orders[o["id"]]["status"] == "completed"
    await world.gw.worker.run_once()
    assert world.fake.orders[o["id"]]["status"] == "processing"  # restored from the pre-read, not guessed
    restore()


async def test_if_someone_else_changed_the_order_the_undo_is_skipped(world):
    o = world.fake.add_order(datetime.now(timezone.utc), status="processing")
    restore = break_audit(world)
    await call(world, world.ka.key, "woo__update_order_status", {"order_id": o["id"], "status": "completed"})
    world.fake.orders[o["id"]]["status"] = "cancelled"  # a shop admin edits it in WordPress meanwhile
    await world.gw.worker.run_once()
    assert world.fake.orders[o["id"]]["status"] == "cancelled"  # NOT overwritten
    assert world.gw.worker.store.list()[0].state == "conflict"
    restore()
    await world.gw.worker.run_once()
    assert world.gw.audit.query(user_id=world.alice.id)[0].error_kind == "reversal_conflict"


async def test_another_user_cannot_change_the_same_order_while_its_undo_is_pending(world):
    a = world.fake.add_order(datetime.now(timezone.utc), status="processing")
    b = world.fake.add_order(datetime.now(timezone.utc), status="processing")
    restore = break_audit(world)
    await call(world, world.ka.key, "woo__update_order_status", {"order_id": a["id"], "status": "completed"})
    restore()

    err, text = await call(world, world.kb.key, "woo__update_order_status", {"order_id": a["id"], "status": "cancelled"})
    assert err and "being reversed" in text and world.fake.orders[a["id"]]["status"] == "completed"
    err, _ = await call(world, world.kb.key, "woo__update_order_status", {"order_id": b["id"], "status": "completed"})
    assert not err  # a different order is not blocked

    await world.gw.worker.run_once()
    assert world.fake.orders[a["id"]]["status"] == "processing"  # back to the version before
    err, _ = await call(world, world.kb.key, "woo__update_order_status", {"order_id": a["id"], "status": "cancelled"})
    assert not err  # now Bob can build on the restored order


# -- control and failure isolation -------------------------------------------------------------------------------

async def test_killing_the_woo_server_leaves_the_others_working_and_shows_it_unreachable(world):
    v = world.gw.registry.create(name="Docs", slug="docs", url=world.docs.url, timeout=2)
    world.gw.manager.add(v.id, "docs", world.gw.registry.upstream_config(v.id))
    await world.woo.stop()  # "kill the pod"

    async with Client(world.url, auth=world.ka.key) as c:
        names = sorted(t.name for t in await c.list_tools())
    assert names == ["docs__echo"]
    err, text = await call(world, world.ka.key, "woo__list_products")
    assert err and "unreachable" in text and "127.0.0.1" not in text
    err, data = await call(world, world.ka.key, "docs__echo", {"text": "alive"})
    assert not err
    assert {s.slug: s.status.value for s in world.gw.manager.servers()} == {"woo": "unreachable", "docs": "connected"}


async def test_disabling_the_woo_server_removes_its_tools_for_everyone(world):
    await world.gw.manager.disable(world.woo_id)
    for key in (world.ka.key, world.kb.key):
        async with Client(world.url, auth=key) as c:
            assert await c.list_tools() == []


async def test_a_wrong_shared_token_shows_up_as_an_authentication_failure(world):
    world.gw.registry.update(world.woo_id, credential="not-the-token")
    await world.gw.manager.sync(world.gw.registry.desired())
    cat = await world.gw.service.list_tools(__import__("gateway.service", fromlist=["Caller"]).Caller("u", "u@x"))
    state = [s for s in cat.servers if s.slug == "woo"][0]
    assert cat.tools == [] and state.error.kind.value == "auth_failed" and state.status.value == "unreachable"
