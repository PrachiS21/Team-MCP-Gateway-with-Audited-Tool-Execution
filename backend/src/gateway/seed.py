"""Register servers from a config file at startup (Helm mounts it from a ConfigMap; on Cloud Run the same JSON comes
as text in GATEWAY_SEED_JSON).

The SLUG is the match key. For each entry: no server with that slug -> create it; one exists -> update it
to match the file (name, URL, credential, timeout). An admin's enabled/disabled switch is never touched,
servers missing from the file are never deleted, and servers the file does not mention are left alone.

EVERY start is audited, changed or not: each server in the file gets an admin_events row
(server_created / server_updated / server_unchanged). Changes are also logged at WARNING and to the incident
JSONL file; unchanged checks are logged quietly at INFO. Nothing secret is ever written: only which fields changed.

Secrets are never in the file: an entry names an environment variable ("credential_env") that Kubernetes
fills from a Secret. A bad file stops the gateway from starting, with nothing applied.

Optional "users": [{"email", "role", "key_env"}] are created (or their role brought in line) and their key, read
from the named variable, is registered: only its hash is stored. This is what keeps service accounts (the CX
webhook's) working where the database starts empty after every restart. Each one is audited as user_created /
user_updated / user_unchanged, with the key's id but never the key. An admin's deactivation is never undone, and a
revoked key stays revoked.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from gateway.audit.service import AuditLog
from gateway.auth.keys import parse_key
from gateway.auth.service import ROLES, AuthError, AuthService
from gateway.incidents import IncidentLog
from gateway.registry.registry import CREDENTIAL_TYPES, ServerChange, ServerRegistry

log = logging.getLogger("gateway")

ACTOR = "system:seed"
ALLOWED_KEYS = {"slug", "name", "url", "credential_type", "credential_env", "credential_header", "timeout"}
USER_KEYS = {"email", "role", "key_env"}
SECRET_LOOKING_KEYS = {"credential", "token", "password", "secret", "api_key", "authorization"}


class SeedError(Exception):
    pass


@dataclass(frozen=True)
class SeedReport:
    changes: list[ServerChange]  # servers created or updated at this start
    unchanged: list[str]  # slugs checked and already matching
    users: dict[str, str] = field(default_factory=dict)  # email -> "created" / "updated" / "unchanged"


@dataclass(frozen=True)
class SeedEntry:
    slug: str
    name: str
    url: str
    credential_type: str = "none"
    credential: str | None = field(default=None, repr=False)  # resolved from the environment; never printed
    credential_header: str | None = None
    timeout: float = 10.0


@dataclass(frozen=True)
class SeedUser:
    email: str
    role: str
    key_env: str
    key: str = field(repr=False)  # resolved from the environment; never printed


def _read(source: Path | str) -> dict:
    """`source` is the seed file, or the seed itself as JSON text (GATEWAY_SEED_JSON)."""
    where = f"seed file {source}" if isinstance(source, Path) else "GATEWAY_SEED_JSON"
    try:
        raw = json.loads(source.read_text() if isinstance(source, Path) else source)
    except FileNotFoundError:
        raise SeedError(f"{where} does not exist") from None
    except (OSError, ValueError) as exc:
        raise SeedError(f"{where} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("servers"), list):
        raise SeedError('seed file must look like {"servers": [ ... ], "users": [ ... ]} ("users" is optional)')
    return raw


def load_seed(source: Path | str, env: Mapping[str, str]) -> list[SeedEntry]:
    return _servers(_read(source), env)


def _servers(raw: dict, env: Mapping[str, str]) -> list[SeedEntry]:
    servers = raw["servers"]
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


def _users(raw: dict, env: Mapping[str, str]) -> list[SeedUser]:
    """Users whose keys live in a secret manager. Needed where the database starts empty after a restart
    (Cloud Run with SQLite): without them, a service account such as the CX webhook's would lose its key."""
    items = raw.get("users", [])
    if not isinstance(items, list):
        raise SeedError('"users" must be a list')
    users: list[SeedUser] = []
    for i, item in enumerate(items, 1):
        where = f"user #{i}"
        if not isinstance(item, dict):
            raise SeedError(f"{where} must be an object")
        for key in item:
            if key.lower() in SECRET_LOOKING_KEYS | {"key"}:
                raise SeedError(f'{where}: {key!r} must not be in the file; name an environment variable with '
                                f'"key_env" instead (filled from a secret)')
            if key not in USER_KEYS:
                raise SeedError(f"{where}: unknown key {key!r} (allowed: {', '.join(sorted(USER_KEYS))})")
        email = item.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise SeedError(f"{where}: 'email' is required")
        email = email.strip().lower()
        role = item.get("role", "member")
        if role not in ROLES:
            raise SeedError(f"{where} ({email}): role must be one of {ROLES}")
        var = item.get("key_env")
        if not var:
            raise SeedError(f"{where} ({email}): 'key_env' is required (without a key the user could not log in "
                            f"after a restart)")
        key = env.get(var)
        if not key:
            raise SeedError(f"{where} ({email}): environment variable {var} is not set or empty")
        if parse_key(key) is None:
            raise SeedError(f"{where} ({email}): environment variable {var} does not hold a gateway API key "
                            f"(make one with: gateway-admin generate-key-string)")
        users.append(SeedUser(email, role, var, key))

    emails = [u.email for u in users]
    dupes = sorted({e for e in emails if emails.count(e) > 1})
    if dupes:
        raise SeedError(f"duplicate user(s) in the seed file: {', '.join(dupes)}")
    by_key: dict[str, str] = {}
    for u in users:
        if u.key in by_key:
            raise SeedError(f"{by_key[u.key]} and {u.key_env} hold the same key: each user needs its own")
        by_key[u.key] = u.key_env
    return users


