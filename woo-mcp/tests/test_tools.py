import json
from datetime import datetime, timezone

import pytest
from fastmcp import Client, FastMCP

from tests.fixtures import fake_woocommerce as fw
from tests.fixtures.fake_woocommerce import FakeWooCommerce
from woo_mcp.client import WooClient
from woo_mcp.tools import parse_price, register_tools

NOW = datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)  # 22:30 IST on 19 October
CUSTOMER_STRINGS = ["Asha", "Verma", "asha.verma@example.com", "98765", "MG Road", "Pune", "411001", "Leave at the door"]


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class Env:
    pass


@pytest.fixture
async def env():
    e = Env()
    e.fake = FakeWooCommerce()
    e.client = WooClient("http://fake-store.local", fw.KEY, fw.SECRET, timeout=2.0, transport=e.fake.transport())
    e.mcp = FastMCP("woo-test")
    register_tools(e.mcp, e.client, "Asia/Kolkata", now=lambda: NOW)
    async with Client(e.mcp) as c:
        e.c = c
        yield e
    await e.client.aclose()


async def call(env, name, args=None):
    """Return (is_error, structured data or error text)."""
    r = await env.c.call_tool(name, args or {}, raise_on_error=False)
    if r.is_error:
        return True, r.content[0].text
    return False, r.structured_content


def seed_orders(fake):
    return {
        "early": fake.add_order(utc(2026, 10, 18, 18, 31)),  # 00:01 IST on the 19th: today
        "late": fake.add_order(utc(2026, 10, 19, 12, 0), status="completed"),  # 17:30 IST: today
        "yesterday": fake.add_order(utc(2026, 10, 18, 18, 29)),  # 23:59 IST on the 18th: not today
        "midnight": fake.add_order(utc(2026, 10, 19, 18, 30)),  # 00:00 IST on the 20th: not today (end exclusive)
    }


# -- the tool list -------------------------------------------------------------------

async def test_exposes_exactly_the_planned_tools_with_safety_hints(env):
    tools = {t.name: t for t in await env.c.list_tools()}
    assert set(tools) == {"list_products", "get_product", "create_product", "delete_product",
                          "list_orders", "get_order", "update_order_status"}
    for name in ("list_products", "get_product", "list_orders", "get_order"):
        assert tools[name].annotations.read_only_hint is True
    assert tools["delete_product"].annotations.destructive_hint is True
    assert tools["delete_product"].annotations.idempotent_hint is True
    assert tools["update_order_status"].annotations.read_only_hint is False


# -- products ------------------------------------------------------------------------------

async def test_create_product_is_published_and_visible_in_the_store_and_list(env):
    err, p = await call(env, "create_product", {"name": "Blue Mug", "regular_price": 499, "description": "Ceramic", "sku": "MUG-1"})
    assert not err and p["name"] == "Blue Mug" and p["price"] == "499" and p["status"] == "publish"
    assert p["sku"] == "MUG-1" and p["description"] == "Ceramic" and p["id"] in env.fake.products
    err, listing = await call(env, "list_products", {"search": "mug"})
    assert [i["id"] for i in listing["items"]] == [p["id"]]


@pytest.mark.parametrize("given, sent", [(9.99, "9.99"), ("10", "10"), (5, "5"), ("0", "0"), (" 12.50 ", "12.50"), ("1234.5678", "1234.5678")])
async def test_prices_become_plain_strings(env, given, sent):
    assert parse_price(given) == sent
    err, p = await call(env, "create_product", {"name": "X", "regular_price": given})
    assert not err and p["regular_price"] == sent


@pytest.mark.parametrize("bad", ["abc", "-5", -1, "", "NaN", "Infinity", "1e3", "1.23456", "1,5", "₹5"])
async def test_bad_prices_are_rejected_before_reaching_the_store(env, bad):
    err, text = await call(env, "create_product", {"name": "X", "regular_price": bad})
    assert err and "regular_price" in text
    assert env.fake.products == {}
    assert ("POST", "/wp-json/wc/v3/products") not in env.fake.requests


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
async def test_bad_names_are_rejected(env, name):
    err, _ = await call(env, "create_product", {"name": name, "regular_price": "5"})
    assert err and env.fake.products == {}


async def test_draft_products_are_not_public_status(env):
    err, p = await call(env, "create_product", {"name": "Hidden", "regular_price": "5", "status": "draft"})
    assert not err and p["status"] == "draft"
    err, listing = await call(env, "list_products", {"status": "publish"})
    assert listing["items"] == []


async def test_get_product_and_not_found_message(env):
    prod = env.fake.add_product("Tea", "120")
    err, p = await call(env, "get_product", {"product_id": prod["id"]})
    assert not err and p["name"] == "Tea" and "stock_status" in p
    err, text = await call(env, "get_product", {"product_id": 999})
    assert err and text == "Product 999 not found."


async def test_list_products_paging(env):
    for i in range(5):
        env.fake.add_product(f"Item {i}")
    err, page = await call(env, "list_products", {"per_page": 2, "page": 1})
    assert page["count"] == 2 and page["has_more"] is True
    err, last = await call(env, "list_products", {"per_page": 2, "page": 3})
    assert last["count"] == 1 and last["has_more"] is False
    err, _ = await call(env, "list_products", {"per_page": 500})  # over the cap: rejected by the schema
    assert err


