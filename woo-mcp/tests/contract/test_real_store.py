"""Contract tests: our WooCommerce MCP code against a REAL WooCommerce store.

Skipped unless these are set, so they never run by accident:
    WOO_CONTRACT_URL     the store's public URL, exactly as in WP_URL (e.g. http://192.168.1.50:8080)
    WOO_CONTRACT_KEY     WC_CONSUMER_KEY from deploy/compose/.env
    WOO_CONTRACT_SECRET  WC_CONSUMER_SECRET from deploy/compose/.env
Run:  uv run pytest tests/contract -v -s     (-s shows the findings printed by the informational tests)

These settle the assumptions the fake store could not: key auth over HTTP, hostname redirects, the order
date filters and their time zone, and whether a created product really reaches the storefront.
They create products and orders and delete them again afterwards.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastmcp import Client, FastMCP

from woo_mcp.client import WooClient, WooError
from woo_mcp.dates import today_window
from woo_mcp.tools import register_tools

BASE = os.environ.get("WOO_CONTRACT_URL", "").rstrip("/")
KEY = os.environ.get("WOO_CONTRACT_KEY", "")
SECRET = os.environ.get("WOO_CONTRACT_SECRET", "")
TZ = "Asia/Kolkata"

pytestmark = pytest.mark.skipif(not (BASE and KEY and SECRET),
                                reason="set WOO_CONTRACT_URL, WOO_CONTRACT_KEY and WOO_CONTRACT_SECRET to run against a real store")

# Names chosen to appear nowhere else (an earlier version used "Contract", which is also in the product name).
BILLING = {"first_name": "Zqfirst", "last_name": "Zqlast", "email": "zq.person@example.com",
           "phone": "+91 90000 00000", "address_1": "1 Zq Street", "city": "Zqcity", "postcode": "411001",
           "country": "IN", "state": "MH"}
CUSTOMER_STRINGS = ["Zqfirst", "Zqlast", "zq.person@example.com", "90000 00000", "1 Zq Street", "Zqcity", "411001"]


def parse_gmt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    pass


@pytest.fixture
async def store():
    s = Store()
    s.client = WooClient(BASE, KEY, SECRET, timeout=30)
    s.mcp = FastMCP("contract")
    register_tools(s.mcp, s.client, TZ)
    s.products, s.orders = [], []
    yield s
    for oid in s.orders:
        try:
            await s.client._request("DELETE", f"/orders/{oid}", params={"force": "true"})
        except WooError:
            pass
    for pid in s.products:
        await s.client.delete_product(pid)
    await s.client.aclose()


async def tool(store, name, args=None):
    async with Client(store.mcp) as c:
        r = await c.call_tool(name, args or {}, raise_on_error=False)
    return (True, r.content[0].text) if r.is_error else (False, r.structured_content)


async def new_product(store, name="Contract Mug", price="499"):
    err, p = await tool(store, "create_product", {"name": name, "regular_price": price})
    assert not err, p
    store.products.append(p["id"])
    return p


async def new_order(store, product_id, qty=2, status="processing"):
    order = await store.client._request("POST", "/orders", json={
        "payment_method": "cod", "payment_method_title": "Cash on delivery", "status": status,
        "billing": BILLING, "shipping": BILLING, "line_items": [{"product_id": product_id, "quantity": qty}]})
    store.orders.append(order["id"])
    return order


# -- assumption 1: key authentication over plain HTTP -------------------------------------------------------

async def test_basic_auth_over_http_works_with_the_forwarded_proto_header(store):
    assert BASE.startswith("http://"), "this check is about plain HTTP; use an http:// store URL"
    assert await store.client.ping() is True


async def test_what_happens_without_the_forwarded_proto_header_is_recorded(store):
    """Informational: shows whether the header is really needed. Either answer is acceptable."""
    async with httpx.AsyncClient() as h:
        r = await h.get(f"{BASE}/wp-json/wc/v3/products", params={"per_page": 1}, auth=(KEY, SECRET))
    print(f"\n[finding] Basic auth over HTTP WITHOUT X-Forwarded-Proto -> HTTP {r.status_code}"
          f" ({'header not needed' if r.status_code == 200 else 'header IS needed'})")
    assert r.status_code in (200, 401)


async def test_wrong_keys_are_rejected_as_an_auth_error():
    bad = WooClient(BASE, KEY, "cs_wrong_secret_value", timeout=30)
    try:
        with pytest.raises(WooError) as e:
            await bad.list_products()
        assert e.value.kind == "auth"
    finally:
        await bad.aclose()


# -- assumption 2: calling the store by a different host name -------------------------------------------------

async def test_a_different_host_header_does_not_make_wordpress_redirect_rest_calls(store):
    """In the cluster the MCP server reaches WordPress as http://<service-name>, not the public URL."""
    async with httpx.AsyncClient(follow_redirects=False) as h:
        r = await h.get(f"{BASE}/wp-json/wc/v3/products", params={"per_page": 1}, auth=(KEY, SECRET),
                        headers={"X-Forwarded-Proto": "https", "Host": "wordpress.store-main.svc.cluster.local"})
    print(f"\n[finding] REST call with an in-cluster Host header -> HTTP {r.status_code}"
          f"{' redirect to ' + r.headers.get('location', '?') if r.is_redirect else ''}")
    assert r.status_code == 200


# -- assumption 4: a created product reaches the storefront ----------------------------------------------------

async def test_a_created_product_is_visible_on_the_storefront(store):
    p = await new_product(store, "Storefront Check Mug", "249")
    assert p["status"] == "publish" and p["permalink"].startswith(BASE)
    async with httpx.AsyncClient(follow_redirects=True) as h:
        page = await h.get(p["permalink"])
        assert page.status_code == 200 and "Storefront Check Mug" in page.text
        shop = await h.get(f"{BASE}/?s=Storefront+Check+Mug&post_type=product")
        assert shop.status_code == 200 and "Storefront Check Mug" in shop.text


