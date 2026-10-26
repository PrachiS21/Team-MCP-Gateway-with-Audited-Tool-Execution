"""GST calculator MCP server: tools, a bearer-token check and health endpoints (same pattern as woo-mcp).

It has no state and no outbound calls. In the cluster it is internal-only: a NetworkPolicy admits only the gateway,
and this token means a pod that reaches the Service still cannot call a tool.
"""

import hmac
import os
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import Field
from starlette.responses import JSONResponse

from decimal import Decimal

from gst_mcp.gst import RATES, GstError, calculate, money, parse_amount, parse_rate

STATE_CODE = Annotated[str, Field(pattern=r"^[A-Za-z]{2}$", description="Two-letter Indian state code as WooCommerce "
                                  "stores it, e.g. MH (Maharashtra), KA (Karnataka), DL (Delhi).")]


class StaticTokenVerifier(TokenVerifier):
    """Accepts exactly one bearer token (the one the gateway holds as its credential)."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._token):
            return AccessToken(token="", client_id="gateway", scopes=[])
        return None


def build_server(token: str, store_state: str = "MH") -> FastMCP:
    if not token:
        raise ValueError("a server token is required")
    store_state = store_state.upper()
    mcp = FastMCP("gst", auth=StaticTokenVerifier(token), mask_error_details=True)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def gst_rates() -> dict:
        """List the GST rate slabs this calculator accepts, and the store's home state."""
        return {"rates_percent": [str(r) for r in RATES], "store_state": store_state}

    @mcp.tool(annotations={"readOnlyHint": True})
    async def calculate_gst(
        amounts: Annotated[list[str | float], Field(min_length=1, max_length=200,
                                                    description="One or more amounts in INR, e.g. order totals.")],
        rate: Annotated[str | float, Field(description="GST rate in percent; see gst_rates.")] = 18,
        prices_include_gst: Annotated[bool, Field(description="True if the amounts already include GST "
                                                  "(the GST part is then extracted from them).")] = True,
        customer_state: STATE_CODE | None = None,
    ) -> dict:
        """Calculate Indian GST for amounts: CGST + SGST for a customer in the store's state, IGST for another state.

        If customer_state is not given, the sale is treated as within the store's state.
        """
        try:
            r = parse_rate(rate)
            values = [parse_amount(a) for a in amounts]
        except GstError as exc:
            raise ToolError(str(exc)) from None
        intra = customer_state is None or customer_state.upper() == store_state
        lines = [calculate(v, r, prices_include_gst=prices_include_gst, intra_state=intra) for v in values]
        total = {k: str(money(sum((getattr(line, k) for line in lines), start=Decimal("0.00"))))
                 for k in ("taxable", "gst", "cgst", "sgst", "igst", "total")}
        return {
            "rate_percent": str(r),
            "prices_include_gst": prices_include_gst,
            "supply": "intra-state (CGST + SGST)" if intra else "inter-state (IGST)",
            "store_state": store_state,
            "lines": [{k: str(getattr(line, k)) for k in ("amount", "taxable", "gst", "cgst", "sgst", "igst", "total")}
                      for line in lines],
            "totals": total,
        }

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_request):  # nothing to depend on: ready as soon as it runs
        return JSONResponse({"status": "ready"})

    return mcp


def create_app():
    token = os.environ.get("MCP_SERVER_TOKEN", "")
    if not token:
        raise SystemExit("missing required environment variable: MCP_SERVER_TOKEN")
    state = os.environ.get("STORE_STATE", "MH")
    if len(state) != 2 or not state.isalpha():
        raise SystemExit("STORE_STATE must be a two-letter state code, e.g. MH")
    return build_server(token, state).http_app(path="/mcp", stateless_http=True)
