"""The gateway against the REAL upstream servers, each running as its own process, as in the cluster.

  gst         our GST server   (upstreams/gst, started with its own uv environment)
  everything  the official MCP reference server (upstreams/everything, Node; run `npm ci` in that folder first)
  woo         our WooCommerce server against the REAL store, only when WOO_CONTRACT_URL/KEY/SECRET are set

Killing a server here is a real SIGKILL of its process, a slow call is a real slow tool, a wrong credential is
really rejected by the server. Nothing is simulated, so these tests catch what the fakes cannot: a server that
behaves differently from FastMCP (everything keeps sessions; FastMCP servers in stateless mode do not).
Clients reach the gateway over real HTTP with a real API key, exactly like Claude Code or MCP Inspector.
"""

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest
from fastmcp import Client

from gateway.config import Settings
from gateway.registry.crypto import generate_key
from gateway.server import create_gateway
from tests.test_endpoint import Served

ROOT = Path(__file__).resolve().parents[2]
EVERYTHING_JS = ROOT / "upstreams/everything/node_modules/@modelcontextprotocol/server-everything/dist/index.js"
GST_TOKEN = "gst-real-test-token"

needs_node = pytest.mark.skipif(not (shutil.which("node") and EVERYTHING_JS.exists()),
                                reason="needs node and `npm ci` in upstreams/everything")
needs_uv = pytest.mark.skipif(not shutil.which("uv"), reason="needs uv to run the gst server")
WOO_ENV = {k: os.environ.get(f"WOO_CONTRACT_{k}", "") for k in ("URL", "KEY", "SECRET")}
needs_store = pytest.mark.skipif(not all(WOO_ENV.values()), reason="set WOO_CONTRACT_URL/KEY/SECRET (the real store)")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RealServer:
    """One upstream MCP server as a separate OS process, on a fixed port so it can be killed and started again."""

    def __init__(self, name: str, cmd: list[str], env: dict[str, str], cwd: Path, port: int):
        self.name, self.cmd, self.cwd, self.port = name, cmd, cwd, port
        self.env = {**os.environ, **env}
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 60) -> None:
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} exited with code {self.proc.returncode} while starting")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"{self.name} did not open port {self.port}")

    def kill(self) -> None:
        """SIGKILL, like a pod being killed: no clean shutdown, sessions just vanish."""
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()


def gst_server(port: int, token: str = GST_TOKEN) -> RealServer:
    return RealServer("gst", ["uv", "run", "--quiet", "python", "-m", "gst_mcp"],
                      {"MCP_SERVER_TOKEN": token, "MCP_PORT": str(port), "STORE_STATE": "MH"}, ROOT / "upstreams/gst", port)


def everything_server(port: int) -> RealServer:
    return RealServer("everything", ["node", str(EVERYTHING_JS), "streamableHttp"], {"PORT": str(port)},
                      ROOT / "upstreams/everything", port)


class World:
    pass


@pytest.fixture
async def world(tmp_path):
    w = World()
    w.gst = gst_server(free_port())
    w.everything = everything_server(free_port())
    servers = [w.gst] + ([w.everything] if EVERYTHING_JS.exists() and shutil.which("node") else [])
    for s in servers:
        s.start()
    w.gw = create_gateway(Settings(secret_key=generate_key(), db_url=f"sqlite:///{tmp_path / 'gw.db'}",
                                   jobs_db_url=f"sqlite:///{tmp_path / 'jobs.db'}", allow_loopback=True))
    w.register = lambda **kw: register(w.gw, **kw)
    w.register(slug="gst", url=w.gst.url, token=GST_TOKEN)
    if w.everything in servers:
        w.register(slug="everything", url=w.everything.url, timeout=3)
    w.user = w.gw.auth.create_user("user-b@example.com")
    w.key = w.gw.auth.issue_key(w.user.id).key
    w.served = Served(w.gw)
    await w.served.start()
    yield w
    await w.served.stop()
    for s in servers:
        s.kill()


def register(gw, *, slug, url, token=None, timeout=5.0):
    v = gw.registry.create(name=slug, slug=slug, url=url, timeout=timeout,
                           credential_type="bearer" if token else "none", credential=token)
    gw.manager.add(v.id, slug, gw.registry.upstream_config(v.id), enabled=True)
    return v


async def tools(w) -> set[str]:
    async with Client(w.served.url, auth=w.key) as c:
        return {t.name for t in await c.list_tools()}


async def call(w, name, args):
    async with Client(w.served.url, auth=w.key) as c:
        r = await c.call_tool(name, args, raise_on_error=False)
    return r.is_error, (r.content[0].text if r.content else "")


async def statuses(w) -> dict[str, tuple[str, str | None]]:
    cat = await w.gw.manager.list_all_tools()
    return {s.slug: (s.status.value, s.error.kind.value if s.error else None) for s in cat.servers}


# -- listing and routing ------------------------------------------------------------------------------------------

@needs_uv
@needs_node
async def test_a_client_sees_both_real_servers_and_calls_are_routed_and_audited(world):
    names = await tools(world)
    assert {"gst__calculate_gst", "gst__gst_rates", "everything__get-sum", "everything__echo"} <= names
    err, text = await call(world, "everything__get-sum", {"a": 2, "b": 3})
    assert not err and "5" in text
    err, text = await call(world, "gst__calculate_gst", {"amounts": ["1180"]})
    assert not err and '"gst":"180.00"' in text.replace(" ", "")
    rows = {r.server_slug: r for r in world.gw.audit.query()}
    assert rows["gst"].outcome == rows["everything"].outcome == "success"
    assert {r.user_email for r in rows.values()} == {"user-b@example.com"}