async def test_delete_product_is_idempotent(env):
    prod = env.fake.add_product("Gone soon")
    err, first = await call(env, "delete_product", {"product_id": prod["id"]})
    assert not err and first == {"id": prod["id"], "deleted": True, "already_gone": False}
    assert prod["id"] not in env.fake.products
    err, second = await call(env, "delete_product", {"product_id": prod["id"]})
    assert not err and second["already_gone"] is True


# -- orders -------------------------------------------------------------------------------------

async def test_today_means_the_store_day_in_ist_with_exact_boundaries(env):
    o = seed_orders(env.fake)
    err, res = await call(env, "list_orders", {"today": True})
    assert not err
    assert {i["id"] for i in res["items"]} == {o["early"]["id"], o["late"]["id"]}
    assert [i["id"] for i in res["items"]] == [o["late"]["id"], o["early"]["id"]]  # newest first
    assert res["store_timezone"] == "Asia/Kolkata"
    assert res["window"] == {"from": "2026-10-18T18:30:00Z", "to": "2026-10-19T18:30:00Z"}  # stated, so the model can say what 'today' meant


async def test_orders_are_shown_in_ist_not_utc(env):
    o = seed_orders(env.fake)
    err, order = await call(env, "get_order", {"order_id": o["early"]["id"]})
    assert order["created_at"] == "2026-10-19T00:01:00+05:30"


async def test_date_filters_without_offset_are_read_in_ist(env):
    o = seed_orders(env.fake)
    err, res = await call(env, "list_orders", {"created_after": "2026-10-19", "created_before": "2026-10-20"})
    assert {i["id"] for i in res["items"]} == {o["early"]["id"], o["late"]["id"]}
    err, res = await call(env, "list_orders", {"created_after": "2026-10-18T18:00:00Z"})  # explicit offset is respected
    assert len(res["items"]) == 4


async def test_status_filter(env):
    o = seed_orders(env.fake)
    err, res = await call(env, "list_orders", {"status": "completed"})
    assert [i["id"] for i in res["items"]] == [o["late"]["id"]]


@pytest.mark.parametrize("args, fragment", [
    ({"today": True, "created_after": "2026-10-19"}, "either today"),
    ({"created_after": "yesterday"}, "ISO 8601"),
    ({"created_after": "2026-10-20", "created_before": "2026-10-19"}, "earlier"),
    ({"status": "shipped"}, "status"),
])
async def test_bad_order_queries_are_rejected_with_a_helpful_message(env, args, fragment):
    err, text = await call(env, "list_orders", args)
    assert err and fragment in text


async def test_orders_contain_no_customer_data_anywhere(env):
    o = seed_orders(env.fake)
    _, listing = await call(env, "list_orders", {})
    _, single = await call(env, "get_order", {"order_id": o["early"]["id"]})
    _, updated = await call(env, "update_order_status", {"order_id": o["early"]["id"], "status": "completed"})
    text = json.dumps([listing, single, updated])
    for secret in CUSTOMER_STRINGS:
        assert secret not in text, secret
    assert single["line_items"] == [{"product_id": 10, "name": "Blue Mug", "quantity": 2, "total": "19.98"}]
    assert single["item_count"] == 2 and single["currency"] == "INR" and single["total"] == "19.98"


async def test_update_order_status_changes_the_store_and_returns_the_order(env):
    o = seed_orders(env.fake)
    err, res = await call(env, "update_order_status", {"order_id": o["early"]["id"], "status": "completed"})
    assert not err and res["status"] == "completed" and env.fake.orders[o["early"]["id"]]["status"] == "completed"


async def test_update_order_status_errors(env):
    o = seed_orders(env.fake)
    err, _ = await call(env, "update_order_status", {"order_id": o["early"]["id"], "status": "shipped"})
    assert err and env.fake.orders[o["early"]["id"]]["status"] == "processing"
    err, text = await call(env, "update_order_status", {"order_id": 9999, "status": "completed"})
    assert err and text == "Order 9999 not found."


# -- the store misbehaves ---------------------------------------------------------------------------

async def test_store_down_gives_a_clear_error_without_urls(env):
    env.fake.down = True
    err, text = await call(env, "list_products")
    assert err and "unavailable" in text and "fake-store" not in text and "http" not in text


async def test_wrong_store_keys_give_an_auth_message_and_leak_nothing(env):
    bad = WooClient("http://fake-store.local", "ck_wrong", "cs_wrong_secret", transport=env.fake.transport())
    mcp = FastMCP("bad")
    register_tools(mcp, bad, "Asia/Kolkata")
    async with Client(mcp) as c:
        r = await c.call_tool("list_products", {}, raise_on_error=False)
    assert r.is_error and "API keys" in r.content[0].text
    assert "cs_wrong_secret" not in r.content[0].text and "ck_wrong" not in r.content[0].text


async def test_the_forwarded_proto_header_really_matters_to_the_fake(env):
    """Guards the assumption behind X-Forwarded-Proto: without it the (realistic) store says no."""
    import httpx
    async with httpx.AsyncClient(transport=env.fake.transport(), base_url="http://x", auth=(fw.KEY, fw.SECRET)) as h:
        assert (await h.get("/wp-json/wc/v3/products")).status_code == 401
        assert (await h.get("/wp-json/wc/v3/products", headers={"X-Forwarded-Proto": "https"})).status_code == 200
