"""Tests against the DEPLOYED system: real pods, real Ingress, real NetworkPolicies, real pod kills. Run on the HP.

Skipped unless these are set (scripts/cluster-test.sh sets the first and asks for the second):
    CLUSTER_BASE        the platform URL, e.g. http://app.192.168.1.11.sslip.io
    CLUSTER_ADMIN_KEY   an admin API key (printed once in the gateway's first log)
The kubectl tests also need kubectl pointed at the local cluster (context k3d-wc).

Every request goes through the public Ingress, exactly like Claude Code or the dashboard would.
The suite creates its own member user (cluster-test-<random>@example.com) and leaves the system as it found it:
servers it disables are re-enabled, pods it stops are started again.
"""

import base64
import json
import os
import secrets
import shutil
import subprocess
import time

import httpx
import pytest
from fastmcp import Client

BASE = os.environ.get("CLUSTER_BASE", "").rstrip("/")
ADMIN_KEY = os.environ.get("CLUSTER_ADMIN_KEY", "")
PLATFORM_NS = os.environ.get("PLATFORM_NS", "platform")
STORE_NS = os.environ.get("STORE_NS", "store-main")

pytestmark = pytest.mark.skipif(not (BASE and ADMIN_KEY), reason="set CLUSTER_BASE and CLUSTER_ADMIN_KEY")


def _kubectl_ready() -> bool:
    if not shutil.which("kubectl"):
        return False
    ctx = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True).stdout.strip()
    return ctx == "k3d-" + os.environ.get("CLUSTER", "wc")


needs_kubectl = pytest.mark.skipif(not _kubectl_ready(), reason="needs kubectl pointed at the local k3d cluster")


def kubectl(*args: str, check: bool = True, timeout: float = 180) -> str:
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


# -- fixtures: an admin dashboard session and a fresh member with their own key -----------------------------------------

@pytest.fixture(scope="module")
def admin():
    h = httpx.Client(base_url=BASE, timeout=60)
    r = h.post("/api/login", json={"api_key": ADMIN_KEY})
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    assert r.json()["user"]["role"] == "admin", "CLUSTER_ADMIN_KEY must belong to an admin"
    yield h
    h.close()


@pytest.fixture(scope="module")
def member(admin):
    email = f"cluster-test-{secrets.token_hex(3)}@example.com"
    r = admin.post("/api/users", json={"email": email, "role": "member"})
    assert r.status_code == 201, r.text
    body = r.json()
    yield {"email": email, "id": body["user"]["id"], "key": body["api_key"], "key_id": body["key_id"]}
    admin.post(f"/api/users/{body['user']['id']}/deactivate", json={})  # tidy up: the test user cannot be used again


def servers(admin) -> dict[str, dict]:
    r = admin.get("/api/servers")
    assert r.status_code == 200, r.text
    return {s["slug"]: s for s in r.json()["servers"]}


async def tool_names(key: str) -> set[str]:
    async with Client(f"{BASE}/mcp", auth=key) as c:
        return {t.name for t in await c.list_tools()}


async def call(key: str, name: str, args: dict):
    async with Client(f"{BASE}/mcp", auth=key, timeout=120) as c:
        r = await c.call_tool(name, args, raise_on_error=False)
    text = r.content[0].text if r.content else ""
    return r.is_error, (r.structured_content if not r.is_error and r.structured_content is not None else text)


