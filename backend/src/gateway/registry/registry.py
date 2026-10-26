"""Persistent list of upstream servers (SQLAlchemy + SQLite).

Credentials are write-only: they are encrypted on the way in, and every public
read returns a ServerView with `has_credential` instead of the secret. The one
method that decrypts, `upstream_config`, exists for the manager.
"""

import builtins  # noqa: F401  (used in a string annotation)
import uuid
from urllib.parse import urlsplit
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, String, Text, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from gateway.mcp.manager import SLUG_RE, ManagerError, UpstreamManager
from gateway.mcp.upstream_client import UpstreamConfig

from .crypto import SecretBox
from .url_policy import validate_upstream_url

CREDENTIAL_TYPES = ("none", "bearer", "header")
UNSET: Any = object()  # "leave this field as it is" in update()


class RegistryError(ValueError):
    pass


class _Base(DeclarativeBase):
    pass


class _Server(_Base):
    __tablename__ = "servers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    credential_type: Mapped[str] = mapped_column(String(10), default="none")
    credential_header: Mapped[str | None] = mapped_column(String(100), nullable=True)
    credential_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted
    timeout: Mapped[float] = mapped_column(Float, default=10.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@dataclass(frozen=True)
class ServerChange:
    """What sync_server() did. `details` is safe to log and store: it never holds a secret value."""

    slug: str
    action: str  # "created" | "updated"
    changed: tuple[str, ...]  # field NAMES that changed ("credential" means the secret changed; its value is not here)
    details: dict


def safe_url(url: str) -> str:
    """scheme://host:port/path only: no user:password@ part, no query string, no fragment."""
    p = urlsplit(url)
    host = p.hostname or ""
    return f"{p.scheme}://{host}{':' + str(p.port) if p.port else ''}{p.path}"


@dataclass(frozen=True)
class ServerView:
    """What the rest of the system (and the dashboard) may see. No secret."""

    id: str
    name: str
    slug: str
    url: str
    enabled: bool
    credential_type: str
    credential_header: str | None
    has_credential: bool
    timeout: float
    created_at: datetime


def _view(s: _Server) -> ServerView:
    return ServerView(s.id, s.name, s.slug, s.url, s.enabled, s.credential_type, s.credential_header,
                      s.credential_enc is not None, s.timeout, s.created_at)


class ServerRegistry:
    def __init__(self, db_url: str, box: SecretBox, allow_loopback: bool = False):
        self._engine = create_engine(db_url)
        _Base.metadata.create_all(self._engine)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        self._box = box
        self._allow_loopback = allow_loopback

    # -- validation -------------------------------------------------------

    def _validate(self, slug: str, url: str, ctype: str, header: str | None, credential: str | None) -> None:
        if not SLUG_RE.match(slug):
            raise RegistryError(f"invalid slug {slug!r}: use lowercase letters, digits and single hyphens")
        validate_upstream_url(url, self._allow_loopback)  # raises UrlPolicyError (a ValueError)
        if ctype not in CREDENTIAL_TYPES:
            raise RegistryError(f"credential_type must be one of {CREDENTIAL_TYPES}")
        if ctype == "none" and credential:
            raise RegistryError("a credential was given but credential_type is 'none'")
        if ctype != "none" and not credential:
            raise RegistryError(f"credential_type {ctype!r} needs a credential")
        if ctype == "header" and not header:
            raise RegistryError("credential_type 'header' needs credential_header (the header name)")

    # -- CRUD -------------------------------------------------------------

    def create(self, *, name: str, slug: str, url: str, credential_type: str = "none",
               credential: str | None = None, credential_header: str | None = None,
               enabled: bool = True, timeout: float = 10.0) -> ServerView:
        self._validate(slug, url, credential_type, credential_header, credential)
        row = _Server(
            id=uuid.uuid4().hex, name=name, slug=slug, url=url, enabled=enabled,
            credential_type=credential_type,
            credential_header=credential_header if credential_type == "header" else None,
            credential_enc=self._box.encrypt(credential) if credential else None,
            timeout=timeout, created_at=datetime.now(timezone.utc),
        )
        with self._sessions() as s:
            s.add(row)
            try:
                s.commit()
            except IntegrityError:
                raise RegistryError(f"slug {slug!r} is already used by another server") from None
        return _view(row)

    def get(self, server_id: str) -> ServerView:
        with self._sessions() as s:
            return _view(self._row(s, server_id))

    def get_by_slug(self, slug: str) -> ServerView:
        with self._sessions() as s:
            row = s.scalars(select(_Server).where(_Server.slug == slug)).first()
            if row is None:
                raise RegistryError(f"no server with slug {slug!r}")
            return _view(row)

    def list(self) -> list[ServerView]:
        with self._sessions() as s:
            return [_view(r) for r in s.scalars(select(_Server).order_by(_Server.created_at))]

    def update(self, server_id: str, *, name: str = UNSET, slug: str = UNSET, url: str = UNSET,
               credential_type: str = UNSET, credential: str | None = UNSET,
               credential_header: str | None = UNSET, timeout: float = UNSET) -> ServerView:
        """Change only the fields passed. Leaving `credential` out keeps the stored
        secret; passing None removes it."""
        with self._sessions() as s:
            row = self._row(s, server_id)
            new_slug = row.slug if slug is UNSET else slug
            new_url = row.url if url is UNSET else url
            new_type = row.credential_type if credential_type is UNSET else credential_type
            new_header = row.credential_header if credential_header is UNSET else credential_header
            if credential is UNSET:
                # Keep the secret, but re-check the combination makes sense.
                check_cred = "kept" if row.credential_enc else None
            else:
                check_cred = credential
            self._validate(new_slug, new_url, new_type, new_header, check_cred)
            row.slug, row.url, row.credential_type = new_slug, new_url, new_type
            row.credential_header = new_header if new_type == "header" else None
            if name is not UNSET:
                row.name = name
            if timeout is not UNSET:
                row.timeout = timeout
            if credential is not UNSET:
                row.credential_enc = self._box.encrypt(credential) if credential else None
            try:
                s.commit()
            except IntegrityError:
                raise RegistryError(f"slug {new_slug!r} is already used by another server") from None
            return _view(row)

    def set_enabled(self, server_id: str, enabled: bool) -> ServerView:
        with self._sessions() as s:
            row = self._row(s, server_id)
            row.enabled = enabled
            s.commit()
            return _view(row)

    def delete(self, server_id: str) -> None:
        with self._sessions() as s:
            s.delete(self._row(s, server_id))
            s.commit()

    def _row(self, s: Session, server_id: str) -> _Server:
        row = s.get(_Server, server_id)
        if row is None:
            raise RegistryError(f"unknown server id {server_id!r}")
        return row

    # -- sync from a seed file ---------------------------------------------

    def validate_entry(self, *, slug: str, url: str, credential_type: str, credential: str | None,
                       credential_header: str | None) -> None:
        """Run the same checks create() would (slug, URL policy, credential combination), without saving."""
        self._validate(slug, url, credential_type, credential_header, credential)

    def sync_server(self, *, slug: str, name: str, url: str, credential_type: str, credential: str | None,
                    credential_header: str | None, timeout: float) -> "ServerChange | None":
        """Make the server with this SLUG match the given values. The slug is the match key; the
        enabled/disabled switch is never touched. Returns None when nothing changed."""
        try:
            existing = self.get_by_slug(slug)
        except RegistryError:
            self.create(name=name, slug=slug, url=url, credential_type=credential_type, credential=credential,
                        credential_header=credential_header, timeout=timeout)
            return ServerChange(slug, "created", (), {"url": safe_url(url), "credential_type": credential_type})

        current = self.upstream_config(existing.id).credential  # decrypted only to compare, never exposed
        header_now = existing.credential_header if existing.credential_type == "header" else None
        header_new = credential_header if credential_type == "header" else None
        changed: list[str] = []
        details: dict = {}
        if existing.name != name:
            changed.append("name"); details["name"] = {"from": existing.name, "to": name}
        if existing.url != url:
            changed.append("url"); details["url"] = {"from": safe_url(existing.url), "to": safe_url(url)}
        if existing.credential_type != credential_type:
            changed.append("credential_type"); details["credential_type"] = {"from": existing.credential_type, "to": credential_type}
        if header_now != header_new:
            changed.append("credential_header"); details["credential_header"] = {"from": header_now, "to": header_new}
        if current != credential:
            changed.append("credential")  # the VALUE is deliberately not recorded anywhere
        if existing.timeout != timeout:
            changed.append("timeout"); details["timeout"] = {"from": existing.timeout, "to": timeout}
        if not changed:
            return None
        self.update(existing.id, name=name, url=url, credential_type=credential_type, credential=credential,
                    credential_header=credential_header, timeout=timeout)
        return ServerChange(slug, "updated", tuple(changed), details)

    # -- bridge to the manager -------------------------------------------

    def upstream_config(self, server_id: str) -> UpstreamConfig:
        """The only place a stored credential is decrypted. Server-side use only."""
        with self._sessions() as s:
            return self._config(self._row(s, server_id))

    def _config(self, row: _Server) -> UpstreamConfig:
        secret = self._box.decrypt(row.credential_enc) if row.credential_enc else None
        if row.credential_type == "header":
            return UpstreamConfig(row.url, secret, row.credential_header or "Authorization", None, row.timeout)
        return UpstreamConfig(row.url, secret, timeout=row.timeout)  # bearer or none

    def desired(self) -> "builtins.list[tuple[str, str, UpstreamConfig, bool]]":  # `list` is shadowed by the method above
        """Every saved server as (id, slug, config, enabled): what UpstreamManager.sync() applies."""
        with self._sessions() as s:
            return [(r.id, r.slug, self._config(r), r.enabled) for r in s.scalars(select(_Server))]

    def load_into(self, manager: UpstreamManager) -> int:
        """Register every saved server with the manager (call at startup)."""
        with self._sessions() as s:
            rows = list(s.scalars(select(_Server)))
            for r in rows:
                try:
                    manager.add(r.id, r.slug, self._config(r), enabled=r.enabled)
                except ManagerError:
                    pass  # already registered
            return len(rows)
