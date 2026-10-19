"""The MCP tools. Each one validates its input, calls WooClient, and returns a trimmed result.
Any failure becomes a ToolError with a short, safe message that tells the caller what to fix."""

import functools
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Callable, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from woo_mcp.client import WooClient, WooError
from woo_mcp.dates import to_utc_iso, today_window
from woo_mcp.shapes import order_view, product_view

OrderStatus = Literal["pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"]
ProductStatus = Literal["publish", "draft", "private", "pending"]
PerPage = Annotated[int, Field(ge=1, le=50, description="Results per page (1-50).")]
Page = Annotated[int, Field(ge=1, description="Page number, starting at 1.")]
Id = Annotated[int, Field(ge=1)]

MAX_DECIMALS = 4


def parse_price(value: str | int | float) -> str:
    """A non-negative decimal as the plain string WooCommerce requires, e.g. 9.99 -> '9.99'."""
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        d = None
    if d is None or not d.is_finite() or d < 0 or d.as_tuple().exponent < -MAX_DECIMALS or "e" in str(value).lower():
        raise ValueError(f"regular_price must be a non-negative number with at most {MAX_DECIMALS} decimals, got {value!r}.")
    return format(d, "f")


def _guard(fn):
    """Turn store and validation problems into clean ToolErrors; never leak anything else."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except WooError as e:
            raise ToolError(e.message) from None
        except ValueError as e:
            raise ToolError(str(e)) from None
    return wrapper


def register_tools(mcp: FastMCP, client: WooClient, timezone_name: str,
                   now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
    read = {"readOnlyHint": True, "openWorldHint": False}

    @_guard
    async def list_products(search: Annotated[str | None, Field(description="Text to look for in product names.")] = None,
                            status: ProductStatus | None = None, per_page: PerPage = 20, page: Page = 1) -> dict:
        """List products in the store, newest first. Filter by name text or status."""
        items = await client.list_products(search=search, status=status, per_page=per_page, page=page)
        return {"items": [product_view(p) for p in items], "count": len(items), "page": page,
                "has_more": len(items) == per_page}

    @_guard
    async def get_product(product_id: Id) -> dict:
        """Get one product by its id, including its description."""
        return product_view(await client.get_product(product_id), detail=True)

    @_guard
    async def create_product(
        name: Annotated[str, Field(min_length=1, max_length=200, description="Product name.")],
        regular_price: Annotated[str | int | float, Field(description="Price as a number, e.g. 499 or 9.99.")],
        description: Annotated[str | None, Field(max_length=5000)] = None,
        status: ProductStatus = "publish",
        sku: Annotated[str | None, Field(max_length=100)] = None,
    ) -> dict:
        """Create a product. With the default status 'publish' it appears on the storefront at once."""
        if not name.strip():
            raise ValueError("name must not be blank.")
        payload = {"name": name.strip(), "regular_price": parse_price(regular_price), "status": status}
        if description:
            payload["description"] = description
        if sku:
            payload["sku"] = sku
        return product_view(await client.create_product(payload), detail=True)

    @_guard
    async def delete_product(product_id: Id) -> dict:
        """PERMANENTLY delete a product. Safe to repeat: deleting a product that is already gone succeeds.
        Used by the gateway to undo a product creation."""
        return await client.delete_product(product_id)

    @_guard
    async def list_orders(
        status: OrderStatus | None = None,
        today: Annotated[bool, Field(description="Only orders placed today in the store's time zone.")] = False,
        created_after: Annotated[str | None, Field(description="ISO 8601 date or datetime, inclusive. "
                                                   "Without an offset it is read in the store's time zone.")] = None,
        created_before: Annotated[str | None, Field(description="ISO 8601 date or datetime, exclusive.")] = None,
        per_page: PerPage = 20, page: Page = 1,
    ) -> dict:
        """List orders, newest first. Use today=true for 'orders today'; otherwise created_after/created_before.
        Orders contain no customer contact details."""
        if today and (created_after or created_before):
            raise ValueError("use either today=true or created_after/created_before, not both.")
        if today:
            after, before = today_window(timezone_name, now())
        else:
            after = to_utc_iso(created_after, timezone_name) if created_after else None
            before = to_utc_iso(created_before, timezone_name) if created_before else None
            if after and before and after >= before:
                raise ValueError("created_after must be earlier than created_before.")
        items = await client.list_orders(status=status, after=after, before=before, per_page=per_page, page=page)
        return {"items": [order_view(o, timezone_name) for o in items], "count": len(items), "page": page,
                "has_more": len(items) == per_page, "store_timezone": timezone_name,
                "window": {"from": after, "to": before} if (after or before) else None}

    @_guard
    async def get_order(order_id: Id) -> dict:
        """Get one order by id: status, total, time and items (no customer contact details)."""
        return order_view(await client.get_order(order_id), timezone_name)

    @_guard
    async def update_order_status(order_id: Id, status: OrderStatus) -> dict:
        """Change an order's status. Returns the updated order."""
        return order_view(await client.update_order(order_id, {"status": status}), timezone_name)

    for fn, hints in (
        (list_products, read), (get_product, read), (list_orders, read), (get_order, read),
        (create_product, {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}),
        (delete_product, {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}),
        (update_order_status, {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}),
    ):
        mcp.tool(fn, name=fn.__name__, annotations=hints)
