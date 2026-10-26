"""Tests for the cluster scripts, run on any machine: docker, k3d, kubectl, helm, rsync, ip and hostname are replaced by
stub.py (see its docstring), while openssl, base64 and the shell utilities are the real ones.

Run from the project root:   scripts/tests/run.sh
What this proves: the scripts make the right calls, in the right order, with the right safety checks, and keep
secrets off command lines and out of output. What it cannot prove: that the real tools behave as the stubs do.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

SCRIPTS = Path(__file__).resolve().parents[1]
STUB = Path(__file__).with_name("stub.py")
TOOLS = ["kubectl", "docker", "k3d", "helm", "rsync", "ip", "hostname", "curl", "claude"]
IP_LINE = "1.1.1.1 via 192.168.1.1 dev wlp2s0 src 192.168.1.11 uid 1000"


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.state = tmp / "state"
        self.log = tmp / "calls.log"
        self.tmpdir = tmp / "tmpdir"
        for d in (self.bin, self.state, self.tmpdir):
            d.mkdir()
        self.log.touch()
        for tool in TOOLS:
            p = self.bin / tool
            p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB}" {tool} "$@"\n')
            p.chmod(0o755)
        self.vars = {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "STUB_LOG": str(self.log),
                     "STUB_STATE": str(self.state), "TMPDIR": str(self.tmpdir), "STUB_IP_LINE": IP_LINE,
                     "STUB_IMAGES": "gateway:dev woo-mcp:dev gst-mcp:dev everything-mcp:dev dashboard:dev", "HOME": str(tmp)}

    def run(self, script, *args, stdin="", **extra):
        env = {**self.vars, **{k: v for k, v in extra.items() if v is not None}}
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
        return subprocess.run([str(SCRIPTS / script), *args], input=stdin, capture_output=True, text=True, env=env)

    def calls(self, tool=None):
        lines = self.log.read_text().splitlines()
        return [line for line in lines if tool is None or line.split(" ", 1)[0] == tool]

    def secret(self, ns, name):
        folder = self.state / ns / name
        return {p.name: p.read_text() for p in folder.iterdir()} if folder.is_dir() else None

    def add_secret(self, ns, name, **values):
        folder = self.state / ns / name
        folder.mkdir(parents=True)
        for k, v in values.items():
            (folder / k).write_text(v)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def all_values(env):
    return [v for ns in env.state.iterdir() for s in ns.iterdir() for v in env.secret(ns.name, s.name).values()]


# -- bootstrap-secrets.sh ---------------------------------------------------------------------------------------

def test_bootstrap_creates_every_secret_the_charts_need_with_strong_random_values(env):
    r = env.run("bootstrap-secrets.sh")
    assert r.returncode == 0, r.stderr
    db, wp = env.secret("store-main", "store-db"), env.secret("store-main", "store-wordpress")
    mcp, gw = env.secret("store-main", "store-mcp"), env.secret("platform", "gateway-secrets")
    assert set(db) == {"DB_PASSWORD", "DB_ROOT_PASSWORD"} and all(re.fullmatch(r"[0-9a-f]{32}", v) for v in db.values())
    assert set(wp) == {"WP_ADMIN_PASSWORD", "WC_CONSUMER_KEY", "WC_CONSUMER_SECRET"}
    assert re.fullmatch(r"ck_[0-9a-f]{40}", wp["WC_CONSUMER_KEY"]) and re.fullmatch(r"cs_[0-9a-f]{40}", wp["WC_CONSUMER_SECRET"])
    assert set(gw) == {"GATEWAY_SECRET_KEY", "WOO_MCP_TOKEN", "GST_MCP_TOKEN"}
    assert gw["GST_MCP_TOKEN"] != gw["WOO_MCP_TOKEN"] and len(gw["GST_MCP_TOKEN"]) == 48
    Fernet(gw["GATEWAY_SECRET_KEY"].encode())  # raises if it is not a valid key for the gateway's encryption
    assert gw["WOO_MCP_TOKEN"] == mcp["MCP_SERVER_TOKEN"] and len(mcp["MCP_SERVER_TOKEN"]) == 48
    assert len(set(all_values(env))) == len(all_values(env)) - 1  # all different, except the one shared token


def test_bootstrap_never_shows_a_secret_on_a_command_line_or_in_its_output(env):
    r = env.run("bootstrap-secrets.sh")
    seen = env.log.read_text() + r.stdout + r.stderr
    for value in all_values(env):
        assert value not in seen
    assert list(env.tmpdir.iterdir()) == []  # its temporary files are gone


def test_bootstrap_is_safe_to_run_again_and_changes_nothing(env):
    env.run("bootstrap-secrets.sh")
    before = sorted(all_values(env))
    r = env.run("bootstrap-secrets.sh")
    assert r.returncode == 0 and sorted(all_values(env)) == before
    assert r.stdout.count("already exists") == 4


def test_bootstrap_gives_the_gateway_the_token_the_store_already_has(env):
    env.add_secret("store-main", "store-mcp", MCP_SERVER_TOKEN="existing-token")
    assert env.run("bootstrap-secrets.sh").returncode == 0
    assert env.secret("platform", "gateway-secrets")["WOO_MCP_TOKEN"] == "existing-token"


def test_bootstrap_refuses_mismatched_tokens_instead_of_overwriting(env):
    env.add_secret("store-main", "store-mcp", MCP_SERVER_TOKEN="store-token")
    env.add_secret("platform", "gateway-secrets", GATEWAY_SECRET_KEY="k", WOO_MCP_TOKEN="other-token")
    r = env.run("bootstrap-secrets.sh")
    assert r.returncode != 0 and "different token" in r.stderr
    assert env.secret("platform", "gateway-secrets")["WOO_MCP_TOKEN"] == "other-token"
    assert "store-token" not in r.stderr + r.stdout and "other-token" not in r.stderr + r.stdout


def test_bootstrap_refuses_any_cluster_but_the_local_one(env):
    r = env.run("bootstrap-secrets.sh", STUB_CONTEXT="production")
    assert r.returncode != 0 and "production" in r.stderr
    assert not [c for c in env.calls("kubectl") if "create" in c or "apply" in c]


# -- deploy-local.sh -------------------------------------------------------------------------------------------

def secrets_present(env):
    for ns, name in [("store-main", "store-db"), ("store-main", "store-wordpress"), ("store-main", "store-mcp"),
                     ("platform", "gateway-secrets")]:
        env.add_secret(ns, name, X="x")


def test_deploy_installs_the_store_then_the_platform_with_hosts_built_from_the_detected_address(env):
    secrets_present(env)
    r = env.run("deploy-local.sh")
    assert r.returncode == 0, r.stderr
    store, platform = [c for c in env.calls("helm") if " --install " in c]
    assert store.startswith("helm upgrade --install store ") and platform.startswith("helm upgrade --install platform ")
    assert "-n store-main" in store and "values-local.yaml" in store and "--set host=shop.192.168.1.11.sslip.io" in store
    assert "-n platform" in platform and "--set host=app.192.168.1.11.sslip.io" in platform
    for call in (store, platform):
        assert "--wait" in call
        assert "--atomic" not in call and "--rollback-on-failure" not in call  # first install: nothing to roll back to
    assert "--wait-for-jobs" in store  # the setup Job must finish before the release counts as installed


@pytest.mark.parametrize("version, flag", [("v4.3.1", "--rollback-on-failure"), ("v3.17.0", "--atomic")])
def test_deploy_rolls_back_failed_upgrades_with_the_flag_this_helm_understands(env, version, flag):
    secrets_present(env)
    assert env.run("deploy-local.sh", STUB_HELM_RELEASES="store platform", STUB_HELM_VERSION=version).returncode == 0
    for call in [c for c in env.calls("helm") if " --install " in c]:
        assert flag in call


def test_deploy_uses_host_ip_when_given_and_falls_back_to_hostname(env):
    secrets_present(env)
    env.run("deploy-local.sh", HOST_IP="10.0.0.7")
    assert "host=shop.10.0.0.7.sslip.io" in [c for c in env.calls("helm") if " --install " in c][0]
    env.log.write_text("")
    env.run("deploy-local.sh", STUB_IP_LINE=None, STUB_HOSTNAME_I="172.16.0.9 fe80::1")
    assert "host=shop.172.16.0.9.sslip.io" in [c for c in env.calls("helm") if " --install " in c][0]


@pytest.mark.parametrize("bad", ["", "not-an-ip", "192.168.1", "192.168.1.11; rm -rf ~", "192.168.1.11\nevil"])
def test_deploy_stops_when_the_address_is_unknown_or_malformed(env, bad):
    secrets_present(env)
    r = env.run("deploy-local.sh", HOST_IP=bad or None, STUB_IP_LINE=None)
    assert r.returncode != 0 and "HOST_IP" in r.stderr and not env.calls("helm")


def test_deploy_stops_before_helm_when_an_image_or_a_secret_is_missing(env):
    secrets_present(env)
    r = env.run("deploy-local.sh", STUB_IMAGES="gateway:dev woo-mcp:dev gst-mcp:dev everything-mcp:dev")
    assert r.returncode != 0 and "dashboard:dev" in r.stderr and "build-images" in r.stderr
    (env.state / "platform" / "gateway-secrets" / "X").unlink()
    (env.state / "platform" / "gateway-secrets").rmdir()
    r = env.run("deploy-local.sh")
    assert r.returncode != 0 and "gateway-secrets" in r.stderr and "bootstrap-secrets" in r.stderr
    assert not env.calls("helm")


def test_deploy_refuses_any_cluster_but_the_local_one(env):
    secrets_present(env)
    r = env.run("deploy-local.sh", STUB_CONTEXT="some-vps")
    assert r.returncode != 0 and not env.calls("helm")


# -- teardown.sh -------------------------------------------------------------------------------------------------

def deletions(env):
    return [c for c in env.calls() if " uninstall " in c or "delete" in c]


@pytest.mark.parametrize("answer", ["", "yes", "y", "DELETE"])
def test_teardown_deletes_nothing_unless_delete_is_typed(env, answer):
    r = env.run("teardown.sh", stdin=answer + "\n")
    assert r.returncode != 0 and "nothing was changed" in r.stderr and deletions(env) == []


def test_teardown_removes_releases_and_namespaces_but_keeps_the_cluster(env):
    r = env.run("teardown.sh", stdin="delete\n")
    assert r.returncode == 0, r.stderr
    assert "helm uninstall platform -n platform" in env.calls()
    assert "helm uninstall store -n store-main" in env.calls()
    assert any(c.startswith("kubectl delete namespace platform store-main") for c in env.calls())
    assert not env.calls("k3d")


def test_teardown_with_yes_and_cluster_also_deletes_the_cluster(env):
    assert env.run("teardown.sh", "--yes", "--cluster").returncode == 0
    assert "k3d cluster delete wc" in env.calls()


def test_teardown_rejects_unknown_options_and_foreign_clusters(env):
    assert env.run("teardown.sh", "--force").returncode != 0
    assert env.run("teardown.sh", "--yes", STUB_CONTEXT="production").returncode != 0
    assert deletions(env) == []


# -- k3d-up.sh, build-images.sh, sync-to-hp.sh --------------------------------------------------------------------

def test_k3d_up_creates_the_cluster_with_web_ports_only_when_missing(env):
    assert env.run("k3d-up.sh").returncode == 0
    create = [c for c in env.calls("k3d") if "cluster create" in c]
    assert len(create) == 1 and "80:80@loadbalancer" in create[0] and "443:443@loadbalancer" in create[0]
    assert "kubectl config use-context k3d-wc" in env.calls()
    env.log.write_text("")
    assert env.run("k3d-up.sh", STUB_CLUSTER_EXISTS="1").returncode == 0
    assert not [c for c in env.calls("k3d") if "cluster create" in c]


def test_build_images_builds_both_services_then_imports_them_into_the_cluster(env):
    assert env.run("build-images.sh").returncode == 0
    root = SCRIPTS.parent
    assert env.calls("docker") == [f"docker build -t gateway:dev {root}/backend", f"docker build -t woo-mcp:dev {root}/woo-mcp",
                                   f"docker build -t gst-mcp:dev {root}/upstreams/gst",
                                   f"docker build -t everything-mcp:dev {root}/upstreams/everything",
                                   f"docker build -t dashboard:dev {root}/frontend"]
    assert env.calls("k3d") == ["k3d image import gateway:dev woo-mcp:dev gst-mcp:dev everything-mcp:dev dashboard:dev -c wc"]


def test_sync_leaves_secrets_and_local_data_behind(env):
    assert env.run("sync-to-hp.sh").returncode != 0  # no target: usage, nothing copied
    assert not env.calls("rsync")
    assert env.run("sync-to-hp.sh", "demo@192.168.1.11").returncode == 0
    (call,) = env.calls("rsync")
    for pattern in (".env", ".env.*", "*.db", "*.pem", "*.key", "kubeconfig", ".venv/", ".git/", "incidents*.jsonl"):
        assert f"--exclude {pattern} " in call, pattern
    assert call.endswith("demo@192.168.1.11:wc-platform/")


def test_bootstrap_adds_a_newly_needed_key_to_an_old_gateway_secret_without_touching_the_rest(env):
    env.add_secret("store-main", "store-mcp", MCP_SERVER_TOKEN="shared")
    env.add_secret("platform", "gateway-secrets", GATEWAY_SECRET_KEY="keep-me", WOO_MCP_TOKEN="shared")
    r = env.run("bootstrap-secrets.sh")
    assert r.returncode == 0, r.stderr
    gw = env.secret("platform", "gateway-secrets")
    assert gw["GATEWAY_SECRET_KEY"] == "keep-me" and gw["WOO_MCP_TOKEN"] == "shared" and len(gw["GST_MCP_TOKEN"]) == 48
    assert "added GST_MCP_TOKEN" in r.stdout and gw["GST_MCP_TOKEN"] not in env.log.read_text() + r.stdout + r.stderr
    before = dict(gw)
    env.run("bootstrap-secrets.sh")
    assert env.secret("platform", "gateway-secrets") == before  # a second run adds nothing


@pytest.mark.parametrize("conditions, taints, says", [
    ("Ready DiskPressure ", "", "DiskPressure"),
    ("Ready MemoryPressure ", "", "MemoryPressure"),
    ("Ready ", "node.kubernetes.io/disk-pressure", "tainted"),
])
def test_deploy_stops_at_once_when_the_node_cannot_start_new_pods(env, conditions, taints, says):
    secrets_present(env)
    r = env.run("deploy-local.sh", STUB_NODE_CONDITIONS=conditions, STUB_NODE_TAINTS=taints)
    assert r.returncode != 0 and says in r.stderr
    assert not [c for c in env.calls("helm") if " --install " in c]


def test_deploy_goes_ahead_on_a_healthy_node_and_gives_the_store_ten_minutes(env):
    secrets_present(env)
    assert env.run("deploy-local.sh", STUB_NODE_CONDITIONS="Ready ").returncode == 0
    store = [c for c in env.calls("helm") if " --install store " in c][0]
    assert "--timeout 10m" in store


# -- set-llm-key.sh ------------------------------------------------------------------------------------------------

def test_set_llm_key_stores_the_key_without_showing_it_and_restarts_the_gateway(env):
    env.add_secret("platform", "gateway-secrets", GATEWAY_SECRET_KEY="keep", WOO_MCP_TOKEN="t")
    r = env.run("set-llm-key.sh", stdin="gsk_SECRET_LLM_KEY_123\n")
    assert r.returncode == 0, r.stderr
    gw = env.secret("platform", "gateway-secrets")
    assert gw["LLM_API_KEY"] == "gsk_SECRET_LLM_KEY_123" and gw["GATEWAY_SECRET_KEY"] == "keep"
    assert "gsk_SECRET_LLM_KEY_123" not in env.log.read_text() + r.stdout + r.stderr
    assert "kubectl -n platform rollout restart deployment/gateway" in env.calls()
    assert list(env.tmpdir.iterdir()) == []


def test_set_llm_key_replaces_an_old_key(env):
    env.add_secret("platform", "gateway-secrets", LLM_API_KEY="old")
    assert env.run("set-llm-key.sh", stdin="new\n").returncode == 0
    assert env.secret("platform", "gateway-secrets")["LLM_API_KEY"] == "new"


@pytest.mark.parametrize("stdin, secret_exists, context, says", [
    ("\n", True, "k3d-wc", "no key given"),
    ("k\n", False, "k3d-wc", "bootstrap-secrets"),
    ("k\n", True, "production", "production"),
])
def test_set_llm_key_refuses_and_changes_nothing(env, stdin, secret_exists, context, says):
    if secret_exists:
        env.add_secret("platform", "gateway-secrets", GATEWAY_SECRET_KEY="keep")
    r = env.run("set-llm-key.sh", stdin=stdin, STUB_CONTEXT=context)
    assert r.returncode != 0 and says in r.stderr
    assert not [c for c in env.calls("kubectl") if " patch " in c or "rollout" in c]


# -- demo-user-b.sh (on the Mac) --------------------------------------------------------------------------------

B_KEY = "wmcp_0123abcd_" + "f" * 64


def test_demo_user_b_registers_the_gateway_with_only_the_url_and_the_key_in_its_own_folder(env):
    demo = env.tmp / "demo-b"
    r = env.run("demo-user-b.sh", "setup", stdin=B_KEY + "\n", DEMO_DIR=str(demo))
    assert r.returncode == 0, r.stderr
    adds = [c for c in env.calls("claude") if c.startswith("claude mcp add")]
    assert adds == [f"claude mcp add --transport http -s local gateway http://app.192.168.1.11.sslip.io/mcp "
                    f"--header Authorization: Bearer {B_KEY}"]
    assert f"(claude ran in {demo.resolve()})" in env.log.read_text() or f"(claude ran in {demo})" in env.log.read_text()
    assert B_KEY not in r.stdout + r.stderr  # shown nowhere; only handed to Claude Code
    assert "claude mcp list" in env.calls()


@pytest.mark.parametrize("key", ["", "nonsense", "wmcp_0123abcd_short", "gsk_" + "a" * 50, B_KEY + "x"])
def test_demo_user_b_refuses_anything_that_is_not_a_gateway_key(env, key):
    r = env.run("demo-user-b.sh", "setup", stdin=key + "\n", DEMO_DIR=str(env.tmp / "d"))
    assert r.returncode != 0 and "gateway API key" in r.stderr and not env.calls("claude")


def test_demo_user_b_stops_before_asking_for_the_key_when_the_gateway_does_not_answer(env):
    r = env.run("demo-user-b.sh", "setup", stdin=B_KEY + "\n", STUB_CURL_CODE="000", DEMO_DIR=str(env.tmp / "d"))
    assert r.returncode != 0 and "did not answer" in r.stderr and not env.calls("claude")


def test_demo_user_b_uses_another_host_and_rejects_a_bad_one(env):
    assert env.run("demo-user-b.sh", "setup", stdin=B_KEY + "\n", HOST_IP="10.0.0.5", DEMO_DIR=str(env.tmp / "d")).returncode == 0
    assert any("http://app.10.0.0.5.sslip.io/mcp" in c for c in env.calls("claude"))
    assert env.run("demo-user-b.sh", "setup", HOST_IP="10.0.0.5; rm -rf ~").returncode != 0


def test_demo_user_b_remove_and_usage(env):
    demo = env.tmp / "demo-b"
    demo.mkdir()
    r = env.run("demo-user-b.sh", "remove", DEMO_DIR=str(demo), STUB_CLAUDE_HAS_GATEWAY="1")
    assert r.returncode == 0 and "removed" in r.stdout and "claude mcp remove gateway -s local" in env.calls()
    assert env.run("demo-user-b.sh", "remove", DEMO_DIR=str(env.tmp / "missing")).returncode == 0
    assert env.run("demo-user-b.sh", "bogus").returncode != 0


def test_build_images_restarts_the_running_deployments_so_the_new_image_is_used(env):
    """Same "dev" tag every time: without a restart the old pods keep the old image (seen on the HP)."""
    assert env.run("build-images.sh").returncode == 0
    restarts = [c for c in env.calls("kubectl") if "rollout restart" in c]
    assert restarts == ["kubectl -n platform rollout restart deployment/gateway",
                        "kubectl -n store-main rollout restart deployment/woo-mcp",
                        "kubectl -n platform rollout restart deployment/gst",
                        "kubectl -n platform rollout restart deployment/everything",
                        "kubectl -n platform rollout restart deployment/dashboard"]
    calls = env.calls()
    assert calls.index(restarts[0]) > max(i for i, c in enumerate(calls) if c.startswith("k3d image import"))


@pytest.mark.parametrize("extra", [{"STUB_GATEWAY_DEPLOYED": "0"}, {"STUB_CONTEXT": "production"}])
def test_build_images_restarts_nothing_when_not_installed_or_on_another_cluster(env, extra):
    assert env.run("build-images.sh", **extra).returncode == 0
    assert not [c for c in env.calls("kubectl") if "rollout restart" in c]
