"""All settings come from environment variables (Kubernetes Secrets / ConfigMaps in the
cluster). Nothing secret has a default."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    secret_key: str  # GATEWAY_SECRET_KEY, a Fernet key (see registry/crypto.py)
    db_url: str = "sqlite:///gateway.db"  # GATEWAY_DB_URL: registry, users, keys, audit log
    jobs_db_url: str = "sqlite:///gateway_jobs.db"  # GATEWAY_JOBS_DB_URL: undo jobs (separate file on purpose)
    admin_email: str | None = None  # GATEWAY_ADMIN_EMAIL: first admin, created at startup
    allow_loopback: bool = False  # GATEWAY_ALLOW_LOOPBACK=1: dev only, lets upstream URLs point at localhost
    host: str = "0.0.0.0"  # GATEWAY_HOST
    port: int = 8000  # GATEWAY_PORT
    incident_log: Path | None = None  # GATEWAY_INCIDENT_LOG: JSONL file that survives an audit DB outage
    worker_interval: float = 2.0  # GATEWAY_WORKER_INTERVAL: seconds between undo-worker passes
    sync_interval: float = 5.0  # GATEWAY_SYNC_INTERVAL: seconds between registry -> live-server syncs
    woo_slug: str = "woo"  # GATEWAY_WOO_SLUG: slug of the registered WooCommerce MCP server (its undo rules apply)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        key = env.get("GATEWAY_SECRET_KEY")
        if not key:
            raise SystemExit("GATEWAY_SECRET_KEY is not set. Generate one with: "
                             "python -c 'from gateway.registry.crypto import generate_key; print(generate_key())'")
        return cls(
            secret_key=key,
            db_url=env.get("GATEWAY_DB_URL", cls.db_url),
            jobs_db_url=env.get("GATEWAY_JOBS_DB_URL", cls.jobs_db_url),
            admin_email=env.get("GATEWAY_ADMIN_EMAIL") or None,
            allow_loopback=env.get("GATEWAY_ALLOW_LOOPBACK", "") in ("1", "true", "yes"),
            host=env.get("GATEWAY_HOST", cls.host),
            port=int(env.get("GATEWAY_PORT", cls.port)),
            incident_log=Path(env["GATEWAY_INCIDENT_LOG"]) if env.get("GATEWAY_INCIDENT_LOG") else None,
            worker_interval=float(env.get("GATEWAY_WORKER_INTERVAL", cls.worker_interval)),
            sync_interval=float(env.get("GATEWAY_SYNC_INTERVAL", cls.sync_interval)),
            woo_slug=env.get("GATEWAY_WOO_SLUG", cls.woo_slug),
        )
