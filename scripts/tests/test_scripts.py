"""Tests for the cluster scripts, run on any machine: docker, k3d, kubectl, helm, rsync, ip and hostname are replaced by
stub.py (see its docstring), while openssl, base64 and the shell utilities are the real ones.

Run from the project root:   scripts/tests/run.sh
What this proves: the scripts make the right calls, in the right order, with the right safety checks, and keep
secrets off command lines and out of output. What it cannot prove: that the real tools behave as the stubs do.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

from gateway.auth.keys import parse_key
from gateway.config import load_secrets_file
from gateway.seed import load_seed

SCRIPTS = Path(__file__).resolve().parents[1]
STUB = Path(__file__).with_name("stub.py")
TOOLS = ["kubectl", "docker", "k3d", "helm", "rsync", "ip", "hostname", "curl", "claude", "gcloud", "git"]
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


# -- Cloud Run: gcp-secrets.sh, gcp-deploy.sh, gcp-teardown.sh ----------------------------------------------------
# The gcloud stub keeps a small Google Cloud under state/gcloud (see stub.py). What the scripts produce is checked with
# the gateway's own code where it can be: its secrets-file loader and its seed loader.

GCP = SCRIPTS.parent / "deploy" / "gcp"
LLM_KEY = "gsk_" + "Q" * 40  # one repeated character: the repository's secret scan treats these as placeholders
CK, CS = "ck_" + "1" * 40, "cs_" + "2" * 40
WOO_ON_CLOUD_RUN = {"WOO_BASE_URL": "https://shop.example.com", "ADMIN_EMAIL": "owner@example.com"}
WOO_ON_HP = {"WOO_MCP_URL": "https://woo-mcp.tail1234.ts.net/mcp", "ADMIN_EMAIL": "owner@example.com"}
HP_TOKEN = "ab" * 24  # the HP's woo-mcp token: 48 hex characters, as bootstrap-secrets.sh makes it
SECRET_NAMES = ["gateway-env", "woo-mcp-token", "gst-mcp-token", "woo-consumer-key", "woo-consumer-secret"]


def gcp_versions(env, name):
    """(live, destroyed) version numbers of a secret."""
    folder = env.state / "gcloud" / "secrets" / name
    numbers = sorted(int(p.name) for p in folder.iterdir() if p.name.isdigit())
    destroyed = [n for n in numbers if (folder / f"{n}.destroyed").exists()]
    return [n for n in numbers if n not in destroyed], destroyed


def gcp_secret(env, name):
    live, _ = gcp_versions(env, name)
    return (env.state / "gcloud" / "secrets" / name / str(live[-1])).read_text()


def gateway_env(env):
    """gateway-env, read by the gateway's own loader (GATEWAY_SECRETS_FILE)."""
    f = env.tmp / "gateway.env"
    f.write_text(gcp_secret(env, "gateway-env"))
    values = {"GATEWAY_SECRETS_FILE": str(f)}
    load_secrets_file(values)
    del values["GATEWAY_SECRETS_FILE"]
    return values


def every_gcp_secret_value(env):
    return [*gateway_env(env).values(), *(gcp_secret(env, n) for n in SECRET_NAMES[1:])]


def gcp_ready(env):
    r = env.run("gcp-secrets.sh", stdin=f"{LLM_KEY}\n{CK}\n{CS}\n")
    assert r.returncode == 0, r.stderr
    env.log.write_text("")


def gcloud_calls(env, prefix):
    return [c for c in env.calls("gcloud") if c.startswith(f"gcloud {prefix}")]


def test_gcp_secrets_makes_five_values_in_one_region_in_the_shapes_the_services_need(env):
    r = env.run("gcp-secrets.sh", stdin=f"{LLM_KEY}\n{CK}\n{CS}\n")
    assert r.returncode == 0, r.stderr
    gw = gateway_env(env)
    assert set(gw) == {"GATEWAY_SECRET_KEY", "GATEWAY_ADMIN_KEY", "GATEWAY_LLM_API_KEY"}
    Fernet(gw["GATEWAY_SECRET_KEY"].encode())  # raises if it is not a valid key for the gateway's encryption
    assert parse_key(gw["GATEWAY_ADMIN_KEY"]) is not None and gw["GATEWAY_LLM_API_KEY"] == LLM_KEY
    woo, gst = gcp_secret(env, "woo-mcp-token"), gcp_secret(env, "gst-mcp-token")
    assert re.fullmatch(r"[0-9a-f]{48}", woo) and re.fullmatch(r"[0-9a-f]{48}", gst) and woo != gst  # no "\n" at the end
    assert gcp_secret(env, "woo-consumer-key") == CK and gcp_secret(env, "woo-consumer-secret") == CS
    creates = gcloud_calls(env, "secrets create")
    assert [c.split()[3] for c in creates] == SECRET_NAMES  # five values: inside Secret Manager's free six
    assert all("--replication-policy=user-managed --locations=us-central1" in c for c in creates)  # one location each


