import asyncio
import socket

import httpx
import pytest
import uvicorn
from fastmcp import Client

from tests.fixtures import fake_woocommerce as fw
from tests.fixtures.fake_woocommerce import FakeWooCommerce, ServedFakeWoo
from woo_mcp.config import Settings
from woo_mcp.server import build_server

TOKEN = "gateway-shared-token-123"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Env:
    pass


@pytest.fixture
async def env():
    e = Env()
    e.fake = FakeWooCommerce()
    e.store = ServedFakeWoo(e.fake, free_port())
    await e.store.start()
    e.clock = Clock()
    e.settings = Settings(base_url=e.store.url, consumer_key=fw.KEY, consumer_secret=fw.SECRET,
                          server_token=TOKEN, timeout=2.0)
    e.mcp, e.client = build_server(e.settings, clock=e.clock)
    e.port = free_port()
    e.url = f"http://127.0.0.1:{e.port}/mcp"
    e.base = f"http://127.0.0.1:{e.port}"
    e.server = uvicorn.Server(uvicorn.Config(e.mcp.http_app(path="/mcp", stateless_http=True),
                                             host="127.0.0.1", port=e.port, log_level="error"))
    e.task = asyncio.create_task(e.server.serve())
    while not e.server.started:
        await asyncio.sleep(0.02)
    yield e
    e.server.should_exit = True
    await e.task
    await e.store.stop()


# -- the token -------------------------------------------------------------------------

async def test_no_token_or_wrong_token_is_a_401(env):
    async with httpx.AsyncClient() as h:
        assert (await h.post(env.url, json={})).status_code == 401
        headers = [f"Bearer {bad}" for bad in ("wrong", TOKEN + "x", TOKEN[:-1], TOKEN.upper())]
        headers += ["Bearer", TOKEN, f"Basic {TOKEN}", f"Token {TOKEN}"]  # right secret, wrong or missing scheme
        for value in headers:
            r = await h.post(env.url, json={}, headers={"Authorization": value})
            assert r.status_code == 401, value
    with pytest.raises(Exception):
        async with Client(env.url, auth="wrong") as c:
            await c.list_tools()


async def test_the_right_token_lists_the_tools_and_calls_work_end_to_end(env):
    async with Client(env.url, auth=TOKEN) as c:
        names = {t.name for t in await c.list_tools()}
        assert names == {"list_products", "get_product", "create_product", "delete_product",
                         "list_orders", "get_order", "update_order_status"}
        created = await c.call_tool("create_product", {"name": "Blue Mug", "regular_price": "499"})
        listing = await c.call_tool("list_products", {})
    assert created.structured_content["name"] == "Blue Mug"
    assert [p["name"] for p in listing.structured_content["items"]] == ["Blue Mug"]
    assert len(env.fake.products) == 1  # it really reached the store over HTTP, with the store's own keys


async def test_the_token_is_not_the_store_key_and_neither_leaks(env):
    async with httpx.AsyncClient() as h:
        r401 = await h.post(env.url, json={}, headers={"Authorization": "Bearer nope"})
    env.fake.down = True
    async with Client(env.url, auth=TOKEN) as c:
        failed = await c.call_tool("list_products", {}, raise_on_error=False)
        tools = await c.list_tools()
    blob = r401.text + failed.content[0].text + repr(tools)
    for secret in (TOKEN, fw.SECRET, fw.KEY, env.store.url):
        assert secret not in blob


async def test_unexpected_errors_are_masked(env):
    async def explode(**_):
        raise RuntimeError("secret internals at /srv/app with password hunter2")
    env.client.list_products = explode
    async with Client(env.url, auth=TOKEN) as c:
        r = await c.call_tool("list_products", {}, raise_on_error=False)
    assert r.is_error and "hunter2" not in r.content[0].text and "/srv/app" not in r.content[0].text


# -- health ----------------------------------------------------------------------------------

