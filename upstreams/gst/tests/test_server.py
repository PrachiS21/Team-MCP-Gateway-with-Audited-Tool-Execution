import asyncio
import socket

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

from gst_mcp.server import build_server, create_app

TOKEN = "gateway-shared-token-xyz"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def call(name, args=None):
    async with Client(build_server(TOKEN, "MH")) as c:  # in-process: tool behaviour only
        r = await c.call_tool(name, args or {}, raise_on_error=False)
    return (True, r.content[0].text) if r.is_error else (False, r.structured_content)


async def test_rates_and_home_state():
    err, res = await call("gst_rates")
    assert not err and res == {"rates_percent": ["0", "5", "18", "40"], "store_state": "MH"}


async def test_totals_over_several_order_amounts_within_the_state():
    err, res = await call("calculate_gst", {"amounts": ["1180", 590.0]})  # defaults: 18%, prices include GST
    assert not err and res["supply"].startswith("intra-state")
    assert res["totals"] == {"taxable": "1500.00", "gst": "270.00", "cgst": "135.00", "sgst": "135.00",
                             "igst": "0.00", "total": "1770.00"}
    assert len(res["lines"]) == 2


async def test_a_customer_in_another_state_pays_igst_and_state_case_does_not_matter():
    _, other = await call("calculate_gst", {"amounts": ["1000"], "prices_include_gst": False, "customer_state": "ka"})
    _, same = await call("calculate_gst", {"amounts": ["1000"], "prices_include_gst": False, "customer_state": "mh"})
    assert other["totals"]["igst"] == "180.00" and other["totals"]["cgst"] == "0.00"
    assert same["totals"]["cgst"] == "90.00" and same["totals"]["igst"] == "0.00"


@pytest.mark.parametrize("args, says", [
    ({"amounts": ["abc"]}, "not a number"),
    ({"amounts": ["100"], "rate": 12}, "rate must be one of"),
    ({"amounts": []}, "amounts"),
    ({"amounts": ["100"], "customer_state": "Maharashtra"}, "customer_state"),
])
async def test_bad_input_gets_a_clear_error(args, says):
    err, text = await call("calculate_gst", args)
    assert err and says in text


# -- over real HTTP: the token check and the health endpoints ----------------------------------------------------

@pytest.fixture
async def url():
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(build_server(TOKEN).http_app(path="/mcp", stateless_http=True),
                                           host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


async def test_only_the_gateway_token_can_use_the_tools(url):
    async with Client(f"{url}/mcp", auth=BearerAuth(TOKEN)) as c:
        assert {t.name for t in await c.list_tools()} == {"gst_rates", "calculate_gst"}
    for headers in ({"Authorization": "Bearer wrong-token"}, {}):
        async with httpx.AsyncClient() as h:
            r = await h.post(f"{url}/mcp", headers={**headers, "Accept": "application/json, text/event-stream"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401


async def test_an_empty_bearer_token_is_rejected(url):
    """httpx refuses to send "Bearer " with nothing after it, so write the request by hand (as in woo-mcp's tests)."""
    host, port = url.removeprefix("http://").split(":")
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(b"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer \r\nContent-Type: application/json\r\n"
                 b"Accept: application/json, text/event-stream\r\nContent-Length: " + str(len(body)).encode() +
                 b"\r\nConnection: close\r\n\r\n" + body)
    await writer.drain()
    status = (await reader.readline()).decode()
    writer.close()
    assert " 401 " in status, status


async def test_health_endpoints_need_no_token(url):
    async with httpx.AsyncClient() as h:
        assert (await h.get(f"{url}/healthz")).status_code == 200
        assert (await h.get(f"{url}/readyz")).status_code == 200


def test_it_refuses_to_start_without_a_token_or_with_a_bad_state(monkeypatch):
    monkeypatch.delenv("MCP_SERVER_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        create_app()
    monkeypatch.setenv("MCP_SERVER_TOKEN", "t")
    monkeypatch.setenv("STORE_STATE", "Maharashtra")
    with pytest.raises(SystemExit):
        create_app()
    with pytest.raises(ValueError):
        build_server("")
