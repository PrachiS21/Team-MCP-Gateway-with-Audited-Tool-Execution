"""Settings, all from environment variables (Kubernetes Secrets / ConfigMaps in the cluster).
Secrets are excluded from repr() so they cannot leak through a log line or an error message."""

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REQUIRED = ("WOO_BASE_URL", "WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET", "MCP_SERVER_TOKEN")


@dataclass(frozen=True)
class Settings:
    base_url: str  # WOO_BASE_URL, e.g. http://wordpress.shop.svc.cluster.local (no /wp-json)
    consumer_key: str = field(repr=False)  # WOO_CONSUMER_KEY
    consumer_secret: str = field(repr=False)  # WOO_CONSUMER_SECRET
    server_token: str = field(repr=False)  # MCP_SERVER_TOKEN: what the gateway must present
    timezone: str = "Asia/Kolkata"  # STORE_TIMEZONE: IANA name; default is IST (UTC+05:30, no daylight saving)
    timeout: float = 10.0  # WOO_TIMEOUT, seconds per store request
    host: str = "0.0.0.0"  # MCP_HOST
    port: int = 8000  # MCP_PORT

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ) if env is None else env
        missing = [name for name in REQUIRED if not env.get(name)]
        if missing:
            raise SystemExit("missing required environment variables: " + ", ".join(missing))
        base_url = env["WOO_BASE_URL"].rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise SystemExit("WOO_BASE_URL must start with http:// or https://")
        tz = env.get("STORE_TIMEZONE", cls.timezone)
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise SystemExit(f"STORE_TIMEZONE {tz!r} is not a valid IANA time zone (e.g. Asia/Kolkata)") from None
        try:
            timeout = float(env.get("WOO_TIMEOUT", cls.timeout))
            port = int(env.get("MCP_PORT", cls.port))
        except ValueError:
            raise SystemExit("WOO_TIMEOUT must be a number and MCP_PORT an integer") from None
        return cls(base_url=base_url, consumer_key=env["WOO_CONSUMER_KEY"], consumer_secret=env["WOO_CONSUMER_SECRET"],
                   server_token=env["MCP_SERVER_TOKEN"], timezone=tz, timeout=timeout,
                   host=env.get("MCP_HOST", cls.host), port=port)
