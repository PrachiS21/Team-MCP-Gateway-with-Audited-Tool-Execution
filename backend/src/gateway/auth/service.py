"""Users and API keys. The only code that turns a key into a user.

Plaintext keys exist only in the return value of issue_key(); the database and
every read method hold hashes or metadata. Revocation takes effect on the very
next verify() because verify() reads the database each time.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .keys import generate_key, hash_key, hashes_match, parse_key
from .models import ApiKeyRow, Base, UserRow

ROLES = ("admin", "member")
_LAST_USED_RESOLUTION = timedelta(minutes=1)  # don't write to the DB on every request


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class User:
    id: str
    email: str
    role: str
    active: bool
    created_at: datetime

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True)
class KeyInfo:
    """Metadata about a key. Contains neither the key nor its hash."""

    key_id: str
    user_id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass(frozen=True)
class IssuedKey:
    key_id: str
    key: str  # plaintext, shown to the user exactly once
    name: str


def _user(r: UserRow) -> User:
    return User(r.id, r.email, r.role, r.active, r.created_at)


def _info(r: ApiKeyRow) -> KeyInfo:
    return KeyInfo(r.id, r.user_id, r.name, r.created_at, r.last_used_at, r.revoked_at)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuthService:
    def __init__(self, db_url: str):
        self._engine = create_engine(db_url)
        Base.metadata.create_all(self._engine)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)

    # -- users ------------------------------------------------------------

    def create_user(self, email: str, role: str = "member") -> User:
        email = email.strip().lower()
        if "@" not in email:
            raise AuthError(f"{email!r} is not a valid email address")
        if role not in ROLES:
            raise AuthError(f"role must be one of {ROLES}")
        row = UserRow(id=uuid.uuid4().hex, email=email, role=role, active=True, created_at=_now())
        with self._sessions() as s:
            s.add(row)
            try:
                s.commit()
            except IntegrityError:
                raise AuthError(f"a user with email {email!r} already exists") from None
        return _user(row)

    def get_user(self, user_id: str) -> User:
        with self._sessions() as s:
            return _user(self._user_row(s, user_id))

    def get_user_by_email(self, email: str) -> User | None:
        with self._sessions() as s:
            row = s.scalars(select(UserRow).where(UserRow.email == email.strip().lower())).first()
            return _user(row) if row else None

    def list_users(self) -> list[User]:
        with self._sessions() as s:
            return [_user(r) for r in s.scalars(select(UserRow).order_by(UserRow.created_at))]

    def set_active(self, user_id: str, active: bool) -> User:
        """Deactivating a user makes all of their keys stop working immediately."""
        with self._sessions() as s:
            row = self._user_row(s, user_id)
            row.active = active
            s.commit()
            return _user(row)

    def _user_row(self, s, user_id: str) -> UserRow:
        row = s.get(UserRow, user_id)
        if row is None:
            raise AuthError(f"unknown user id {user_id!r}")
        return row

    # -- keys -------------------------------------------------------------

    def issue_key(self, user_id: str, name: str = "default") -> IssuedKey:
        with self._sessions() as s:
            user = self._user_row(s, user_id)
            if not user.active:
                raise AuthError("cannot issue a key for a deactivated user")
            for _ in range(5):  # key_id is 32 bits; retry on the (very unlikely) collision
                key_id, key = generate_key()
                if s.get(ApiKeyRow, key_id) is None:
                    break
            else:
                raise AuthError("could not generate a unique key id")
            s.add(ApiKeyRow(id=key_id, user_id=user_id, key_hash=hash_key(key), name=name, created_at=_now()))
            s.commit()
        return IssuedKey(key_id, key, name)

    def revoke_key(self, key_id: str) -> KeyInfo:
        """Idempotent: revoking an already revoked key keeps the original time."""
        with self._sessions() as s:
            row = s.get(ApiKeyRow, key_id)
            if row is None:
                raise AuthError(f"unknown key id {key_id!r}")
            if row.revoked_at is None:
                row.revoked_at = _now()
                s.commit()
            return _info(row)

    def list_keys(self, user_id: str) -> list[KeyInfo]:
        with self._sessions() as s:
            rows = s.scalars(select(ApiKeyRow).where(ApiKeyRow.user_id == user_id).order_by(ApiKeyRow.created_at))
            return [_info(r) for r in rows]

    def verify(self, key: str) -> User | None:
        """The only way a key becomes a user. None for anything not currently valid."""
        key_id = parse_key(key)
        if key_id is None:
            return None
        with self._sessions() as s:
            row = s.get(ApiKeyRow, key_id)
            if row is None or not hashes_match(row.key_hash, hash_key(key)):
                return None
            if row.revoked_at is not None:
                return None
            user = s.get(UserRow, row.user_id)
            if user is None or not user.active:
                return None
            now = _now()
            last = row.last_used_at
            if last is not None and last.tzinfo is None:  # SQLite drops tz info
                last = last.replace(tzinfo=timezone.utc)
            if last is None or now - last > _LAST_USED_RESOLUTION:
                row.last_used_at = now
                s.commit()
            return _user(user)

    # -- bootstrap --------------------------------------------------------

    def ensure_admin(self, email: str) -> tuple[User, IssuedKey | None]:
        """Make sure an active admin with this email exists. A key is issued (and
        returned) only when the user has no usable key yet, so restarts don't
        mint a new key every time."""
        user = self.get_user_by_email(email) or self.create_user(email, "admin")
        if user.role != "admin" or not user.active:
            with self._sessions() as s:
                row = self._user_row(s, user.id)
                row.role, row.active = "admin", True
                s.commit()
                user = _user(row)
        if any(not k.revoked for k in self.list_keys(user.id)):
            return user, None
        return user, self.issue_key(user.id, "bootstrap")
