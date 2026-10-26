"""Register servers from a config file at startup (Helm mounts it from a ConfigMap).

The SLUG is the match key. For each entry: no server with that slug -> create it; one exists -> update it
to match the file (name, URL, credential, timeout). An admin's enabled/disabled switch is never touched,
servers missing from the file are never deleted, and servers the file does not mention are left alone.

EVERY start is audited, changed or not: each server in the file gets an admin_events row
(server_created / server_updated / server_unchanged). Changes are also logged at WARNING and to the incident
JSONL file; unchanged checks are logged quietly at INFO. Nothing secret is ever written: only which fields changed.

Secrets are never in the file: an entry names an environment variable ("credential_env") that Kubernetes
fills from a Secret. A bad file stops the gateway from starting, with nothing applied.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from gateway.audit.service import AuditLog
from gateway.incidents import IncidentLog
from gateway.registry.registry import CREDENTIAL_TYPES, ServerChange, ServerRegistry

log = logging.getLogger("gateway")

ACTOR = "system:seed"
ALLOWED_KEYS = {"slug", "name", "url", "credential_type", "credential_env", "credential_header", "timeout"}
SECRET_LOOKING_KEYS = {"credential", "token", "password", "secret", "api_key", "authorization"}


class SeedError(Exception):
    pass


@dataclass(frozen=True)
class SeedReport:
    changes: list[ServerChange]  # servers created or updated at this start
    unchanged: list[str]  # slugs checked and already matching


@dataclass(frozen=True)
class SeedEntry:
    slug: str
    name: str
    url: str
    credential_type: str = "none"
    credential: str | None = field(default=None, repr=False)  # resolved from the environment; never printed
    credential_header: str | None = None
    timeout: float = 10.0


def load_seed(path: Path, env: Mapping[str, str]) -> list[SeedEntry]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise SeedError(f"seed file {path} does not exist") from None
    except (OSError, ValueError) as exc:
        raise SeedError(f"seed file {path} is not valid JSON: {exc}") from None
    servers = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(servers, list):
        raise SeedError('seed file must look like {"servers": [ ... ]}')

    entries: list[SeedEntry] = []
    for i, item in enumerate(servers, 1):
        where = f"server #{i}"
        if not isinstance(item, dict):
            raise SeedError(f"{where} must be an object")
        for key in item:
            if key.lower() in SECRET_LOOKING_KEYS:
                raise SeedError(f"{where}: {key!r} must not be in the file; name an environment variable with "
                                f'"credential_env" instead (Kubernetes fills it from a Secret)')
            if key not in ALLOWED_KEYS:
                raise SeedError(f"{where}: unknown key {key!r} (allowed: {', '.join(sorted(ALLOWED_KEYS))})")
        for required in ("slug", "url"):
            if not isinstance(item.get(required), str) or not item[required].strip():
                raise SeedError(f"{where}: {required!r} is required")
        ctype = item.get("credential_type", "none")
        if ctype not in CREDENTIAL_TYPES:
            raise SeedError(f"{where}: credential_type must be one of {CREDENTIAL_TYPES}")
        credential = None
        if ctype != "none":
            var = item.get("credential_env")
            if not var:
                raise SeedError(f"{where} ({item['slug']}): credential_type {ctype!r} needs credential_env")
            credential = env.get(var)
            if not credential:
                raise SeedError(f"{where} ({item['slug']}): environment variable {var} is not set or empty")
        elif item.get("credential_env"):
            raise SeedError(f"{where} ({item['slug']}): credential_env given but credential_type is 'none'")
        try:
            timeout = float(item.get("timeout", SeedEntry.timeout))
        except (TypeError, ValueError):
            raise SeedError(f"{where}: timeout must be a number") from None
        if timeout <= 0:
            raise SeedError(f"{where}: timeout must be positive")
        entries.append(SeedEntry(slug=item["slug"].strip(), name=str(item.get("name") or item["slug"]).strip(),
                                 url=item["url"].strip(), credential_type=ctype, credential=credential,
                                 credential_header=item.get("credential_header"), timeout=timeout))

    slugs = [e.slug for e in entries]
    dupes = sorted({s for s in slugs if slugs.count(s) > 1})
    if dupes:
        raise SeedError(f"duplicate slug(s) in the seed file: {', '.join(dupes)}")
    return entries


def _record(audit: AuditLog, action: str, slug: str, details: dict) -> None:
    try:
        audit.record_event(actor=ACTOR, action=action, server_slug=slug, details=details)
    except Exception:  # noqa: BLE001 - never block startup on the audit store
        log.exception("could not record the admin event %s for %s", action, slug)


def apply_seed(path: Path, env: Mapping[str, str], registry: ServerRegistry, audit: AuditLog,
               incidents: IncidentLog) -> SeedReport:
    entries = load_seed(path, env)

    # Check EVERY entry before changing anything, so a bad one never leaves a half-applied seed.
    for e in entries:
        try:
            registry.validate_entry(slug=e.slug, url=e.url, credential_type=e.credential_type,
                                    credential=e.credential, credential_header=e.credential_header)
        except ValueError as exc:
            raise SeedError(f"server {e.slug!r}: {exc}") from None

    changes: list[ServerChange] = []
    unchanged: list[str] = []
    for e in entries:
        change = registry.sync_server(slug=e.slug, name=e.name, url=e.url, credential_type=e.credential_type,
                                      credential=e.credential, credential_header=e.credential_header,
                                      timeout=e.timeout)
        if change is None:  # checked, already matching: audited, but quiet in the logs
            unchanged.append(e.slug)
            _record(audit, "server_unchanged", e.slug, {"changed": []})
            log.info("seed: server %s already matches the seed file", e.slug)
            continue
        changes.append(change)
        details = {"changed": list(change.changed), **change.details}
        incidents.notice(f"server_{change.action}_from_seed", {"server": change.slug, **details})
        _record(audit, f"server_{change.action}", change.slug, details)
    return SeedReport(changes, unchanged)
