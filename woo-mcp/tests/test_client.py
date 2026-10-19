import base64

import httpx
import pytest

from woo_mcp.client import WooClient, WooError

KEY, SECRET = "ck_test_key", "cs_test_secret"
BASE = "http://wordpress.shop.svc.cluster.local"


class Store:
    """A scripted store: replies come from a queue; every request is recorded."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        return httpx.Response(status, json=body)


def client(store) -> WooClient:
    return WooClient(BASE, KEY, SECRET, timeout=1.0, transport=httpx.MockTransport(store))


# -- what we send ---------------------------------------------------------------

async def test_sends_basic_auth_forwarded_proto_and_the_right_path():
    store = Store((200, []))
    await client(store).list_products(search="shirt", status="publish")
    req = store.requests[0]
    assert req.url.path == "/wp-json/wc/v3/products"
    assert dict(req.url.params) == {"search": "shirt", "status": "publish", "per_page": "20", "page": "1"}
    assert req.headers["authorization"] == "Basic " + base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    assert req.headers["x-forwarded-proto"] == "https"


async def test_none_parameters_are_not_sent_and_paging_is_clamped():
    store = Store((200, []))
    c = client(store)
    await c.list_orders(per_page=10_000, page=-5)
    assert dict(store.requests[0].url.params) == {"per_page": "50", "page": "1", "orderby": "date", "order": "desc"}
    await c.list_orders(per_page=0)
    assert store.requests[1].url.params["per_page"] == "1"


async def test_order_date_filters_are_passed_through():
    store = Store((200, []))
    await client(store).list_orders(after="2026-10-19T00:00:00Z", before="2026-10-20T00:00:00Z", status="processing")
    p = store.requests[0].url.params
    assert (p["after"], p["before"], p["status"]) == ("2026-10-19T00:00:00Z", "2026-10-20T00:00:00Z", "processing")


async def test_write_requests_use_the_right_verbs_and_bodies():
    store = Store((200, {"id": 1}))
    c = client(store)
    await c.create_product({"name": "Mug", "regular_price": "9.99"})
    await c.update_order(7, {"status": "completed"})
    await c.delete_product(3)
    post, put, delete = store.requests
    assert (post.method, post.url.path) == ("POST", "/wp-json/wc/v3/products")
    assert post.content == b'{"name":"Mug","regular_price":"9.99"}'
    assert (put.method, put.url.path) == ("PUT", "/wp-json/wc/v3/orders/7")
    assert (delete.method, delete.url.path, delete.url.params["force"]) == ("DELETE", "/wp-json/wc/v3/products/3", "true")


# -- retries --------------------------------------------------------------------

async def test_get_is_retried_once_after_a_network_error_then_succeeds():
    store = Store(httpx.ConnectError("boom"), (200, [{"id": 1}]))
    assert await client(store).list_products() == [{"id": 1}]
    assert len(store.requests) == 2


async def test_get_is_retried_once_on_503_but_gives_up_after_that():
    store = Store((503, {}))
    with pytest.raises(WooError) as e:
        await client(store).get_order(1)
    assert e.value.kind == "unavailable" and len(store.requests) == 2


async def test_post_is_never_retried():
    store = Store(httpx.ReadTimeout("slow"), (200, {"id": 1}))
    with pytest.raises(WooError) as e:
        await client(store).create_product({"name": "Mug"})
    assert e.value.kind == "unavailable" and len(store.requests) == 1  # a retry could create a duplicate


async def test_put_and_delete_are_not_retried_either():
    store = Store((503, {}))
    c = client(store)
    for call in (lambda: c.update_order(1, {"status": "completed"}), lambda: c.delete_product(1)):
        with pytest.raises(WooError):
            await call()
    assert len(store.requests) == 2  # exactly one attempt each


# -- errors are safe and clear -------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
async def test_credential_rejection_is_an_auth_error(status):
    with pytest.raises(WooError) as e:
        await client(Store((status, {"message": "bad keys"}))).list_products()
    assert e.value.kind == "auth" and "API keys" in e.value.message


async def test_missing_resources_name_what_was_missing():
    with pytest.raises(WooError) as e:
        await client(Store((404, {}))).get_order(99)
    assert e.value.kind == "not_found" and e.value.message == "Order 99 not found."


async def test_validation_errors_pass_the_stores_message_without_html_and_shortened():
    html = {"code": "x", "message": "Invalid <b>price</b> <a href='http://x'>see docs</a>. " + "long " * 200}
    with pytest.raises(WooError) as e:
        await client(Store((400, html))).create_product({"name": "x", "regular_price": "abc"})
    assert e.value.kind == "invalid"
    assert "<" not in e.value.message and e.value.message.startswith("Invalid price see docs.")
    assert len(e.value.message) <= 300


async def test_non_json_success_and_5xx_are_unexpected_or_unavailable():
    def not_json(_):
        return httpx.Response(200, text="<html>maintenance</html>")
    with pytest.raises(WooError) as e:
        await WooClient(BASE, KEY, SECRET, transport=httpx.MockTransport(not_json)).get_product(1)
    assert e.value.kind == "unexpected"
    with pytest.raises(WooError) as e:
        await client(Store((500, {}))).get_product(1)
    assert e.value.kind == "unavailable"


async def test_errors_never_contain_urls_or_credentials():
    for reply in (httpx.ConnectError(f"cannot reach {BASE} with {KEY}:{SECRET}"), (401, {"message": SECRET}),
                  (500, {"message": f"{BASE} {SECRET}"}), (400, {"message": "plain"})):
        with pytest.raises(WooError) as e:
            await client(Store(reply)).get_product(1)
        text = e.value.message + repr(e.value)
        assert SECRET not in text and KEY not in text and "wordpress.shop" not in text


# -- idempotent delete and health -------------------------------------------------------

async def test_delete_product_is_idempotent():
    assert await client(Store((200, {"id": 3}))).delete_product(3) == {"id": 3, "deleted": True, "already_gone": False}
    assert await client(Store((404, {}))).delete_product(3) == {"id": 3, "deleted": True, "already_gone": True}
    with pytest.raises(WooError):  # other failures are still errors
        await client(Store((403, {}))).delete_product(3)


async def test_ping():
    assert await client(Store((200, []))).ping() is True
    assert await client(Store((401, {}))).ping() is False
    assert await client(Store(httpx.ConnectError("x"))).ping() is False