async def test_healthz_needs_no_token(env):
    async with httpx.AsyncClient() as h:
        r = await h.get(env.base + "/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_readyz_reflects_the_store_without_exposing_details(env):
    async with httpx.AsyncClient() as h:
        assert (await h.get(env.base + "/readyz")).json() == {"status": "ready"}
        env.fake.down = True
        env.clock.t += 6  # let the cached answer expire
        r = await h.get(env.base + "/readyz")
        assert r.status_code == 503 and r.json() == {"status": "store unavailable"}
        env.fake.down = False
        env.clock.t += 6
        assert (await h.get(env.base + "/readyz")).status_code == 200


async def test_readyz_is_cached_so_probes_do_not_hammer_the_store(env):
    async with httpx.AsyncClient() as h:
        for _ in range(5):
            await h.get(env.base + "/readyz")
        hits = len(env.fake.requests)
        env.clock.t += 6
        await h.get(env.base + "/readyz")
    assert hits == 1 and len(env.fake.requests) == 2  # one store call per 5-second window


async def test_readyz_fails_when_the_store_rejects_our_keys(env):
    bad = Settings(base_url=env.store.url, consumer_key="ck_wrong", consumer_secret="cs_wrong",
                   server_token=TOKEN, timeout=2.0)
    mcp, _ = build_server(bad)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(mcp.http_app(path="/mcp", stateless_http=True), host="127.0.0.1",
                                           port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        async with httpx.AsyncClient() as h:
            assert (await h.get(f"http://127.0.0.1:{port}/readyz")).status_code == 503
    finally:
        server.should_exit = True
        await task


# -- raw-socket cases: things a normal HTTP client library refuses to send ------------------------

async def raw_request(port: int, header_lines: list[bytes]) -> tuple[int, bytes]:
    """Send a hand-built HTTP/1.1 request and return (status code, body). No client library involved."""
    # A VALID MCP message: a request that gets past authentication answers 200, so a 400 can only come
    # from the HTTP layer rejecting a malformed header, never from the body.
    body = (b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26",'
            b'"capabilities":{},"clientInfo":{"name":"raw","version":"1"}}}')
    request = (b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
               b"Accept: application/json, text/event-stream\r\n"
               + b"".join(line + b"\r\n" for line in header_lines)
               + b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), payload


@pytest.mark.parametrize("header_lines", [
    [b"Authorization: Bearer "],                        # empty token (trailing space), the case httpx refuses to send
    [b"Authorization: Bearer   "],                      # whitespace-only token
    [b"Authorization: Bearer\t"],                       # tab instead of a token
    [b"Authorization: "],                               # header present but empty
    [],                                                 # header absent
    [b"Authorization: Bearer " + b"A" * 20_000],        # absurdly long token
    [b"Authorization: Bearer t\xc3\xb6ken-\xff\xfe"],   # non-ASCII bytes
    [b"Authorization: Bearer wrong", b"Authorization: Bearer " + TOKEN.encode()],  # duplicate headers, first one wrong
    [b"authorization: bearer " + TOKEN.encode()],       # lowercase scheme with the RIGHT token: see next test
], ids=["empty", "spaces", "tab", "blank-header", "absent", "huge", "non-ascii", "duplicate", "lowercase"])
async def test_malformed_authorization_headers_never_get_in_and_never_crash_the_server(env, header_lines):
    status, body = await raw_request(env.port, header_lines)
    if header_lines == [b"authorization: bearer " + TOKEN.encode()]:
        assert status in (200, 401)  # HTTP auth schemes are case-insensitive; either answer is fine, never a crash
    else:
        assert status in (400, 401), (status, body[:100])  # rejected; 200 would mean it got in
    assert TOKEN.encode() not in body
    # the server is still healthy afterwards
    async with httpx.AsyncClient() as h:
        assert (await h.get(env.base + "/healthz")).status_code == 200


async def test_a_good_request_still_works_over_the_raw_path(env):
    """Control for the tests above: the raw helper is not what makes the others fail."""
    status, _ = await raw_request(env.port, [b"Authorization: Bearer " + TOKEN.encode()])
    assert status == 200