def test_gcp_secrets_never_shows_a_value_on_a_command_line_or_in_its_output(env):
    r = env.run("gcp-secrets.sh", stdin=f"{LLM_KEY}\n{CK}\n{CS}\n")
    seen = env.log.read_text() + r.stdout + r.stderr
    for value in every_gcp_secret_value(env):
        assert value not in seen
    assert list(env.tmpdir.iterdir()) == []  # its temporary files are gone


def test_gcp_secrets_is_safe_to_run_again_and_changes_nothing(env):
    gcp_ready(env)
    before = every_gcp_secret_value(env)
    r = env.run("gcp-secrets.sh", stdin="")
    assert r.returncode == 0, r.stderr
    assert every_gcp_secret_value(env) == before and r.stdout.count("already exists") == 5
    assert not gcloud_calls(env, "secrets create") and not gcloud_calls(env, "secrets versions add")


def test_an_llm_key_added_later_keeps_the_other_values_and_leaves_one_billed_version(env):
    assert env.run("gcp-secrets.sh", stdin=f"\n{CK}\n{CS}\n").returncode == 0  # Enter: chat off for now
    first = gateway_env(env)
    assert "GATEWAY_LLM_API_KEY" not in first
    r = env.run("gcp-secrets.sh", stdin=f"{LLM_KEY}\n")
    assert r.returncode == 0, r.stderr
    assert gateway_env(env) == {**first, "GATEWAY_LLM_API_KEY": LLM_KEY}  # the admin key and secret key did not change
    assert gcp_versions(env, "gateway-env") == ([2], [1])  # the old version is destroyed: only one is billed
    assert LLM_KEY not in env.log.read_text() + r.stdout + r.stderr


@pytest.mark.parametrize("stdin, says", [
    (f"not a key!\n{CK}\n{CS}\n", "an API key"),
    (f"{LLM_KEY}\nck_tooshort\n{CS}\n", "consumer key"),
    (f"{LLM_KEY}\n{CK}\n{CK}\n", "consumer secret"),  # the key pasted twice
])
def test_gcp_secrets_refuses_a_bad_value_before_creating_anything(env, stdin, says):
    r = env.run("gcp-secrets.sh", stdin=stdin)
    assert r.returncode != 0 and says in r.stderr and "nothing was changed" in r.stderr
    assert not gcloud_calls(env, "secrets create")
    for value in stdin.splitlines():  # every typed value, good or bad
        assert value not in r.stdout + r.stderr


@pytest.mark.parametrize("script, args", [("gcp-secrets.sh", []), ("gcp-deploy.sh", []), ("gcp-teardown.sh", ["--yes"])])
def test_gcp_scripts_refuse_to_run_without_a_project(env, script, args):
    r = env.run(script, *args, STUB_GCP_PROJECT="", **WOO_ON_CLOUD_RUN)
    assert r.returncode != 0 and "GCP_PROJECT" in r.stderr
    assert not [c for c in env.calls("gcloud") if " create" in c or " delete" in c or " deploy" in c]


