"""Date handling for "orders today" and date filters.

Day boundaries are computed in the STORE's time zone (a shop's "today" is not UTC's), then
sent to WooCommerce as explicit UTC instants. WordPress may read a date without an offset as
UTC, so an explicit instant is unambiguous. Daylight-saving days (23 or 25 hours) work because
the end of the day is the next local midnight, not start + 24 h.
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_window(tz_name: str, now: datetime | None = None) -> tuple[str, str]:
    """(start, end) of the store's current local day as UTC instants; end is exclusive."""
    tz = ZoneInfo(tz_name)
    local_now = (now or datetime.now(timezone.utc)).astimezone(tz)
    start = datetime.combine(local_now.date(), time.min, tzinfo=tz)
    end = datetime.combine(local_now.date() + timedelta(days=1), time.min, tzinfo=tz)
    return _utc_iso(start), _utc_iso(end)


def to_utc_iso(value: str, tz_name: str) -> str:
    """Parse an ISO 8601 date or datetime. With no offset it is read in the store's time zone."""
    try:
        dt = datetime.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise ValueError(f"{value!r} is not an ISO 8601 date, e.g. 2026-10-19 or 2026-10-19T14:30:00+05:30") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return _utc_iso(dt)
