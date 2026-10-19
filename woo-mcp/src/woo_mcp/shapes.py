"""What the tools return: small, structured, and free of customer data.

Orders deliberately carry NO customer name, email, phone, address or note: the questions this
server exists to answer (what came in, what is it worth, what is in it) do not need them, and
everything returned here may be shown to an LLM provider.
"""

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_TAGS = re.compile(r"<[^>]+>")


def strip_html(text: str | None, limit: int = 1000) -> str:
    return _TAGS.sub("", text or "").strip()[:limit]


def product_view(p: dict, detail: bool = False) -> dict:
    view = {"id": p["id"], "name": p.get("name", ""), "status": p.get("status"), "price": p.get("price"),
            "regular_price": p.get("regular_price"), "sku": p.get("sku") or None, "permalink": p.get("permalink")}
    if detail:
        view["description"] = strip_html(p.get("description"))
        view["stock_status"] = p.get("stock_status")
    return view


def _local(date_gmt: str | None, tz: ZoneInfo) -> str | None:
    """WooCommerce gives UTC as '2026-10-19T08:30:00' with no offset; show it in the store's zone."""
    if not date_gmt:
        return None
    return datetime.fromisoformat(date_gmt).replace(tzinfo=timezone.utc).astimezone(tz).isoformat()


def order_view(o: dict, tz_name: str) -> dict:
    tz = ZoneInfo(tz_name)
    items = [{"product_id": i.get("product_id"), "name": i.get("name"), "quantity": i.get("quantity"),
              "total": i.get("total")} for i in o.get("line_items", [])]
    return {"id": o["id"], "status": o.get("status"), "total": o.get("total"), "currency": o.get("currency"),
            "created_at": _local(o.get("date_created_gmt"), tz), "payment_method": o.get("payment_method_title"),
            "customer_id": o.get("customer_id"), "item_count": sum(i["quantity"] or 0 for i in items),
            "line_items": items}