def wait_for(predicate, timeout: float = 120, every: float = 2, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for: {what}")


# -- the three real servers through the real Ingress -------------------------------------------------------------------

async def test_a_member_sees_the_tools_of_all_three_servers(member):
    names = await tool_names(member["key"])
    for expected in ("woo__list_orders", "woo__create_product", "woo__update_order_status",
                     "gst__calculate_gst", "everything__get-sum"):
        assert expected in names, f"{expected} missing from {sorted(names)}"
    assert "woo__delete_product" not in names  # internal tool stays hidden


def test_the_dashboard_shows_all_three_connected(admin):
    got = servers(admin)
    for slug in ("woo", "gst", "everything"):
        assert got[slug]["status"] == "connected", (slug, got[slug].get("error"))
        assert got[slug]["tools"]
    woo = got["woo"]
    assert woo["has_credential"] is True and woo["credential_type"] == "bearer"
    assert "credential" not in woo  # only facts ABOUT the credential (has_/_type/_header), never the credential itself
    if _kubectl_ready():  # and the real token appears nowhere in what the dashboard receives
        token = base64.b64decode(kubectl("-n", PLATFORM_NS, "get", "secret", "gateway-secrets",
                                         "-o", "jsonpath={.data.WOO_MCP_TOKEN}")).decode()
        assert token and token not in json.dumps(got)


async def test_each_server_answers_a_call(member):
    err, products = await call(member["key"], "woo__list_products", {"per_page": 5})
    assert not err and "items" in products, products
    err, rates = await call(member["key"], "gst__gst_rates", {})
    assert not err and rates["store_state"], rates
    err, text = await call(member["key"], "everything__get-sum", {"a": 20, "b": 22})
    assert not err and "42" in str(text), text


async def test_the_cross_server_question_gst_in_todays_orders(member):
    err, orders = await call(member["key"], "woo__list_orders", {"today": True, "per_page": 50})
    assert not err, orders
    totals = [o["total"] for o in orders["items"]] or ["0"]
    err, gst = await call(member["key"], "gst__calculate_gst", {"amounts": totals})
    assert not err and gst["totals"]["total"] == f"{sum(float(t) for t in totals):.2f}", gst


def test_the_calls_are_in_the_audit_log_under_the_member(admin, member):
    entries = admin.get("/api/audit", params={"user": member["email"], "limit": 200}).json()["entries"]
    called = {(e["server"], e["tool"]) for e in entries if e["outcome"] == "success"}
    assert {("woo", "list_orders"), ("gst", "calculate_gst"), ("everything", "get-sum")} <= called, called


# -- what is public and what is not --------------------------------------------------------------------------------------

def test_only_mcp_and_api_are_routed_from_outside():
    with httpx.Client(timeout=15) as h:
        assert h.get(f"{BASE}/healthz").status_code == 404  # health endpoints are for the kubelet only
        assert h.get(f"{BASE}/api/me").status_code == 401  # routed, but needs a login
        bad = h.post(f"{BASE}/mcp", json={}, headers={"Authorization": "Bearer wmcp_00000000_" + "0" * 64})
        assert bad.status_code == 401


@needs_kubectl
def test_network_policies_block_everything_but_the_allowed_paths():
    """A throwaway pod in another namespace tries each internal service. Reaching the storefront through Traefik is
    the control: it proves the pod has a working network, so the refusals come from the NetworkPolicies."""
    # TCP reachability only (nc -z): that is exactly what a NetworkPolicy decides, and it avoids counting an HTTP error
    # (Traefik answers 404 to a request with no host name) as "blocked".
    targets = {
        "control_traefik": ("traefik.kube-system.svc.cluster.local", 80),
        "woo_mcp": (f"woo-mcp.{STORE_NS}.svc.cluster.local", 8000),
        "mariadb": (f"mariadb.{STORE_NS}.svc.cluster.local", 3306),
        "wordpress_direct": (f"wordpress.{STORE_NS}.svc.cluster.local", 80),
        "gateway_direct": (f"gateway.{PLATFORM_NS}.svc.cluster.local", 8000),
        "gst": (f"gst.{PLATFORM_NS}.svc.cluster.local", 8000),
        "everything": (f"everything.{PLATFORM_NS}.svc.cluster.local", 3001),
    }
    script = "; ".join(f'if nc -z -w 5 {host} {port}; then echo "{name}=open"; else echo "{name}=blocked"; fi'
                       for name, (host, port) in targets.items())
    # Not `kubectl run -i`: attaching after the pod starts can lose its first lines. Run it, wait, then read the log.
    pod = f"nettest-{secrets.token_hex(3)}"
    kubectl("run", pod, "-n", "default", "--restart=Never", "--image=busybox:1.36", "--", "sh", "-c", script)
    try:
        kubectl("-n", "default", "wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{pod}", "--timeout=180s",
                timeout=200)
        out = kubectl("-n", "default", "logs", pod)
    finally:
        kubectl("-n", "default", "delete", "pod", pod, "--wait=false", check=False)
    got = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    assert got.get("control_traefik") == "open", f"the test pod cannot reach anything, so it proves nothing: {got}"
    for name in targets:
        if name != "control_traefik":
            assert got.get(name) == "blocked", f"{name} is reachable from another namespace: {got}"


# -- admin control ----------------------------------------------------------------------------------------------------------

async def test_disabling_a_server_hides_its_tools_at_once_and_enabling_brings_them_back(admin, member):
    sid = servers(admin)["everything"]["id"]
    try:
        assert admin.post(f"/api/servers/{sid}/disable", json={}).status_code == 200
        assert not any(n.startswith("everything__") for n in await tool_names(member["key"]))
        assert servers(admin)["everything"]["status"] == "disabled"
    finally:
        assert admin.post(f"/api/servers/{sid}/enable", json={}).status_code == 200
    assert "everything__get-sum" in await tool_names(member["key"])


# -- a slow server ------------------------------------------------------------------------------------------------------------

async def test_a_slow_tool_times_out_with_a_clear_error_and_the_server_stays_usable(admin, member):
    timeout = servers(admin)["everything"]["timeout"]
    t0 = time.monotonic()
    err, text = await call(member["key"], "everything__trigger-long-running-operation",
                           {"duration": int(timeout) + 5, "steps": 2})
    took = time.monotonic() - t0
    assert err and "did not answer in time" in text and took < timeout + 4, (text, took)
    err, text = await call(member["key"], "everything__get-sum", {"a": 1, "b": 2})
    assert not err and "3" in str(text), text


# -- killing pods ---------------------------------------------------------------------------------------------------------

def _scale(ns: str, deploy: str, replicas: int) -> None:
    kubectl("-n", ns, "scale", f"deployment/{deploy}", f"--replicas={replicas}")
    if replicas:
        kubectl("-n", ns, "rollout", "status", f"deployment/{deploy}", "--timeout=180s")
    else:
        kubectl("-n", ns, "wait", "--for=delete", "pod", "-l", f"app.kubernetes.io/name={deploy}", "--timeout=120s",
                check=False)


@needs_kubectl
@pytest.mark.parametrize("ns, deploy, slug, probe", [
    (PLATFORM_NS, "everything", "everything", ("everything__get-sum", {"a": 1, "b": 1})),
    (STORE_NS, "woo-mcp", "woo", ("woo__list_products", {"per_page": 1})),
])
async def test_with_a_server_down_the_others_keep_working_and_the_dashboard_says_unreachable(
        admin, member, ns, deploy, slug, probe):
    """Scaled to zero, so the outage lasts as long as the test needs (a deleted pod comes back within seconds)."""
    other = "gst__gst_rates"
    try:
        _scale(ns, deploy, 0)
        err, text = await call(member["key"], *probe)
        assert err and "unreachable" in text.lower(), text
        assert (await call(member["key"], other, {}))[0] is False  # the others are unaffected
        names = await tool_names(member["key"])
        assert "gst__calculate_gst" in names and not any(n.startswith(f"{slug}__") for n in names)
        state = servers(admin)[slug]
        assert state["status"] == "unreachable" and state["error"]["kind"] in ("unreachable", "timeout"), state
    finally:
        _scale(ns, deploy, 1)
    wait_for(lambda: servers(admin)[slug]["status"] == "connected", what=f"{slug} connected again")
    assert (await call(member["key"], *probe))[0] is False


@needs_kubectl
@pytest.mark.parametrize("ns, deploy, probe", [
    (PLATFORM_NS, "everything", ("everything__get-sum", {"a": 2, "b": 2})),
    (STORE_NS, "woo-mcp", ("woo__list_products", {"per_page": 1})),
])
async def test_after_a_pod_is_deleted_and_replaced_the_first_call_works(member, ns, deploy, probe):
    """The demo's 'kill the pod'. The gateway held a live session with the old pod; it must not keep using it."""
    assert (await call(member["key"], *probe))[0] is False  # make sure a session to the old pod exists
    kubectl("-n", ns, "delete", "pod", "-l", f"app.kubernetes.io/name={deploy}", "--wait=true")
    kubectl("-n", ns, "rollout", "status", f"deployment/{deploy}", "--timeout=180s")
    kubectl("-n", ns, "wait", "--for=condition=Ready", "pod", "-l", f"app.kubernetes.io/name={deploy}", "--timeout=120s")
    err, text = await call(member["key"], *probe)
    assert not err, f"first call after the pod was replaced failed: {text}"


# -- revocation (last: it ends the member's access) -----------------------------------------------------------------------

async def test_a_revoked_key_is_rejected_on_the_next_call(admin, member):
    assert await tool_names(member["key"])
    assert admin.delete(f"/api/keys/{member['key_id']}").status_code == 200
    with pytest.raises(Exception):
        await tool_names(member["key"])
    entries = admin.get("/api/audit", params={"outcome": "auth_failed", "limit": 50}).json()["entries"]
    assert any(e["key_id"] == member["key_id"] for e in entries)