def test_gcp_deploy_builds_then_deploys_the_upstreams_before_the_gateway_set_up_for_the_free_tier(env):
    gcp_ready(env)
    r = env.run("gcp-deploy.sh", **WOO_ON_CLOUD_RUN)
    assert r.returncode == 0, r.stderr
    calls = env.calls("gcloud")
    builds = gcloud_calls(env, "builds submit")
    root = SCRIPTS.parent
    assert [re.search(r"--config=(\S+)", b).group(1) for b in builds] == [
        f"{root}/deploy/gcp/cloudbuild.yaml", f"{root}/deploy/gcp/cloudbuild-woo-mcp.yaml"]
    deploys = gcloud_calls(env, "run deploy")
    assert [d.split()[3] for d in deploys] == ["woo-mcp", "gst", "gateway"]  # the gateway's seed needs their URLs
    assert calls.index(builds[-1]) < calls.index(deploys[0])
    for d in deploys:
        name = d.split()[3]
        for flag in ("--min-instances=0", "--cpu-throttling", "--allow-unauthenticated", "--region=us-central1",
                     "--port=8080", f"--service-account={name}-run@demo-proj.iam.gserviceaccount.com"):
            assert flag in d, (name, flag)
        assert "--no-cpu-throttling" not in d  # that is instance billing: about $40 a month for an always-on gateway
        image = "gst-mcp" if name == "gst" else name
        assert re.search(rf"--image=us-central1-docker\.pkg\.dev/demo-proj/mcp/{image}:\d{{8}}-\d{{6}}-abc1234 ", d)
    woo, gst, gw = deploys
    assert "--max-instances=2" in woo and "--max-instances=2" in gst
    assert "--max-instances=1" in gw  # one instance = one database
    assert "WOO_BASE_URL=https://shop.example.com" in woo and "WOO_CONSUMER_SECRET=woo-consumer-secret:latest" in woo
    assert "MCP_SERVER_TOKEN=gst-mcp-token:latest" in gst and "STORE_STATE=MH" in gst
    assert "/secrets/gateway/env=gateway-env:latest" in gw and "WOO_MCP_TOKEN=woo-mcp-token:latest" in gw
    assert "woo-consumer" not in gw  # only woo-mcp can read the store's keys
    assert "--startup-probe=httpGet.path=/ready," in gw


def test_gcp_deploy_gives_the_gateway_a_seed_it_accepts_and_no_secret_values(env):
    gcp_ready(env)
    r = env.run("gcp-deploy.sh", **WOO_ON_CLOUD_RUN)
    assert r.returncode == 0, r.stderr
    text = (env.state / "gcloud" / "vars" / "gateway.yaml").read_text()
    gw = yaml.safe_load(text)
    assert gw["GATEWAY_SECRETS_FILE"] == "/secrets/gateway/env" and gw["GATEWAY_ADMIN_EMAIL"] == "owner@example.com"
    assert gw["GATEWAY_LOG_FORMAT"] == "json" and gw["GATEWAY_GCP_PROJECT"] == "demo-proj"
    assert gw["GATEWAY_COOKIE_SECURE"] == "1" and gw["GATEWAY_LLM_MODEL"] == "openai/gpt-oss-120b"
    entries = load_seed(gw["GATEWAY_SEED_JSON"], {"WOO_MCP_TOKEN": "t1", "GST_MCP_TOKEN": "t2"})  # the gateway's loader
    assert [(e.slug, e.url, e.credential) for e in entries] == [
        ("woo", "https://woo-mcp-abc123-uc.a.run.app/mcp", "t1"), ("gst", "https://gst-abc123-uc.a.run.app/mcp", "t2")]
    seen = text + env.log.read_text() + r.stdout + r.stderr
    for value in every_gcp_secret_value(env):
        assert value not in seen
    assert list(env.tmpdir.iterdir()) == []


def test_each_service_account_reads_only_its_own_secrets_and_has_no_project_role(env):
    gcp_ready(env)
    assert env.run("gcp-deploy.sh", **WOO_ON_CLOUD_RUN).returncode == 0
    own = {"gateway": ["gateway-env", "woo-mcp-token", "gst-mcp-token"],
           "woo-mcp": ["woo-mcp-token", "woo-consumer-key", "woo-consumer-secret"], "gst": ["gst-mcp-token"]}
    expected = sorted(f"{secret} serviceAccount:{sa}-run@demo-proj.iam.gserviceaccount.com "
                      f"roles/secretmanager.secretAccessor" for sa, secrets in own.items() for secret in secrets)
    assert sorted((env.state / "gcloud" / "iam.txt").read_text().splitlines()) == expected
    assert not gcloud_calls(env, "projects add-iam-policy-binding")


