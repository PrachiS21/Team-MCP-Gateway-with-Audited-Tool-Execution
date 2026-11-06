"""All settings come from environment variables (Kubernetes Secrets / ConfigMaps in the
cluster). Nothing secret has a default."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, MutableMapping

from gateway.logs import LOG_FORMATS

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")  # as the chat agent requires (chat.py)


def load_secrets_file(environ: MutableMapping[str, str] = os.environ) -> list[str]:
    """GATEWAY_SECRETS_FILE: several secrets in one file of NAME=value lines, e.g. ONE Secret Manager secret that
    Cloud Run mounts as a file (a secret per value would go past Secret Manager's six free ones). Each line becomes an
    environment variable unless that variable is already set: the real environment wins. Blank lines and # comments
    are skipped; nothing is expanded or run. Errors name the line, never a value. Returns the names it set."""
    path = environ.get("GATEWAY_SECRETS_FILE")
    if not path:
        return []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError as exc:
        raise SystemExit(f"GATEWAY_SECRETS_FILE {path} cannot be read: {exc.strerror}") from None
    found: dict[str, str] = {}
    for n, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep or not _ENV_NAME.fullmatch(name):
            raise SystemExit(f"GATEWAY_SECRETS_FILE line {n}: expected NAME=value")
        if name in found:
            raise SystemExit(f"GATEWAY_SECRETS_FILE line {n}: {name} is set twice")
        found[name] = value
    added = [name for name in found if not environ.get(name)]
    for name in added:
        environ[name] = found[name]
    return added


@dataclass(frozen=True)
class Settings:
    secret_key: str  # GATEWAY_SECRET_KEY, a Fernet key (see registry/crypto.py)
    db_url: str = "sqlite:///gateway.db"  # GATEWAY_DB_URL: registry, users, keys, audit log
    jobs_db_url: str = "sqlite:///gateway_jobs.db"  # GATEWAY_JOBS_DB_URL: undo jobs (separate file on purpose)
    admin_email: str | None = None  # GATEWAY_ADMIN_EMAIL: first admin, created at startup
    admin_key: str | None = field(default=None, repr=False)  # GATEWAY_ADMIN_KEY: that admin's key, from a secret manager
    allow_loopback: bool = False  # GATEWAY_ALLOW_LOOPBACK=1: dev only, lets upstream URLs point at localhost
    host: str = "0.0.0.0"  # GATEWAY_HOST
    port: int = 8000  # GATEWAY_PORT, else PORT (set by Cloud Run)
    incident_log: Path | None = None  # GATEWAY_INCIDENT_LOG: JSONL file that survives an audit DB outage
    worker_interval: float = 2.0  # GATEWAY_WORKER_INTERVAL: seconds between undo-worker passes
    sync_interval: float = 5.0  # GATEWAY_SYNC_INTERVAL: seconds between registry -> live-server syncs
    seed_file: Path | None = None  # GATEWAY_SEED_FILE: JSON listing servers to register/sync at startup (see seed.py)
    seed_json: str | None = None  # GATEWAY_SEED_JSON: the same, as text (Cloud Run: plain config; seeds hold no secrets)
    woo_slug: str = "woo"  # GATEWAY_WOO_SLUG: slug of the registered WooCommerce MCP server (its undo rules apply)
    cookie_secure: bool = False  # GATEWAY_COOKIE_SECURE=1 in production (HTTPS): the browser then sends the cookie over HTTPS only
    # The chat agent (chat.py) is on only when a model and a key are set. Any OpenAI-compatible endpoint works.
    llm_base_url: str = "https://api.groq.com/openai/v1"  # GATEWAY_LLM_BASE_URL
    llm_model: str | None = None  # GATEWAY_LLM_MODEL, e.g. a tool-calling model your provider offers
    llm_api_key: str | None = field(default=None, repr=False)  # GATEWAY_LLM_API_KEY: from a Kubernetes Secret
    llm_timeout: float = 60.0  # GATEWAY_LLM_TIMEOUT: seconds per LLM request
    log_format: str = "text"  # GATEWAY_LOG_FORMAT: "text" (readable) or "json" (one object per line, for Cloud Logging)
    gcp_project: str | None = None  # GATEWAY_GCP_PROJECT: with json logs, links each line to its request's trace
    # The Dialogflow CX webhook (cx.py): POST /api/cx/webhook exists only when enabled. It uses the chat agent's LLM.
    cx_enabled: bool = False  # GATEWAY_CX_ENABLED=1
    cx_timeout: float = 25.0  # GATEWAY_CX_TIMEOUT: seconds per customer message (CX waits at most 30)
    cx_history_turns: int = 6  # GATEWAY_CX_HISTORY_TURNS: earlier exchanges sent back to the model (0-10)
    cx_tools: tuple[str, ...] = ()  # GATEWAY_CX_TOOLS: comma-separated <server>__<tool>; empty = every read-only tool

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        key = env.get("GATEWAY_SECRET_KEY")
        if not key:
            raise SystemExit("GATEWAY_SECRET_KEY is not set. Generate one with: "
                             "python -c 'from gateway.registry.crypto import generate_key; print(generate_key())'")
        if env.get("GATEWAY_ADMIN_KEY") and not env.get("GATEWAY_ADMIN_EMAIL"):
            raise SystemExit("GATEWAY_ADMIN_KEY is set but GATEWAY_ADMIN_EMAIL is not: the key needs an admin to belong to")
        log_format = env.get("GATEWAY_LOG_FORMAT") or cls.log_format
        if log_format not in LOG_FORMATS:
            raise SystemExit(f"GATEWAY_LOG_FORMAT must be one of {', '.join(LOG_FORMATS)}")
        if env.get("GATEWAY_SEED_FILE") and env.get("GATEWAY_SEED_JSON"):
            raise SystemExit("set GATEWAY_SEED_FILE or GATEWAY_SEED_JSON, not both")
        cx_timeout, cx_history_turns, cx_tools = _cx_settings(env)
        return cls(
            secret_key=key,
            db_url=env.get("GATEWAY_DB_URL", cls.db_url),
            jobs_db_url=env.get("GATEWAY_JOBS_DB_URL", cls.jobs_db_url),
            admin_email=env.get("GATEWAY_ADMIN_EMAIL") or None,
            admin_key=env.get("GATEWAY_ADMIN_KEY") or None,
            allow_loopback=env.get("GATEWAY_ALLOW_LOOPBACK", "") in ("1", "true", "yes"),
            host=env.get("GATEWAY_HOST", cls.host),
            port=int(env.get("GATEWAY_PORT") or env.get("PORT") or cls.port),
            incident_log=Path(env["GATEWAY_INCIDENT_LOG"]) if env.get("GATEWAY_INCIDENT_LOG") else None,
            worker_interval=float(env.get("GATEWAY_WORKER_INTERVAL", cls.worker_interval)),
            sync_interval=float(env.get("GATEWAY_SYNC_INTERVAL", cls.sync_interval)),
            seed_file=Path(env["GATEWAY_SEED_FILE"]) if env.get("GATEWAY_SEED_FILE") else None,
            seed_json=env.get("GATEWAY_SEED_JSON") or None,
            woo_slug=env.get("GATEWAY_WOO_SLUG", cls.woo_slug),
            cookie_secure=env.get("GATEWAY_COOKIE_SECURE", "") in ("1", "true", "yes"),
            llm_base_url=env.get("GATEWAY_LLM_BASE_URL") or cls.llm_base_url,
            llm_model=env.get("GATEWAY_LLM_MODEL") or None,
            llm_api_key=env.get("GATEWAY_LLM_API_KEY") or None,
            llm_timeout=float(env.get("GATEWAY_LLM_TIMEOUT", cls.llm_timeout)),
            log_format=log_format,
            gcp_project=env.get("GATEWAY_GCP_PROJECT") or None,
            cx_enabled=env.get("GATEWAY_CX_ENABLED", "") in ("1", "true", "yes"),
            cx_timeout=cx_timeout,
            cx_history_turns=cx_history_turns,
            cx_tools=cx_tools,
        )


def _cx_settings(env: Mapping[str, str]) -> tuple[float, int, tuple[str, ...]]:
    try:
        timeout = float(env.get("GATEWAY_CX_TIMEOUT") or Settings.cx_timeout)
        turns = int(env.get("GATEWAY_CX_HISTORY_TURNS") or Settings.cx_history_turns)
    except ValueError:
        raise SystemExit("GATEWAY_CX_TIMEOUT must be a number and GATEWAY_CX_HISTORY_TURNS a whole number") from None
    if not 1 <= timeout < 30:
        raise SystemExit("GATEWAY_CX_TIMEOUT must be at least 1 and under 30 seconds: Dialogflow CX waits at most 30")
    if not 0 <= turns <= 10:
        raise SystemExit("GATEWAY_CX_HISTORY_TURNS must be between 0 and 10")
    tools = tuple(t.strip() for t in (env.get("GATEWAY_CX_TOOLS") or "").split(",") if t.strip())
    for t in tools:
        if not _TOOL_NAME.fullmatch(t):
            raise SystemExit(f"GATEWAY_CX_TOOLS: {t!r} is not a tool name (<server>__<tool>)")
    return timeout, turns, tools
