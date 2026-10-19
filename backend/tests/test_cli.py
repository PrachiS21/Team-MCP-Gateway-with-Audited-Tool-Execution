import asyncio
import io
import re

import pytest
from fastmcp import Client

from gateway.auth.service import AuthService
from gateway.cli import main
from gateway.config import Settings
from gateway.registry.crypto import generate_key
from gateway.server import create_gateway
from tests.fixtures.fake_upstream import FakeUpstream
from tests.test_endpoint import Served, names

SECRET = "UP-SECRET-FROM-CLI"


@pytest.fixture
def settings(tmp_path):
    return Settings(secret_key=generate_key(), db_url=f"sqlite:///{tmp_path / 'gw.db'}",
                    jobs_db_url=f"sqlite:///{tmp_path / 'jobs.db'}", allow_loopback=True, sync_interval=0.2,
                    worker_interval=0.2)


class Run:
    def __init__(self, settings):
        self.settings = settings

    def __call__(self, *argv, stdin="", env=None, tty=False, prompt=None):
        out, err = io.StringIO(), io.StringIO()
        stdin_io = _Tty(stdin) if tty else io.StringIO(stdin)
        code = main(list(argv), settings=self.settings, out=out, err=err, stdin=stdin_io, env=env or {},
                    prompt=prompt or (lambda _: ""))
        return code, out.getvalue(), err.getvalue()


class _Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def cli(settings):
    return Run(settings)


# -- users and keys -----------------------------------------------------------------

def test_generate_secret_key_needs_no_settings(capsys):
    assert main(["generate-secret-key"]) == 0
    key = capsys.readouterr().out.strip()
    from cryptography.fernet import Fernet
    Fernet(key.encode())  # a valid key


def test_user_lifecycle_and_clear_errors(cli):
    assert cli("create-user", "a@example.com", "--role", "admin")[0] == 0
    code, _, err = cli("create-user", "a@example.com")
    assert code == 1 and "already exists" in err
    code, out, _ = cli("list-users")
    assert code == 0 and "a@example.com" in out and "admin" in out
    assert cli("deactivate-user", "a@example.com")[0] == 0 and "no" in cli("list-users")[1]
    code, _, err = cli("deactivate-user", "ghost@example.com")
    assert code == 1 and "no user" in err


def test_issue_list_and_revoke_a_key(cli, settings):
    cli("create-user", "a@example.com")
    code, out, _ = cli("issue-key", "a@example.com", "--name", "laptop")
    key = re.search(r"wmcp_[0-9a-f]{8}_[0-9a-f]{64}", out).group(0)
    auth = AuthService(settings.db_url)
    assert auth.verify(key).email == "a@example.com"

    code, listing, _ = cli("list-keys", "a@example.com")
    assert key not in listing and "laptop" in listing and "active" in listing  # metadata only

    key_id = key.split("_")[1]
    assert cli("revoke-key", key_id)[0] == 0
    assert auth.verify(key) is None
    assert "revoked" in cli("list-keys", "a@example.com")[1]
    assert cli("revoke-key", "deadbeef")[0] == 1


def test_quiet_issue_key_prints_only_the_key(cli):
    cli("create-user", "a@example.com")
    _, out, _ = cli("issue-key", "a@example.com", "--quiet")
    assert re.fullmatch(r"wmcp_[0-9a-f]{8}_[0-9a-f]{64}\n", out)


# -- servers: the credential never touches the command line ------------------------------

def test_add_server_reads_the_credential_from_env_and_never_prints_it(cli, settings):
    code, out, err = cli("add-server", "--slug", "shop", "--url", "http://10.0.0.5:8000/mcp",
                         "--credential-type", "bearer", "--credential-env", "SHOP_TOKEN", env={"SHOP_TOKEN": SECRET})
    assert code == 0, err
    listing = cli("list-servers")[1]
    assert "shop" in listing and "bearer" in listing
    assert SECRET not in out + err + listing
    import sqlite3
    raw = " ".join(str(r) for r in sqlite3.connect(settings.db_url.removeprefix("sqlite:///")).execute("select * from servers"))
    assert SECRET not in raw  # encrypted at rest


def test_credential_from_stdin_and_from_hidden_prompt(cli):
    assert cli("add-server", "--slug", "a", "--url", "http://10.0.0.5/mcp", "--credential-type", "bearer",
               "--credential-stdin", stdin="tok-1\n")[0] == 0
    asked = []
    code, _, _ = cli("add-server", "--slug", "b", "--url", "http://10.0.0.6/mcp", "--credential-type", "header",
                     "--credential-header", "X-Api-Key", tty=True, prompt=lambda q: asked.append(q) or "tok-2")
    assert code == 0 and asked and "hidden" in asked[0]
    assert "header (X-Api-Key)" in cli("list-servers")[1]


