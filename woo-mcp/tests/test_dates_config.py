from datetime import datetime, timezone

import pytest

from woo_mcp.config import Settings
from woo_mcp.dates import to_utc_iso, today_window

UTC = timezone.utc


def at(*args):
    return datetime(*args, tzinfo=UTC)


# -- today_window ---------------------------------------------------------------------

def test_utc_day():
    assert today_window("UTC", at(2026, 10, 19, 15, 30)) == ("2026-10-19T00:00:00Z", "2026-10-20T00:00:00Z")


def test_store_day_is_in_the_store_time_zone_not_utc():
    # 20:00 UTC on the 19th is already 01:30 on the 20th in Kolkata (+05:30): the shop's "today" is the 20th.
    assert today_window("Asia/Kolkata", at(2026, 10, 19, 20, 0)) == ("2026-10-19T18:30:00Z", "2026-10-20T18:30:00Z")
    # 02:00 UTC on the 19th is still the 18th in New York (EDT, -04:00).
    assert today_window("America/New_York", at(2026, 10, 19, 2, 0)) == ("2026-10-18T04:00:00Z", "2026-10-19T04:00:00Z")


def test_daylight_saving_days_are_23_and_25_hours_long():
    # New York clocks go back on 2026-11-01 (25-hour day) and forward on 2026-03-08 (23-hour day).
    start, end = today_window("America/New_York", at(2026, 11, 1, 12, 0))
    assert (start, end) == ("2026-11-01T04:00:00Z", "2026-11-02T05:00:00Z")
    start, end = today_window("America/New_York", at(2026, 3, 8, 12, 0))
    assert (start, end) == ("2026-03-08T05:00:00Z", "2026-03-09T04:00:00Z")


def test_an_order_just_after_local_midnight_is_inside_the_window():
    start, end = today_window("Asia/Kolkata", at(2026, 10, 19, 18, 31))  # 00:01 on the 20th local
    assert start <= "2026-10-19T18:31:00Z" < end


# -- to_utc_iso -----------------------------------------------------------------------

def test_naive_values_are_read_in_the_store_zone_and_aware_values_are_kept():
    assert to_utc_iso("2026-10-19", "Asia/Kolkata") == "2026-10-18T18:30:00Z"
    assert to_utc_iso("2026-10-19T09:00:00", "UTC") == "2026-10-19T09:00:00Z"
    assert to_utc_iso("2026-10-19T09:00:00+02:00", "Asia/Kolkata") == "2026-10-19T07:00:00Z"
    assert to_utc_iso("2026-10-19T09:00:00Z", "Asia/Kolkata") == "2026-10-19T09:00:00Z"


@pytest.mark.parametrize("bad", ["yesterday", "19/10/2026", "", "2026-13-40"])
def test_unparseable_dates_get_a_helpful_error(bad):
    with pytest.raises(ValueError, match="ISO 8601"):
        to_utc_iso(bad, "UTC")


# -- config ----------------------------------------------------------------------------

ENV = {"WOO_BASE_URL": "http://wordpress.shop.svc/", "WOO_CONSUMER_KEY": "ck_1", "WOO_CONSUMER_SECRET": "cs_1",
       "MCP_SERVER_TOKEN": "tok_1"}


def test_defaults_and_trailing_slash():
    s = Settings.from_env(ENV)
    assert s.base_url == "http://wordpress.shop.svc" and s.timeout == 10.0 and s.port == 8000
    assert s.timezone == "Asia/Kolkata"  # IST by default


def test_secrets_are_not_in_repr():
    text = repr(Settings.from_env(ENV))
    assert "ck_1" not in text and "cs_1" not in text and "tok_1" not in text


def test_all_overrides():
    s = Settings.from_env({**ENV, "STORE_TIMEZONE": "Europe/London", "WOO_TIMEOUT": "3.5", "MCP_PORT": "9000", "MCP_HOST": "127.0.0.1"})
    assert (s.timezone, s.timeout, s.port, s.host) == ("Europe/London", 3.5, 9000, "127.0.0.1")


@pytest.mark.parametrize("missing", ["WOO_BASE_URL", "WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET", "MCP_SERVER_TOKEN"])
def test_each_required_variable_is_reported_by_name(missing):
    env = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(SystemExit, match=missing):
        Settings.from_env(env)


@pytest.mark.parametrize("override, message", [
    ({"WOO_BASE_URL": "wordpress.shop.svc"}, "http"),
    ({"STORE_TIMEZONE": "Mars/Olympus"}, "IANA"),
    ({"WOO_TIMEOUT": "fast"}, "number"),
    ({"MCP_PORT": "http"}, "integer"),
])
def test_invalid_values_are_rejected_with_a_clear_message(override, message):
    with pytest.raises(SystemExit, match=message):
        Settings.from_env({**ENV, **override})


def test_default_store_day_is_the_indian_day():
    """With the default zone, 'today' for an order placed at 23:30 IST on the 19th (18:00 UTC) is the 19th."""
    s = Settings.from_env(ENV)
    start, end = today_window(s.timezone, at(2026, 10, 19, 18, 0))
    assert (start, end) == ("2026-10-18T18:30:00Z", "2026-10-19T18:30:00Z")
    assert start <= "2026-10-19T18:00:00Z" < end