def test_gcp_deploy_sets_up_the_cost_guards(env):
    gcp_ready(env)
    assert env.run("gcp-deploy.sh", **WOO_ON_CLOUD_RUN).returncode == 0
    root = SCRIPTS.parent
    [policy] = gcloud_calls(env, "artifacts repositories set-cleanup-policies")
    assert f"--policy={root}/deploy/gcp/cleanup-policy.json" in policy and "--no-dry-run" in policy
    [bucket] = gcloud_calls(env, "storage buckets create")
    assert "gs://demo-proj-build-source" in bucket and "--location=us-central1" in bucket
    [lifecycle] = gcloud_calls(env, "storage buckets update")
    assert f"--lifecycle-file={root}/deploy/gcp/build-source-lifecycle.json" in lifecycle
    builds = gcloud_calls(env, "builds submit")
    assert builds and all("--gcs-source-staging-dir=gs://demo-proj-build-source/source" in b for b in builds)
    [budget] = gcloud_calls(env, "billing budgets create")
    for part in ("--budget-amount=1 ", "--filter-projects=projects/demo-proj", "--threshold-rule=percent=0.5",
                 "--threshold-rule=percent=1.0", "--display-name=demo-proj free-tier guard"):
        assert part in budget + " ", part


# -- woo-mcp on the HP (WOO_MCP_URL): published with Tailscale Funnel, nothing of it in Google Cloud ---------------

def hp_ready(env):
    r = env.run("gcp-secrets.sh", stdin=f"{LLM_KEY}\n{HP_TOKEN}\n", WOO_MCP_URL=WOO_ON_HP["WOO_MCP_URL"])
    assert r.returncode == 0, r.stderr
    env.log.write_text("")
    return r


def test_with_woo_mcp_on_the_hp_the_secrets_are_three_and_the_token_is_the_hps(env):
    r = hp_ready(env)
    assert gcp_secret(env, "woo-mcp-token") == HP_TOKEN  # typed, not made: a new token would never match the HP's
    assert sorted(p.name for p in (env.state / "gcloud" / "secrets").iterdir()) == \
           ["gateway-env", "gst-mcp-token", "woo-mcp-token"]  # the WooCommerce keys stay on the HP
    assert HP_TOKEN not in r.stdout + r.stderr


def test_a_token_pasted_still_in_base64_is_refused_before_anything_is_created(env):
    import base64
    pasted = base64.b64encode(HP_TOKEN.encode()).decode()  # kubectl's output without `| base64 -d`
    r = env.run("gcp-secrets.sh", stdin=f"{LLM_KEY}\n{pasted}\n", WOO_MCP_URL=WOO_ON_HP["WOO_MCP_URL"])
    assert r.returncode != 0 and "base64 -d" in r.stderr and pasted not in r.stdout + r.stderr
    assert not gcloud_calls(env, "secrets create")


def test_with_woo_mcp_on_the_hp_nothing_of_it_is_built_or_deployed_and_the_seed_points_at_the_hp(env):
    hp_ready(env)
    r = env.run("gcp-deploy.sh", **WOO_ON_HP)
    assert r.returncode == 0, r.stderr
    [build] = gcloud_calls(env, "builds submit")
    assert f"--config={SCRIPTS.parent}/deploy/gcp/cloudbuild.yaml " in build + " "
    assert [d.split()[3] for d in gcloud_calls(env, "run deploy")] == ["gst", "gateway"]
    assert not [c for c in env.calls("gcloud") if "woo-mcp-run" in c or "woo-consumer" in c]
    gw = yaml.safe_load((env.state / "gcloud" / "vars" / "gateway.yaml").read_text())
    entries = load_seed(gw["GATEWAY_SEED_JSON"], {"WOO_MCP_TOKEN": HP_TOKEN, "GST_MCP_TOKEN": "t2"})
    assert [(e.slug, e.url, e.credential) for e in entries] == [
        ("woo", "https://woo-mcp.tail1234.ts.net/mcp", HP_TOKEN), ("gst", "https://gst-abc123-uc.a.run.app/mcp", "t2")]
    assert sorted((env.state / "gcloud" / "iam.txt").read_text().splitlines()) == sorted(
        f"{s} serviceAccount:{sa}-run@demo-proj.iam.gserviceaccount.com roles/secretmanager.secretAccessor"
        for sa, s in [("gateway", "gateway-env"), ("gateway", "woo-mcp-token"), ("gateway", "gst-mcp-token"),
                      ("gst", "gst-mcp-token")])
    assert HP_TOKEN not in env.log.read_text() + r.stdout + r.stderr


def test_with_woo_mcp_on_cloud_run_the_deploy_still_needs_the_store_keys(env):
    hp_ready(env)  # secrets made for the HP: no WooCommerce keys in Google Cloud
    r = env.run("gcp-deploy.sh", **WOO_ON_CLOUD_RUN)
    assert r.returncode != 0 and "woo-consumer-key" in r.stderr and not gcloud_calls(env, "run deploy")


