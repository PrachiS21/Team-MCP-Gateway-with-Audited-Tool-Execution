"""A stateful fake of the WooCommerce REST API (v3), just the endpoints woo-mcp uses.

Realism that matters for our tests:
- Basic auth with a consumer key/secret, AND (like real WooCommerce on a non-SSL site) it only
  accepts that auth when the request says it came over HTTPS (X-Forwarded-Proto).
- Orders carry full customer details, so tests can prove the tools do not pass them on.
- Errors use WooCommerce's {"code", "message"} bodies; prices must be strings.
- ASSUMPTION to confirm against a real store: order `after`/`before` filters compare the order's
  UTC creation time against the UTC instants we send.
"""

import base64
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

KEY, SECRET = "ck_fake_key", "cs_fake_secret"
ORDER_STATUSES = {"pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"}
PRODUCT_STATUSES = {"publish", "draft", "private", "pending"}
_PRICE = re.compile(r"^\d+(\.\d+)?$")
_FMT = "%Y-%m-%dT%H:%M:%S"


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, "data": {"status": status}}, status_code=status)


class FakeWooCommerce:
    def __init__(self, store_url: str = "http://fake-store.local"):
        self.store_url = store_url
        self.products: dict[int, dict] = {}
        self.orders: dict[int, dict] = {}
        self._next_product = 10
        self._next_order = 100
        self.requests: list[tuple[str, str]] = []  # (method, path) of every request, for assertions
        self.down = False  # True: every request gets a 503
        self.fail_next: list[int] = []  # statuses to answer with, once each, before behaving normally
        self.require_https_header = True
        self.app = Starlette(routes=[
            Route("/wp-json/wc/v3/products", self._products, methods=["GET", "POST"]),
            Route("/wp-json/wc/v3/products/{pid:int}", self._product, methods=["GET", "DELETE"]),
            Route("/wp-json/wc/v3/orders", self._orders, methods=["GET"]),
            Route("/wp-json/wc/v3/orders/{oid:int}", self._order, methods=["GET", "PUT"]),
        ])

    # -- seeding helpers for tests --------------------------------------------

    def add_product(self, name: str, price: str = "10.00", status: str = "publish", sku: str = "") -> dict:
        return self._new_product({"name": name, "regular_price": price, "status": status, "sku": sku})

    def add_order(self, created_utc: datetime, status: str = "processing", items: list[tuple[str, int, str]] | None = None,
                  billing: dict | None = None) -> dict:
        oid, self._next_order = self._next_order, self._next_order + 1
        items = items or [("Blue Mug", 2, "9.99")]
        lines = [{"id": i + 1, "name": n, "product_id": 10 + i, "quantity": q, "total": f"{float(p) * q:.2f}",
                  "price": float(p)} for i, (n, q, p) in enumerate(items)]
        total = sum(float(line["total"]) for line in lines)
        utc = created_utc.astimezone(timezone.utc)
        local = utc + timedelta(hours=5, minutes=30)  # the fake store is "in IST", like our default
        order = {
            "id": oid, "status": status, "currency": "INR", "total": f"{total:.2f}",
            "date_created": local.strftime(_FMT), "date_created_gmt": utc.strftime(_FMT),
            "payment_method_title": "Cash on delivery", "customer_id": 0,
            "customer_note": "Leave at the door",
            "billing": billing or {"first_name": "Asha", "last_name": "Verma", "email": "asha.verma@example.com",
                                   "phone": "+91 98765 43210", "address_1": "12 MG Road", "city": "Pune",
                                   "postcode": "411001", "country": "IN"},
            "shipping": {"first_name": "Asha", "last_name": "Verma", "address_1": "12 MG Road", "city": "Pune"},
            "line_items": lines,
        }
        self.orders[oid] = order
        return order

    # -- plumbing ---------------------------------------------------------------

    def _gate(self, request: Request) -> JSONResponse | None:
        self.requests.append((request.method, request.url.path))
        if self.down:
            return _error(503, "service_unavailable", "Service Unavailable")
        if self.fail_next:
            status = self.fail_next.pop(0)
            return _error(status, "injected_failure", f"injected failure {status}")
        auth = request.headers.get("authorization", "")
        expected = "Basic " + base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
        if auth != expected or (self.require_https_header and request.headers.get("x-forwarded-proto") != "https"):
            return _error(401, "woocommerce_rest_cannot_view", "Sorry, you cannot list resources.")
        return None

    @staticmethod
    def _page(items: list[dict], request: Request) -> JSONResponse | list[dict]:
        try:
            per_page = int(request.query_params.get("per_page", 10))
            page = int(request.query_params.get("page", 1))
        except ValueError:
            return _error(400, "rest_invalid_param", "Invalid parameter(s): per_page")
        if not 1 <= per_page <= 100 or page < 1:
            return _error(400, "rest_invalid_param", "Invalid parameter(s): per_page")
        return items[(page - 1) * per_page: page * per_page]

    # -- products -----------------------------------------------------------------

    def _new_product(self, body: dict) -> dict:
        pid, self._next_product = self._next_product, self._next_product + 1
        price = body["regular_price"]
        product = {"id": pid, "name": body["name"], "slug": re.sub(r"[^a-z0-9]+", "-", body["name"].lower()).strip("-"),
                   "permalink": f"{self.store_url}/product/{pid}/", "status": body.get("status", "publish"),
                   "type": "simple", "sku": body.get("sku", ""), "price": price, "regular_price": price,
                   "description": f"<p>{body.get('description', '')}</p>" if body.get("description") else "",
                   "stock_status": "instock"}
        self.products[pid] = product
        return product

    async def _products(self, request: Request) -> JSONResponse:
        if (blocked := self._gate(request)) is not None:
            return blocked
        if request.method == "GET":
            q = request.query_params
            items = [p for p in self.products.values() if p["status"] != "trash"]
            if q.get("status"):
                items = [p for p in items if p["status"] == q["status"]]
            if q.get("search"):
                items = [p for p in items if q["search"].lower() in p["name"].lower()]
            page = self._page(items, request)
            return page if isinstance(page, JSONResponse) else JSONResponse(page)
        body: Any = await request.json()
        if not isinstance(body, dict) or not str(body.get("name", "")).strip():
            return _error(400, "woocommerce_rest_invalid_product", "Product name is required.")
        if not isinstance(body.get("regular_price"), str) or not _PRICE.match(body["regular_price"]):
            return _error(400, "rest_invalid_param", "Invalid parameter(s): regular_price")
        if body.get("status", "publish") not in PRODUCT_STATUSES:
            return _error(400, "rest_invalid_param", "Invalid parameter(s): status")
        return JSONResponse(self._new_product(body), status_code=201)

    async def _product(self, request: Request) -> JSONResponse:
        if (blocked := self._gate(request)) is not None:
            return blocked
        product = self.products.get(request.path_params["pid"])
        if product is None or product["status"] == "trash":
            return _error(404, "woocommerce_rest_product_invalid_id", "Invalid ID.")
        if request.method == "GET":
            return JSONResponse(product)
        if request.query_params.get("force") == "true":
            del self.products[product["id"]]
        else:
            product["status"] = "trash"
        return JSONResponse(product)

    # -- orders -----------------------------------------------------------------------

    @staticmethod
    def _utc(value: str) -> str:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).strftime(_FMT)

    async def _orders(self, request: Request) -> JSONResponse:
        if (blocked := self._gate(request)) is not None:
            return blocked
        q = request.query_params
        items = sorted(self.orders.values(), key=lambda o: o["date_created_gmt"], reverse=True)
        try:
            if q.get("after"):
                items = [o for o in items if o["date_created_gmt"] >= self._utc(q["after"])]
            if q.get("before"):
                items = [o for o in items if o["date_created_gmt"] < self._utc(q["before"])]
        except ValueError:
            return _error(400, "rest_invalid_param", "Invalid parameter(s): after, before")
        if q.get("status"):
            items = [o for o in items if o["status"] == q["status"]]
        page = self._page(items, request)
        return page if isinstance(page, JSONResponse) else JSONResponse(page)

    async def _order(self, request: Request) -> JSONResponse:
        if (blocked := self._gate(request)) is not None:
            return blocked
        order = self.orders.get(request.path_params["oid"])
        if order is None:
            return _error(404, "woocommerce_rest_shop_order_invalid_id", "Invalid ID.")
        if request.method == "PUT":
            body: Any = await request.json()
            if body.get("status") not in ORDER_STATUSES:
                return _error(400, "rest_invalid_param", "Invalid parameter(s): status")
            order["status"] = body["status"]
        return JSONResponse(order)

    # -- ways to use it -----------------------------------------------------------------

    def transport(self) -> httpx.ASGITransport:
        """In-process: hand this to WooClient(transport=...)."""
        return httpx.ASGITransport(app=self.app)


class ServedFakeWoo:
    """The fake on a real local port, for end-to-end tests through the gateway."""

    def __init__(self, fake: FakeWooCommerce, port: int):
        import asyncio
        self.fake, self.port, self._asyncio = fake, port, asyncio
        self.url = f"http://127.0.0.1:{port}"
        self._server = uvicorn.Server(uvicorn.Config(fake.app, host="127.0.0.1", port=port, log_level="error"))
        self._task = None

    async def start(self) -> None:
        self._task = self._asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                self._task.result()
            await self._asyncio.sleep(0.02)

    async def stop(self) -> None:
        self._server.should_exit = True
        await self._task
