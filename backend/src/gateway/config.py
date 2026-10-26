"""All settings come from environment variables (Kubernetes Secrets / ConfigMaps in the
cluster). Nothing secret has a default."""

import os
from dataclasses import dataclass, field
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
    seed_file: Path | None = None  # GATEWAY_SEED_FILE: JSON listing servers to register/sync at startup (see seed.py)
    woo_slug: str = "woo"  # GATEWAY_WOO_SLUG: slug of the registered WooCommerce MCP server (its undo rules apply)
    cookie_secure: bool = False  # GATEWAY_COOKIE_SECURE=1 in production (HTTPS): the browser then sends the cookie over HTTPS only
    # The chat agent (chat.py) is on only when a model and a key are set. Any OpenAI-compatible endpoint works.
    llm_base_url: str = "https://api.groq.com/openai/v1"  # GATEWAY_LLM_BASE_URL
    llm_model: str | None = None  # GATEWAY_LLM_MODEL, e.g. a tool-calling model your provider offers
    llm_api_key: str | None = field(default=None, repr=False)  # GATEWAY_LLM_API_KEY: from a Kubernetes Secret
    llm_timeout: float = 60.0  # GATEWAY_LLM_TIMEOUT: seconds per LLM request

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
            seed_file=Path(env["GATEWAY_SEED_FILE"]) if env.get("GATEWAY_SEED_FILE") else None,
            woo_slug=env.get("GATEWAY_WOO_SLUG", cls.woo_slug),
            cookie_secure=env.get("GATEWAY_COOKIE_SECURE", "") in ("1", "true", "yes"),
            llm_base_url=env.get("GATEWAY_LLM_BASE_URL") or cls.llm_base_url,
            llm_model=env.get("GATEWAY_LLM_MODEL") or None,
            llm_api_key=env.get("GATEWAY_LLM_API_KEY") or None,
            llm_timeout=float(env.get("GATEWAY_LLM_TIMEOUT", cls.llm_timeout)),
        )