async def test_product_roundtrip_and_idempotent_delete(store):
    p = await new_product(store, "Roundtrip Mug", "9.99")
    err, got = await tool(store, "get_product", {"product_id": p["id"]})
    assert not err and got["regular_price"] == "9.99"
    err, found = await tool(store, "list_products", {"search": "Roundtrip Mug"})
    assert [i["id"] for i in found["items"]] == [p["id"]]
    err, first = await tool(store, "delete_product", {"product_id": p["id"]})
    err2, second = await tool(store, "delete_product", {"product_id": p["id"]})
    assert not err and not err2 and first["already_gone"] is False and second["already_gone"] is True
    err, text = await tool(store, "get_product", {"product_id": p["id"]})
    assert err and "not found" in text


@pytest.mark.parametrize("price", ["499", "9.99", "0", "1234.5"])
async def test_price_formats_the_store_accepts(store, price):
    p = await new_product(store, f"Price {price}", price)
    assert float(p["regular_price"]) == float(price)


async def test_what_the_real_store_does_with_a_non_numeric_price_is_recorded(store):
    """Finding: the real store does NOT reject "abc" as a price, which is why our tool validates before sending.
    Bypasses our own validation on purpose; the product is tracked so it is deleted afterwards."""
    created = await store.client.create_product({"name": "Bad Price Check", "regular_price": "abc"})
    store.products.append(created["id"])
    print(f"\n[finding] store accepted regular_price='abc' and kept regular_price={created.get('regular_price')!r}")
    err, text = await tool(store, "create_product", {"name": "Never Created", "regular_price": "abc"})
    assert err and "regular_price" in text  # our own tool refuses it before the store is asked


# -- assumption 3: orders, time zone and date filters ---------------------------------------------------------------

async def test_the_stores_time_zone_is_ist(store):
    p = await new_product(store)
    order = await new_order(store, p["id"])
    local, gmt = datetime.fromisoformat(order["date_created"]), datetime.fromisoformat(order["date_created_gmt"])
    assert local - gmt == timedelta(hours=5, minutes=30), (order["date_created"], order["date_created_gmt"])


async def test_order_date_filters_treat_the_instants_we_send_as_utc(store):
    p = await new_product(store)
    order = await new_order(store, p["id"])
    created = parse_gmt(order["date_created_gmt"])
    minute = timedelta(minutes=1)

    async def ids(**kw):
        return [o["id"] for o in await store.client.list_orders(per_page=50, **kw)]

    assert order["id"] in await ids(after=z(created - minute))
    assert order["id"] not in await ids(after=z(created + minute))
    assert order["id"] in await ids(before=z(created + minute))
    assert order["id"] not in await ids(before=z(created - minute))
    assert order["id"] in await ids(after=z(created - minute), before=z(created + minute))
    # A +/-5.5 h mix-up between UTC and the store's local time would break one of the four lines above.


async def test_a_fresh_order_is_found_by_today_and_shown_in_ist(store):
    p = await new_product(store)
    order = await new_order(store, p["id"])
    err, res = await tool(store, "list_orders", {"today": True, "per_page": 50})
    assert not err and order["id"] in [i["id"] for i in res["items"]]
    mine = [i for i in res["items"] if i["id"] == order["id"]][0]
    assert mine["created_at"].endswith("+05:30")
    start, end = today_window(TZ)
    assert res["window"] == {"from": start, "to": end}


async def test_naive_dates_in_the_tool_are_read_in_ist(store):
    p = await new_product(store)
    order = await new_order(store, p["id"])
    today_ist = datetime.fromisoformat(order["date_created"]).date()
    err, res = await tool(store, "list_orders", {"created_after": str(today_ist), "created_before": str(today_ist + timedelta(days=1)), "per_page": 50})
    assert not err and order["id"] in [i["id"] for i in res["items"]]
    err, res = await tool(store, "list_orders", {"created_after": str(today_ist + timedelta(days=1)), "per_page": 50})
    assert not err and order["id"] not in [i["id"] for i in res["items"]]


async def test_update_order_status_roundtrip(store):
    p = await new_product(store)
    order = await new_order(store, p["id"], status="processing")
    err, done = await tool(store, "update_order_status", {"order_id": order["id"], "status": "completed"})
    assert not err and done["status"] == "completed"
    err, back = await tool(store, "update_order_status", {"order_id": order["id"], "status": "processing"})
    assert not err and back["status"] == "processing"
    err, text = await tool(store, "update_order_status", {"order_id": 999999999, "status": "completed"})
    assert err and "not found" in text


async def test_real_orders_contain_customer_data_but_our_tools_strip_it(store):
    p = await new_product(store)
    order = await new_order(store, p["id"])
    raw = json.dumps(await store.client.get_order(order["id"]))
    assert "zq.person@example.com" in raw  # the real order really has it ...
    _, one = await tool(store, "get_order", {"order_id": order["id"]})
    _, listing = await tool(store, "list_orders", {"today": True, "per_page": 50})
    shown = json.dumps([one, listing])
    for secret in CUSTOMER_STRINGS:
        assert secret not in shown, secret  # ... and none of it reaches the caller
    assert one["line_items"][0]["name"] == p["name"] and one["item_count"] == 2 and one["currency"] == "INR"


async def test_paging_and_the_page_size_cap_against_the_real_api(store):
    err, res = await tool(store, "list_products", {"per_page": 50})
    assert not err and res["count"] <= 50
    err, _ = await tool(store, "list_products", {"per_page": 100})  # above our cap: rejected before reaching the store
    assert err