def test_the_files_in_deploy_gcp_hold_what_the_scripts_promise():
    keep, delete = json.loads((GCP / "cleanup-policy.json").read_text())
    assert keep["action"]["type"] == "Keep" and keep["mostRecentVersions"]["keepCount"] == 2
    assert delete["action"]["type"] == "Delete" and delete["condition"] == {"tagState": "any"}
    assert json.loads((GCP / "build-source-lifecycle.json").read_text()) == \
           {"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]}
    build = yaml.safe_load((GCP / "cloudbuild.yaml").read_text())
    woo = yaml.safe_load((GCP / "cloudbuild-woo-mcp.yaml").read_text())
    # the same folders build-images.sh builds for Kubernetes; woo-mcp on its own, built only for Cloud Run
    assert [(s["id"], s["args"][-1], s["waitFor"]) for s in build["steps"]] == [
        ("gateway", "backend", ["-"]), ("gst-mcp", "upstreams/gst", ["-"])]
    assert [(s["id"], s["args"][-1]) for s in woo["steps"]] == [("woo-mcp", "woo-mcp")]
    for config in (build, woo):
        assert config["images"] == [f"${{_REPO}}/{s['id']}:${{_TAG}}" for s in config["steps"]]
        assert config["options"] == {"logging": "CLOUD_LOGGING_ONLY"}  # no machineType: the free minutes need the default
    ignore = (SCRIPTS.parent / ".gcloudignore").read_text().splitlines()
    assert "#!include:.gitignore" in ignore and ".git" in ignore  # secrets and private notes never leave the Mac


def test_gcp_deploy_run_again_creates_nothing_new_and_deploys_new_revisions(env):
    gcp_ready(env)
    assert env.run("gcp-deploy.sh", **WOO_ON_CLOUD_RUN).returncode == 0
    env.log.write_text("")
    r = env.run("gcp-deploy.sh", **WOO_ON_CLOUD_RUN)
    assert r.returncode == 0, r.stderr
    assert not [c for c in env.calls("gcloud") if re.match(r"gcloud \S+ \S+ create", c)]
    assert len(gcloud_calls(env, "run deploy")) == 3 and "already exists" in r.stdout  # the budget


def test_gcp_deploy_only_warns_when_the_budget_alert_cannot_be_made(env):
    gcp_ready(env)
    r = env.run("gcp-deploy.sh", STUB_BUDGETS_FAIL="1", **WOO_ON_CLOUD_RUN)
    assert r.returncode == 0 and "could not set up the budget alert" in r.stderr
    assert len(gcloud_calls(env, "run deploy")) == 3


def test_gcp_deploy_marks_images_built_from_uncommitted_changes(env):
    gcp_ready(env)
    assert env.run("gcp-deploy.sh", STUB_GIT_STATUS=" M backend/src/gateway/seed.py\n", **WOO_ON_CLOUD_RUN).returncode == 0
    assert all(re.search(r":\d{8}-\d{6}-abc1234-dirty ", d) for d in gcloud_calls(env, "run deploy"))


@pytest.mark.parametrize("change, says", [
    ({"WOO_BASE_URL": None}, "say where woo-mcp runs"),
    ({"WOO_MCP_URL": "https://woo-mcp.tail1234.ts.net/mcp"}, "not both"),
    ({"WOO_BASE_URL": None, "WOO_MCP_URL": "http://woo-mcp.tail1234.ts.net/mcp"}, "WOO_MCP_URL must start with https://"),
    ({"WOO_BASE_URL": "http://shop.example.com"}, "must start with https://"),
    ({"WOO_BASE_URL": "https://shop.example.com,EVIL=1"}, "WOO_BASE_URL"),
    ({"ADMIN_EMAIL": None}, "set ADMIN_EMAIL"),
    ({"ADMIN_EMAIL": "x'@example.com"}, "ADMIN_EMAIL"),
    ({"LLM_MODEL": "a,b"}, "LLM_MODEL"),
    ({"GCP_TAG": "-bad tag"}, "GCP_TAG"),
    ({"STUB_BILLING_ACCOUNT": ""}, "no billing account"),
])
def test_gcp_deploy_stops_before_changing_anything_when_a_setting_is_wrong(env, change, says):
    gcp_ready(env)
    r = env.run("gcp-deploy.sh", **{**WOO_ON_CLOUD_RUN, **change})
    assert r.returncode != 0 and says in r.stderr
    assert not [c for c in env.calls("gcloud") if re.match(r"gcloud (services|builds|run|\S+ \S+ create)", c)]


def test_gcp_deploy_stops_when_a_secret_is_missing_and_says_how_to_make_it(env):
    r = env.run("gcp-deploy.sh", **WOO_ON_CLOUD_RUN)
    assert r.returncode != 0 and "gcp-secrets.sh" in r.stderr
    assert not gcloud_calls(env, "builds submit") and not gcloud_calls(env, "run deploy")


@pytest.mark.parametrize("answer", ["", "yes", "y", "DELETE"])
def test_gcp_teardown_deletes_nothing_unless_delete_is_typed(env, answer):
    r = env.run("gcp-teardown.sh", stdin=answer + "\n")
    assert r.returncode != 0 and "nothing was changed" in r.stderr and not gcloud_calls(env, "run services delete")


def test_gcp_teardown_deletes_the_services_and_keeps_the_secrets(env):
    gcp_ready(env)
    assert env.run("gcp-deploy.sh", **WOO_ON_CLOUD_RUN).returncode == 0
    r = env.run("gcp-teardown.sh", stdin="delete\n")
    assert r.returncode == 0, r.stderr
    assert [c.split()[4] for c in gcloud_calls(env, "run services delete")] == ["gateway", "woo-mcp", "gst"]
    assert not [c for c in env.calls("gcloud") if "secrets delete" in c or "destroy" in c or "repositories delete" in c]
    assert all((env.state / "gcloud" / "secrets" / n).is_dir() for n in SECRET_NAMES)
    r = env.run("gcp-teardown.sh", "--yes")  # nothing left to delete: fine
    assert r.returncode == 0 and r.stdout.count("not found (fine)") == 3
    assert env.run("gcp-teardown.sh", "--force").returncode != 0


# -- tailscale-operator.sh (on the HP) ----------------------------------------------------------------------------

TS_ID, TS_SECRET = "kAbCdEf123", "tskey-client-kAbCdEf123-" + "Z" * 30


def test_tailscale_operator_stores_the_oauth_client_as_a_secret_then_installs_the_chart_without_it(env):
    r = env.run("tailscale-operator.sh", stdin=f"{TS_ID}\n{TS_SECRET}\n")
    assert r.returncode == 0, r.stderr
    assert env.secret("tailscale", "operator-oauth") == {"client_id": TS_ID, "client_secret": TS_SECRET}
    calls = env.calls()
    install = "helm upgrade --install tailscale-operator tailscale/tailscale-operator -n tailscale --wait"
    assert install in calls  # no credentials given to helm: the chart reads tailscale/operator-oauth
    assert calls.index(install) > max(i for i, c in enumerate(calls) if "create secret generic operator-oauth" in c)
    assert "helm repo add tailscale https://pkgs.tailscale.com/helmcharts --force-update" in calls
    assert TS_SECRET not in env.log.read_text() + r.stdout + r.stderr  # never on a command line, never shown
    assert list(env.tmpdir.iterdir()) == []


def test_tailscale_operator_keeps_an_existing_secret_and_pins_a_version_when_asked(env):
    env.add_secret("tailscale", "operator-oauth", client_id="old", client_secret="tskey-client-old")
    r = env.run("tailscale-operator.sh", stdin="", TAILSCALE_OPERATOR_VERSION="1.80.0")
    assert r.returncode == 0 and "already exists" in r.stdout
    assert env.secret("tailscale", "operator-oauth")["client_id"] == "old"
    assert any(c.endswith("-n tailscale --wait --version 1.80.0") for c in env.calls("helm"))


@pytest.mark.parametrize("stdin, context, says", [
    (f"{TS_ID}\nnot-a-client-secret\n", "k3d-wc", "tskey-client-"),
    ("\n\n", "k3d-wc", "OAuth client id"),
    (f"{TS_ID}\n{TS_SECRET}\n", "production", "production"),
])
def test_tailscale_operator_refuses_without_storing_or_installing_anything(env, stdin, context, says):
    r = env.run("tailscale-operator.sh", stdin=stdin, STUB_CONTEXT=context)
    assert r.returncode != 0 and says in r.stderr
    assert env.secret("tailscale", "operator-oauth") is None
    assert not [c for c in env.calls("helm") if "upgrade" in c]
    assert "not-a-client-secret" not in r.stdout + r.stderr
