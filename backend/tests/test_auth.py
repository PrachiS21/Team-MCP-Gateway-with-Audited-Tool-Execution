import sqlite3

import pytest

from gateway.auth.keys import generate_key, hash_key, parse_key
from gateway.auth.service import AuthError, AuthService


@pytest.fixture
def db(tmp_path):
    return tmp_path / "auth.db"


@pytest.fixture
def auth(db):
    return AuthService(f"sqlite:///{db}")


# -- key format ---------------------------------------------------------------

def test_key_format_and_parse():
    key_id, key = generate_key()
    assert key.startswith("wmcp_") and parse_key(key) == key_id
    assert generate_key()[1] != key  # random each time


@pytest.mark.parametrize("bad", [
    "", "wmcp", "wmcp_abc", "xxxx_12345678_" + "a" * 64, "wmcp_1234567_" + "a" * 64,
    "wmcp_12345678_" + "a" * 63, "wmcp_zzzzzzzz_" + "a" * 64, "wmcp_12345678_" + "g" * 64,
    "wmcp_12345678_" + "a" * 64 + "_extra", "Bearer wmcp_12345678_" + "a" * 64,
])
def test_malformed_keys_are_rejected_without_a_db_lookup(auth, bad):
    assert parse_key(bad) is None
    assert auth.verify(bad) is None


# -- users --------------------------------------------------------------------

def test_create_user_normalises_email_and_rejects_duplicates_and_bad_input(auth):
    u = auth.create_user("  Alice@Example.COM ")
    assert u.email == "alice@example.com" and u.role == "member" and u.active
    with pytest.raises(AuthError):
        auth.create_user("alice@example.com")
    with pytest.raises(AuthError):
        auth.create_user("not-an-email")
    with pytest.raises(AuthError):
        auth.create_user("b@example.com", role="root")


# -- keys ---------------------------------------------------------------------

def test_issue_and_verify(auth):
    u = auth.create_user("a@example.com", "admin")
    issued = auth.issue_key(u.id, "laptop")
    who = auth.verify(issued.key)
    assert who is not None and who.id == u.id and who.is_admin


def test_only_the_hash_is_stored(auth, db):
    u = auth.create_user("a@example.com")
    issued = auth.issue_key(u.id)
    dump = " ".join(str(row) for row in sqlite3.connect(db).execute("select * from api_keys"))
    assert issued.key not in dump
    assert hash_key(issued.key) in dump
    # read APIs expose neither the key nor the hash
    assert issued.key not in repr(auth.list_keys(u.id))
    assert hash_key(issued.key) not in repr(auth.list_keys(u.id))


def test_wrong_secret_with_valid_id_is_rejected(auth):
    u = auth.create_user("a@example.com")
    issued = auth.issue_key(u.id)
    forged = f"wmcp_{issued.key_id}_{'0' * 64}"
    assert auth.verify(forged) is None


def test_revocation_takes_effect_on_the_next_verify(auth):
    u = auth.create_user("a@example.com")
    k1, k2 = auth.issue_key(u.id, "one"), auth.issue_key(u.id, "two")
    assert auth.verify(k1.key)
    auth.revoke_key(k1.key_id)
    assert auth.verify(k1.key) is None
    assert auth.verify(k2.key) is not None  # other keys of the same user unaffected
    first = auth.revoke_key(k1.key_id).revoked_at
    assert auth.revoke_key(k1.key_id).revoked_at == first  # idempotent
    with pytest.raises(AuthError):
        auth.revoke_key("deadbeef")


def test_deactivating_a_user_stops_all_their_keys(auth):
    u = auth.create_user("a@example.com")
    k = auth.issue_key(u.id)
    auth.set_active(u.id, False)
    assert auth.verify(k.key) is None
    with pytest.raises(AuthError):
        auth.issue_key(u.id)
    auth.set_active(u.id, True)
    assert auth.verify(k.key) is not None


def test_two_users_resolve_to_their_own_identity(auth):
    a, b = auth.create_user("a@example.com"), auth.create_user("b@example.com")
    ka, kb = auth.issue_key(a.id), auth.issue_key(b.id)
    assert auth.verify(ka.key).email == "a@example.com"
    assert auth.verify(kb.key).email == "b@example.com"


def test_last_used_is_recorded_but_not_written_on_every_call(auth, db):
    u = auth.create_user("a@example.com")
    k = auth.issue_key(u.id)
    assert auth.list_keys(u.id)[0].last_used_at is None
    auth.verify(k.key)
    first = auth.list_keys(u.id)[0].last_used_at
    assert first is not None
    auth.verify(k.key)  # within the same minute: no new write
    assert auth.list_keys(u.id)[0].last_used_at == first


def test_state_survives_a_new_service_instance(auth, db):
    u = auth.create_user("a@example.com")
    k = auth.issue_key(u.id)
    fresh = AuthService(f"sqlite:///{db}")
    assert fresh.verify(k.key).email == "a@example.com"


# -- bootstrap ----------------------------------------------------------------

def test_ensure_admin_is_idempotent_and_does_not_mint_a_key_every_start(auth):
    user, issued = auth.ensure_admin("root@example.com")
    assert user.is_admin and issued is not None and auth.verify(issued.key)
    user2, issued2 = auth.ensure_admin("root@example.com")
    assert user2.id == user.id and issued2 is None  # already has a usable key
    auth.revoke_key(issued.key_id)
    _, issued3 = auth.ensure_admin("root@example.com")  # locked out -> new key
    assert issued3 is not None and auth.verify(issued3.key)


def test_ensure_admin_promotes_and_reactivates_an_existing_user(auth):
    u = auth.create_user("root@example.com", "member")
    auth.set_active(u.id, False)
    user, _ = auth.ensure_admin("root@example.com")
    assert user.is_admin and user.active