@needs_uv
async def test_the_same_real_server_under_two_slugs_shows_the_collision_scheme(world):
    register(world.gw, slug="gst-copy", url=world.gst.url, token=GST_TOKEN)
    names = await tools(world)
    assert {"gst__calculate_gst", "gst-copy__calculate_gst"} <= names
    for name in ("gst__gst_rates", "gst-copy__gst_rates"):
        err, _ = await call(world, name, {})
        assert not err
    assert {r.server_slug for r in world.gw.audit.query()} == {"gst", "gst-copy"}


# -- a dead server ----------------------------------------------------------------------------------------------------

@needs_uv
@needs_node
async def test_killing_the_everything_process_leaves_gst_working_and_it_recovers_after_restart(world):
    assert (await call(world, "everything__get-sum", {"a": 1, "b": 1}))[0] is False  # opens a session first
    world.everything.kill()

    names = await tools(world)  # listing still works, minus the dead server
    assert "gst__calculate_gst" in names and not any(n.startswith("everything__") for n in names)
    t0 = time.monotonic()
    err, text = await call(world, "everything__get-sum", {"a": 1, "b": 1})
    assert err and "unreachable" in text.lower() and time.monotonic() - t0 < 3  # a clear, fast error
    assert (await call(world, "gst__gst_rates", {}))[0] is False
    assert (await statuses(world))["everything"] == ("unreachable", "unreachable")

    world.everything.start()  # "the pod is back" on the same address; its old sessions are gone
    err, text = await call(world, "everything__get-sum", {"a": 2, "b": 2})
    assert not err and "4" in text, text  # the FIRST call after the restart must already work
    assert (await statuses(world))["everything"] == ("connected", None)


@needs_uv
async def test_killing_the_gst_process_and_restarting_it(world):
    world.gst.kill()
    err, text = await call(world, "gst__gst_rates", {})
    assert err and "unreachable" in text.lower()
    world.gst.start()
    err, _ = await call(world, "gst__gst_rates", {})
    assert not err


# -- a slow server ----------------------------------------------------------------------------------------------------

@needs_uv
@needs_node
async def test_a_really_slow_tool_times_out_without_holding_up_the_other_server(world):
    """everything's timeout is 3 s; its long-running tool takes 6 s."""
    async def slow():
        t0 = time.monotonic()
        res = await call(world, "everything__trigger-long-running-operation", {"duration": 6, "steps": 2})
        return res, time.monotonic() - t0

    async def fast():
        await asyncio.sleep(0.5)  # starts while the slow call is in flight
        t0 = time.monotonic()
        res = await call(world, "gst__gst_rates", {})
        return res, time.monotonic() - t0

    ((slow_err, slow_text), slow_s), ((fast_err, _), fast_s) = await asyncio.gather(slow(), fast())
    assert slow_err and "did not answer in time" in slow_text and slow_s < 5
    assert not fast_err and fast_s < 2
    assert [r.error_kind for r in world.gw.audit.query(server_slug="everything")] == ["timeout"]
    err, text = await call(world, "everything__get-sum", {"a": 3, "b": 4})  # and it is usable again afterwards
    assert not err and "7" in text, text


# -- telling the causes apart -------------------------------------------------------------------------------------------

@needs_uv
async def test_each_kind_of_failure_is_reported_as_what_it_is(world):
    register(world.gw, slug="wrong-token", url=world.gst.url, token="not-the-gst-token")
    register(world.gw, slug="nothing-listening", url=f"http://127.0.0.1:{free_port()}/mcp")
    register(world.gw, slug="no-such-host", url="http://no-such-host.invalid:8000/mcp")
    got = await statuses(world)
    assert got["gst"] == ("connected", None)
    assert got["wrong-token"] == ("unreachable", "auth_failed")  # the server answered, it refused our credential
    assert got["nothing-listening"] == ("unreachable", "unreachable")  # nothing at that address
    assert got["no-such-host"] == ("unreachable", "unreachable")  # name does not resolve
    err, text = await call(world, "wrong-token__gst_rates", {})
    assert err and "could not authenticate" in text


# -- the real store -----------------------------------------------------------------------------------------------------

@needs_uv
@needs_store
async def test_woo_against_the_real_store_and_the_cross_server_question(world):
    """'How much GST is in today's orders?': woo list_orders, then gst calculate_gst on their totals."""
    woo = RealServer("woo", ["uv", "run", "--quiet", "python", "-m", "woo_mcp"],
                     {"WOO_BASE_URL": WOO_ENV["URL"], "WOO_CONSUMER_KEY": WOO_ENV["KEY"],
                      "WOO_CONSUMER_SECRET": WOO_ENV["SECRET"], "MCP_SERVER_TOKEN": "woo-real-token",
                      "MCP_PORT": str(port := free_port())}, ROOT / "woo-mcp", port)
    woo.start()
    try:
        register(world.gw, slug="woo", url=woo.url, token="woo-real-token", timeout=15)
        assert "woo__list_orders" in await tools(world)
        async with Client(world.served.url, auth=world.key) as c:
            orders = (await c.call_tool("woo__list_orders", {"today": True, "per_page": 50})).structured_content
            totals = [o["total"] for o in orders["items"]] or ["0"]
            gst = (await c.call_tool("gst__calculate_gst", {"amounts": totals})).structured_content
        assert gst["totals"]["total"] == f"{sum(float(t) for t in totals):.2f}"
    finally:
        woo.kill()
