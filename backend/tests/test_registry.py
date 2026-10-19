import sqlite3

import pytest

from gateway.mcp.manager import UpstreamManager
from gateway.registry.crypto import SecretBox, SecretBoxError, generate_key
from gateway.registry.registry import RegistryError, ServerRegistry
from gateway.registry.url_policy import UrlPolicyError, validate_upstream_url
from tests.fixtures.fake_upstream import FakeUpstream

KEY = generate_key()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "gw.db"


def make(db, key=KEY, allow_loopback=True):
    return ServerRegistry(f"sqlite:///{db}", SecretBox(key), allow_loopback=allow_loopback)


# -- credentials ------------------------------------------------------------

def test_credential_is_never_returned_and_is_encrypted_in_the_file(db):
    reg = make(db)
    v = reg.create(name="Woo", slug="woo", url="http://127.0.0.1:9/mcp",
                   credential_type="bearer", credential="TOP-SECRET")
    assert v.has_credential is True
    assert "TOP-SECRET" not in repr(v) and "TOP-SECRET" not in repr(reg.list()) and "TOP-SECRET" not in repr(reg.get(v.id))
    raw = sqlite3.connect(db).execute("select credential_enc from servers").fetchone()[0]
    assert raw and "TOP-SECRET" not in raw
    assert reg.upstream_config(v.id).credential == "TOP-SECRET"  # the one decrypting path


def test_wrong_key_cannot_read_credentials(db):
    reg = make(db)
    v = reg.create(name="W", slug="w", url="http://127.0.0.1:9/mcp", credential_type="bearer", credential="s")
    other = make(db, key=generate_key())
    with pytest.raises(SecretBoxError):
        other.upstream_config(v.id)


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GATEWAY_SECRET_KEY", raising=False)
    with pytest.raises(SecretBoxError):
        SecretBox()


def test_header_credential_maps_to_raw_header(db):
    reg = make(db)
    v = reg.create(name="H", slug="h", url="http://127.0.0.1:9/mcp",
                   credential_type="header", credential="abc", credential_header="X-Api-Key")
    cfg = reg.upstream_config(v.id)
    assert (cfg.credential, cfg.credential_header, cfg.credential_scheme) == ("abc", "X-Api-Key", None)


# -- CRUD ---------------------------------------------------------------------

def test_persistence_across_instances_and_crud(db):
    reg = make(db)
    v = reg.create(name="A", slug="a", url="http://127.0.0.1:9/mcp")
    assert [s.slug for s in make(db).list()] == ["a"]  # a fresh instance sees it
    reg.set_enabled(v.id, False)
    assert make(db).get(v.id).enabled is False
    reg.delete(v.id)
    assert reg.list() == []
    with pytest.raises(RegistryError):
        reg.get(v.id)


def test_update_keeps_replaces_or_removes_credential(db):
    reg = make(db)
    v = reg.create(name="A", slug="a", url="http://127.0.0.1:9/mcp", credential_type="bearer", credential="one")
    reg.update(v.id, name="Renamed")  # credential left out -> kept
    assert reg.upstream_config(v.id).credential == "one" and reg.get(v.id).name == "Renamed"
    reg.update(v.id, credential="two")
    assert reg.upstream_config(v.id).credential == "two"
    reg.update(v.id, credential_type="none", credential=None)
    assert reg.get(v.id).has_credential is False


def test_validation_errors(db):
    reg = make(db)
    ok = dict(name="x", url="http://127.0.0.1:9/mcp")
    for kw in (dict(slug="Bad_Slug"), dict(slug="a__b"),
               dict(slug="s", credential_type="bearer"),                  # needs a credential
               dict(slug="s", credential_type="none", credential="x"),    # credential without type
               dict(slug="s", credential_type="header", credential="x"),  # header name missing
               dict(slug="s", credential_type="weird", credential="x")):
        with pytest.raises(RegistryError):
            reg.create(**ok, **kw)
    reg.create(**ok, slug="dup")
    with pytest.raises(RegistryError):
        reg.create(**ok, slug="dup")


# -- URL policy -----------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "ftp://example.com/mcp",
    "file:///etc/passwd",
    "http://169.254.169.254/latest/meta-data",
    "http://169.254.1.1/mcp",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://[fe80::1]/mcp",
    "http://[::ffff:169.254.169.254]/mcp",
    "http://0.0.0.0/mcp",
    "http:///nohost",
])
def test_always_blocked_urls(url):
    with pytest.raises(UrlPolicyError):
        validate_upstream_url(url, allow_loopback=True)


def test_loopback_only_in_dev():
    for url in ("http://127.0.0.1:8000/mcp", "http://localhost:8000/mcp", "http://[::1]:8000/mcp"):
        with pytest.raises(UrlPolicyError):
            validate_upstream_url(url, allow_loopback=False)
        validate_upstream_url(url, allow_loopback=True)


def test_in_cluster_addresses_are_allowed():
    validate_upstream_url("http://10.43.0.12:8000/mcp")
    validate_upstream_url("http://woo-mcp.default.svc.cluster.local:8000/mcp")  # does not resolve here -> allowed


def test_registry_applies_the_policy(db):
    reg = make(db, allow_loopback=False)
    with pytest.raises(UrlPolicyError):
        reg.create(name="x", slug="x", url="http://169.254.169.254/")
    with pytest.raises(UrlPolicyError):
        reg.create(name="x", slug="x", url="http://127.0.0.1:8000/mcp")


# -- registry -> manager -> real upstream ---------------------------------------

async def test_load_into_manager_end_to_end(db):
    secure = FakeUpstream("sec", tools=("echo",), token="tok")
    header = FakeUpstream("hdr", tools=("ping",))
    await secure.start()
    await header.start()
    m = UpstreamManager()
    try:
        reg = make(db)
        reg.create(name="Secure", slug="sec", url=secure.url, credential_type="bearer", credential="tok", timeout=2)
        off = reg.create(name="Off", slug="off", url=header.url, enabled=False)
        assert reg.load_into(m) == 2
        cat = await m.list_all_tools()
        assert [t.name for t in cat.tools] == ["sec__echo"]  # disabled one contributes nothing
        assert {s.slug: s.status.value for s in cat.servers}["off"] == "disabled"
        assert (await m.call_tool("sec__echo", {"text": "hi"})).content.data == "sec:echo:hi"
        assert off.enabled is False
    finally:
        await m.close()
        await secure.stop()
        await header.stop()
