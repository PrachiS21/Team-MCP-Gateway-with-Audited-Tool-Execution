"""The MCP server: tools + a bearer-token check + health endpoints.

It is internal-only. Layers of defence: no Ingress, a NetworkPolicy that admits only the gateway
(in the Helm chart), and this token, so a pod that can reach the Service still cannot call a tool.
"""

import hmac
import time
from contextlib import asynccontextmanager
from typing import Callable

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.responses import JSONResponse

from woo_mcp.client import WooClient
from woo_mcp.config import Settings
from woo_mcp.tools import register_tools

READY_CACHE_SECONDS = 5.0


class StaticTokenVerifier(TokenVerifier):
    """Accepts exactly one bearer token (the one the gateway holds as its credential)."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._token):  # constant time
            return AccessToken(token="", client_id="gateway", scopes=[])  # the secret is not kept on the object
        return None


def build_server(settings: Settings, client: WooClient | None = None,
                 clock: Callable[[], float] = time.monotonic) -> tuple[FastMCP, WooClient]:
    client = client or WooClient(settings.base_url, settings.consumer_key, settings.consumer_secret,
                                 timeout=settings.timeout)

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield
        finally:
            await client.aclose()

    # mask_error_details: anything unexpected inside a tool reaches the caller as a generic message;
    # only our deliberate ToolError texts are shown.
    mcp = FastMCP("woocommerce", auth=StaticTokenVerifier(settings.server_token), lifespan=lifespan,
                  mask_error_details=True)
    register_tools(mcp, client, settings.timezone)

    ready = {"at": -READY_CACHE_SECONDS, "ok": False}

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):  # liveness: the process is up
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_request):  # readiness: the store answers with our keys (cached, so probes stay cheap)
        now = clock()
        if now - ready["at"] >= READY_CACHE_SECONDS:
            ready["ok"], ready["at"] = await client.ping(), now
        if ready["ok"]:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "store unavailable"}, status_code=503)

    return mcp, client


def create_app(settings: Settings):
    mcp, _ = build_server(settings)
    return mcp.http_app(path="/mcp", stateless_http=True)