def _record(audit: AuditLog, action: str, slug: str | None, details: dict) -> None:
    try:
        audit.record_event(actor=ACTOR, action=action, server_slug=slug, details=details)
    except Exception:  # noqa: BLE001 - never block startup on the audit store
        log.exception("could not record the admin event %s", action)


def apply_seed(source: Path | str, env: Mapping[str, str], registry: ServerRegistry, audit: AuditLog,
               incidents: IncidentLog, auth: AuthService | None = None) -> SeedReport:
    """`source`: the seed file's path, or the seed as JSON text."""
    raw = _read(source)
    entries, users = _servers(raw, env), _users(raw, env)
    if users and auth is None:
        raise SeedError("the seed file lists users, but this caller gave no user store")

    # Check EVERY entry before changing anything, so a bad one never leaves a half-applied seed.
    for e in entries:
        try:
            registry.validate_entry(slug=e.slug, url=e.url, credential_type=e.credential_type,
                                    credential=e.credential, credential_header=e.credential_header)
        except ValueError as exc:
            raise SeedError(f"server {e.slug!r}: {exc}") from None
    for u in users:  # e.g. the key's id is already taken by another user's key
        existing = auth.get_user_by_email(u.email)
        try:
            auth.check_registrable(u.key, existing.id if existing else None)
        except AuthError as exc:
            raise SeedError(f"user {u.email!r} ({u.key_env}): {exc}") from None

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
    user_actions = {u.email: _apply_user(u, auth, audit, incidents) for u in users}
    return SeedReport(changes, unchanged, user_actions)


def _apply_user(u: SeedUser, auth: AuthService, audit: AuditLog, incidents: IncidentLog) -> str:
    """Create the user or bring their role in line with the file, and register their key. Like a server's
    enabled switch, an admin's deactivation is never undone here. Returns "created", "updated" or "unchanged"."""
    user, changed = auth.get_user_by_email(u.email), []
    created = user is None
    if created:
        user = auth.create_user(u.email, u.role)
    elif user.role != u.role:
        user = auth.set_role(user.id, u.role)
        changed.append("role")
    info, key_added = auth.register_key(user.id, u.key, f"seed ({u.key_env})")
    if key_added and not created:
        changed.append("key")
    action = "created" if created else ("updated" if changed else "unchanged")

    details = {"email": u.email, "role": user.role, "key_id": info.key_id, "changed": changed}  # never the key
    if info.revoked:
        details["key_revoked"] = True
        log.warning("seed: the key in %s (key id %s) was revoked and stays revoked", u.key_env, info.key_id)
    _record(audit, f"user_{action}", None, details)
    if action == "unchanged":
        log.info("seed: user with key id %s already matches the seed file", info.key_id)
    else:  # no email in the logs; the admin event above has it
        incidents.notice(f"user_{action}_from_seed", {"key_id": info.key_id, "role": user.role, "changed": changed})
    return action