def test_missing_credential_is_an_error_when_nothing_can_supply_it(cli):
    code, _, err = cli("add-server", "--slug", "a", "--url", "http://10.0.0.5/mcp", "--credential-type", "bearer")
    assert code == 1 and "needs a credential" in err
    code, _, err = cli("add-server", "--slug", "a", "--url", "http://10.0.0.5/mcp", "--credential-type", "bearer",
                       "--credential-env", "NOT_SET")
    assert code == 1 and "NOT_SET" in err


def test_unsafe_urls_and_bad_slugs_are_refused(cli):
    code, _, err = cli("add-server", "--slug", "meta", "--url", "http://169.254.169.254/latest")
    assert code == 1 and "metadata" in err
    code, _, err = cli("add-server", "--slug", "Bad_Slug", "--url", "http://10.0.0.5/mcp")
    assert code == 1 and "invalid slug" in err
    assert cli("list-servers")[1].strip() == "(none)"


def test_enable_disable_remove_and_rotate_credential(cli):
    cli("add-server", "--slug", "a", "--url", "http://10.0.0.5/mcp", "--credential-type", "bearer",
        "--credential-env", "T", env={"T": "one"})
    assert cli("disable-server", "a")[0] == 0 and "no" in cli("list-servers")[1]
    assert cli("enable-server", "a")[0] == 0 and "yes" in cli("list-servers")[1]
    assert cli("set-credential", "a", "--credential-env", "T", env={"T": "two"})[0] == 0
    code, _, err = cli("set-credential", "a")  # nothing to read from
    assert code == 1 and "no credential" in err
    assert cli("remove-server", "a")[0] == 0 and cli("list-servers")[1].strip() == "(none)"
    assert cli("disable-server", "a")[0] == 1


def test_missing_secret_key_gives_a_helpful_message(monkeypatch):
    monkeypatch.delenv("GATEWAY_SECRET_KEY", raising=False)
    with pytest.raises(SystemExit) as e:
        main(["list-users"])
    assert "GATEWAY_SECRET_KEY" in str(e.value)


def test_audit_command_shows_rows_and_filters(cli, settings):
    from gateway.audit.service import AuditLog
    cli("create-user", "a@example.com")
    log = AuditLog(settings.db_url)
    uid = AuthService(settings.db_url).get_user_by_email("a@example.com").id
    log.record(outcome="success", user_id=uid, user_email="a@example.com", server_slug="woo", tool="list_orders", duration_ms=12)
    log.record(outcome="error", user_id=uid, user_email="a@example.com", server_slug="docs", tool="search", error_kind="timeout")
    _, out, _ = cli("audit")
    assert "woo.list_orders" in out and "docs.search" in out and "timeout" in out
    _, out, _ = cli("audit", "--server", "docs")
    assert "docs.search" in out and "woo.list_orders" not in out
    _, out, _ = cli("audit", "--user", "a@example.com", "--outcome", "success")
    assert "woo.list_orders" in out and "docs.search" not in out
    assert cli("audit", "--user", "ghost@example.com")[0] == 1


# -- live: the CLI changes a RUNNING gateway ------------------------------------------------

async def eventually(check, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            result = await check()
            if result:
                return result
        except Exception:  # noqa: BLE001
            pass
        if asyncio.get_event_loop().time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.1)


async def test_cli_changes_a_running_gateway(cli, settings):
    shop = FakeUpstream("shop", tools=("echo",), token=SECRET)
    await shop.start()
    gw = create_gateway(settings)  # starts with NO servers and NO users
    served = Served(gw)
    await served.start()
    try:
        cli("create-user", "bob@example.com")
        key = cli("issue-key", "bob@example.com", "--quiet")[1].strip()
        assert await names(served.url, key) == []

        # add: the running gateway picks it up, using the credential given through the environment
        assert cli("add-server", "--slug", "shop", "--url", shop.url, "--credential-type", "bearer",
                   "--credential-env", "T", "--timeout", "2", env={"T": SECRET})[0] == 0

        async def sees_shop():
            return await names(served.url, key) == ["shop__echo"]
        await eventually(sees_shop)
        async with Client(served.url, auth=key) as c:
            assert (await c.call_tool("shop__echo", {"text": "x"})).data == "shop:echo:x"

        # disable: tools disappear; enable: they return
        cli("disable-server", "shop")
        await eventually(lambda: _is(names(served.url, key), []))
        cli("enable-server", "shop")
        await eventually(sees_shop)

        # revoke: rejected immediately, no waiting for any sync
        cli("revoke-key", key.split("_")[1])
        with pytest.raises(Exception):
            await names(served.url, key)

        # remove: gone
        key2 = cli("issue-key", "bob@example.com", "--quiet")[1].strip()
        cli("remove-server", "shop")
        await eventually(lambda: _is(names(served.url, key2), []))
    finally:
        await served.stop()
        await shop.stop()


async def _is(awaitable, expected):
    return await awaitable == expected
