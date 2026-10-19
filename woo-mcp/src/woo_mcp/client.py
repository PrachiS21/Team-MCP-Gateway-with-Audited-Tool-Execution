"""The only code that talks to WooCommerce (REST API v3, consumer key + secret).

Rules:
- every request has a timeout;
- GETs are retried once on a network error or 502/503/504; create/update/delete never are
  (retrying a create could duplicate a product);
- every failure becomes a WooError with a safe message: no URLs, no credentials, no raw
  exception text. Store messages are stripped of HTML and shortened.
"""

import re
from typing import Any

import httpx

API_PREFIX = "/wp-json/wc/v3"
MAX_PER_PAGE = 50
_RETRYABLE_STATUS = {502, 503, 504}
_TAGS = re.compile(r"<[^>]+>")


class WooError(Exception):
    """kind: auth | not_found | invalid | unavailable | unexpected"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


def _clean(text: Any, limit: int = 300) -> str:
    return _TAGS.sub("", str(text)).strip()[:limit]


def clamp_page(per_page: int, page: int) -> tuple[int, int]:
    return max(1, min(int(per_page), MAX_PER_PAGE)), max(1, int(page))


class WooClient:
    def __init__(self, base_url: str, consumer_key: str, consumer_secret: str, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + API_PREFIX,
            auth=(consumer_key, consumer_secret),
            # WooCommerce accepts key/secret Basic auth only over HTTPS. Inside the cluster the hop is plain
            # HTTP behind no proxy, so we declare https; WordPress is configured to trust this header.
            headers={"X-Forwarded-Proto": "https", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing ---------------------------------------------------------

    async def _request(self, method: str, path: str, *, params: dict | None = None, json: Any = None,
                       what: str = "resource") -> Any:
        attempts = 2 if method == "GET" else 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                resp = await self._http.request(method, path, params=_drop_none(params), json=json)
            except (httpx.TimeoutException, httpx.TransportError):
                if last:
                    raise WooError("unavailable", "The store is unavailable or did not answer in time.") from None
                continue
            if resp.status_code in _RETRYABLE_STATUS and not last:
                continue
            return self._handle(resp, what)
        raise WooError("unexpected", "The store request failed.")  # pragma: no cover

    def _handle(self, resp: httpx.Response, what: str) -> Any:
        code = resp.status_code
        if 200 <= code < 300:
            try:
                return resp.json()
            except ValueError:
                raise WooError("unexpected", "The store returned a response that is not JSON.") from None
        if code in (401, 403):
            raise WooError("auth", "The store rejected the gateway's credentials. Ask an admin to check the WooCommerce API keys.")
        if code == 404:
            raise WooError("not_found", f"{what} not found.")
        if 400 <= code < 500:
            raise WooError("invalid", _clean(_store_message(resp)) or "The store rejected the request.")
        raise WooError("unavailable", "The store is unavailable or returned an error.")

    # -- products ---------------------------------------------------------

    async def list_products(self, *, search: str | None = None, status: str | None = None,
                            per_page: int = 20, page: int = 1) -> list[dict]:
        per_page, page = clamp_page(per_page, page)
        return await self._request("GET", "/products", params={"search": search, "status": status,
                                                              "per_page": per_page, "page": page})

    async def get_product(self, product_id: int) -> dict:
        return await self._request("GET", f"/products/{int(product_id)}", what=f"Product {product_id}")

    async def create_product(self, payload: dict) -> dict:
        return await self._request("POST", "/products", json=payload)

    async def delete_product(self, product_id: int) -> dict:
        """Permanent delete. Idempotent: a product that is already gone counts as success."""
        try:
            await self._request("DELETE", f"/products/{int(product_id)}", params={"force": "true"},
                                what=f"Product {product_id}")
        except WooError as e:
            if e.kind == "not_found":
                return {"id": int(product_id), "deleted": True, "already_gone": True}
            raise
        return {"id": int(product_id), "deleted": True, "already_gone": False}

    # -- orders -----------------------------------------------------------

    async def list_orders(self, *, status: str | None = None, after: str | None = None, before: str | None = None,
                          per_page: int = 20, page: int = 1) -> list[dict]:
        per_page, page = clamp_page(per_page, page)
        return await self._request("GET", "/orders", params={"status": status, "after": after, "before": before,
                                                            "per_page": per_page, "page": page,
                                                            "orderby": "date", "order": "desc"})

    async def get_order(self, order_id: int) -> dict:
        return await self._request("GET", f"/orders/{int(order_id)}", what=f"Order {order_id}")

    async def update_order(self, order_id: int, payload: dict) -> dict:
        return await self._request("PUT", f"/orders/{int(order_id)}", json=payload, what=f"Order {order_id}")

    # -- health -----------------------------------------------------------

    async def ping(self) -> bool:
        """True if the store answers an authenticated request (used by /readyz)."""
        try:
            await self._request("GET", "/products", params={"per_page": 1})
            return True
        except WooError:
            return False


def _drop_none(params: dict | None) -> dict | None:
    return {k: v for k, v in params.items() if v is not None} if params else None


def _store_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return body.get("message", "") if isinstance(body, dict) else ""
    except ValueError:
        return ""
