"""Tests for the Helm charts. They need `helm` but NO cluster: each chart is rendered with `helm template` and the
resulting Kubernetes objects are checked against the rules this project promised (the platform's requirements, our own
security decisions) and against the real code the pods run (the gateway's seed loader, woo-mcp's required settings).

Run from the project root:   deploy/helm/tests/run.sh
A passing run means the charts are internally consistent. It does NOT prove they work in a cluster (probes, network
policies and the setup Job are only proven on the HP).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
HELM = ROOT / "deploy" / "helm"
sys.path.insert(0, str(ROOT / "woo-mcp" / "src"))  # woo_mcp.config only uses the standard library
sys.path.insert(0, str(ROOT / "backend" / "src"))

CHARTS = {"store": "store-main", "platform": "platform"}  # chart -> the namespace it is installed into
ENVS = ["local", "prod"]
COMBOS = [(c, e) for c in CHARTS for e in ENVS]
WORKLOADS = ("Deployment", "StatefulSet", "Job")
SECRETISH = re.compile(r"PASSWORD|SECRET|TOKEN|KEY", re.I)


def helm(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", *args], capture_output=True, text=True)


def render(chart: str, env: str | None = "local", *sets: str) -> list[dict]:
    cmd = ["template", chart, str(HELM / chart), "-n", CHARTS[chart]]
    if env:
        cmd += ["-f", str(HELM / chart / f"values-{env}.yaml")]
    for s in sets:
        cmd += ["--set", s]
    out = helm(*cmd)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


@pytest.fixture(scope="module")
def rendered():
    return {(c, e): render(c, e) for c, e in COMBOS}


def kinds(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def named(docs, kind, name):
    found = [d for d in kinds(docs, kind) if d["metadata"]["name"] == name]
    assert found, f"no {kind} named {name}"
    return found[0]


def pod_spec(workload):
    return workload["spec"]["template"]["spec"]


def containers(docs):
    for w in docs:
        if w["kind"] in WORKLOADS:
            for c in pod_spec(w)["containers"] + pod_spec(w).get("initContainers", []):
                yield w, c


# -- the charts render ------------------------------------------------------------------------------------

@pytest.mark.parametrize("chart, env", COMBOS)
def test_lint_passes(chart, env):
    out = helm("lint", str(HELM / chart), "-f", str(HELM / chart / f"values-{env}.yaml"))
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize("chart", CHARTS)
def test_rendering_without_an_environment_file_fails_clearly(chart):
    """The public host has no default on purpose: forgetting values-<env>.yaml must not give a half-working chart."""
    out = helm("template", chart, str(HELM / chart), "-n", CHARTS[chart])
    assert out.returncode != 0 and "host" in out.stderr


# -- requirements: probes, resources, secrets -------------------------------------------------------------

@pytest.mark.parametrize("chart, env", COMBOS)
def test_every_container_has_resource_requests_and_limits(rendered, chart, env):
    seen = 0
    for w, c in containers(rendered[chart, env]):
        r = c.get("resources", {})
        for part in ("requests", "limits"):
            assert {"cpu", "memory"} <= set(r.get(part, {})), f"{w['metadata']['name']}/{c['name']} lacks {part}"
        seen += 1
    assert seen


@pytest.mark.parametrize("chart, env", COMBOS)
def test_every_long_running_container_has_readiness_and_liveness_probes(rendered, chart, env):
    for w, c in containers(rendered[chart, env]):
        if w["kind"] != "Job":  # a one-off Job finishes; it has no "ready"
            assert "readinessProbe" in c and "livenessProbe" in c, f"{w['metadata']['name']}/{c['name']}"


@pytest.mark.parametrize("chart, env", COMBOS)
def test_no_secret_value_is_in_the_chart_output(rendered, chart, env):
    docs = rendered[chart, env]
    assert not kinds(docs, "Secret"), "charts must only reference Secrets, never create them"
    for w, c in containers(docs):
        for e in c.get("env", []):
            if SECRETISH.search(e["name"]):
                assert "valueFrom" in e and "value" not in e, f"{e['name']} must come from a Secret"


@pytest.mark.parametrize("chart, env", COMBOS)
def test_every_referenced_secret_name_is_declared_in_values(chart, env, rendered):
    declared = set(yaml.safe_load((HELM / chart / "values.yaml").read_text())["secrets"].values())
    used = {e["valueFrom"]["secretKeyRef"]["name"] for _, c in containers(rendered[chart, env])
            for e in c.get("env", []) if "valueFrom" in e}
    assert used and used <= declared


@pytest.mark.parametrize("chart, env", COMBOS)
def test_images_have_an_explicit_tag_and_never_latest(rendered, chart, env):
    for _, c in containers(rendered[chart, env]):
        assert ":" in c["image"] and not c["image"].endswith(":latest"), c["image"]


# -- internal wiring: nothing points at something that does not exist ---------------------------------------

@pytest.mark.parametrize("chart, env", COMBOS)
def test_volumes_services_and_ingress_backends_all_exist(rendered, chart, env):
    docs = rendered[chart, env]
    pvcs = {d["metadata"]["name"] for d in kinds(docs, "PersistentVolumeClaim")}
    cms = {d["metadata"]["name"] for d in kinds(docs, "ConfigMap")}
    for w in docs:
        if w["kind"] in WORKLOADS:
            for v in pod_spec(w).get("volumes", []):
                if "persistentVolumeClaim" in v:
                    assert v["persistentVolumeClaim"]["claimName"] in pvcs
                if "configMap" in v:
                    assert v["configMap"]["name"] in cms
    pod_labels = [w["spec"]["template"]["metadata"]["labels"] for w in docs if w["kind"] in WORKLOADS]
    services = {s["metadata"]["name"]: s for s in kinds(docs, "Service")}
    for s in services.values():
        assert any(s["spec"]["selector"].items() <= labels.items() for labels in pod_labels), s["metadata"]["name"]
    for ing in kinds(docs, "Ingress"):
        for rule in ing["spec"]["rules"]:
            for p in rule["http"]["paths"]:
                svc = services[p["backend"]["service"]["name"]]
                assert p["backend"]["service"]["port"]["name"] in [x["name"] for x in svc["spec"]["ports"]]


@pytest.mark.parametrize("chart, env", COMBOS)
def test_workloads_with_a_single_use_volume_do_not_roll_two_pods_at_once(rendered, chart, env):
    for w in rendered[chart, env]:
        if w["kind"] == "Deployment" and any("persistentVolumeClaim" in v for v in pod_spec(w).get("volumes", [])):
            assert w["spec"]["strategy"]["type"] == "Recreate", w["metadata"]["name"]


# -- "what is public and what is internal" ------------------------------------------------------------------

@pytest.mark.parametrize("chart, env", COMBOS)
def test_no_service_is_exposed_outside_the_cluster(rendered, chart, env):
    for s in kinds(rendered[chart, env], "Service"):
        assert s["spec"].get("type", "ClusterIP") == "ClusterIP", s["metadata"]["name"]


@pytest.mark.parametrize("env", ENVS)
def test_only_wordpress_is_reachable_through_the_store_ingress(rendered, env):
    docs = rendered["store", env]
    backends = {p["backend"]["service"]["name"] for i in kinds(docs, "Ingress")
                for r in i["spec"]["rules"] for p in r["http"]["paths"]}
    assert backends == {"wordpress"}  # no route to the database or the MCP server


@pytest.mark.parametrize("env", ENVS)
def test_the_gateway_ingress_exposes_mcp_and_api_only_and_not_the_health_endpoints(rendered, env):
    paths = [p["path"] for i in kinds(rendered["platform", env], "Ingress")
             for r in i["spec"]["rules"] for p in r["http"]["paths"]]
    assert paths == ["/mcp", "/api", "/"]  # "/" is the dashboard: static files only, no health endpoints


@pytest.mark.parametrize("env, secure", [("local", "0"), ("prod", "1")])
def test_the_session_cookie_is_https_only_exactly_when_the_site_is_https(rendered, env, secure):
    pod = pod_spec(named(rendered["platform", env], "Deployment", "gateway"))
    env_vars = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
    assert env_vars["GATEWAY_COOKIE_SECURE"] == secure


@pytest.mark.parametrize("chart, env", COMBOS)
def test_each_namespace_denies_all_traffic_by_default(rendered, chart, env):
    deny = named(rendered[chart, env], "NetworkPolicy", "default-deny")["spec"]
    assert deny["podSelector"] == {} and set(deny["policyTypes"]) == {"Ingress", "Egress"}
    assert "ingress" not in deny and "egress" not in deny  # no allowances smuggled into the deny rule


def test_the_mcp_server_accepts_connections_only_from_the_gateway(rendered):
    store = rendered["store", "local"]
    platform = rendered["platform", "local"]
    policy = named(store, "NetworkPolicy", "woo-mcp")["spec"]
    sources = [f for rule in policy["ingress"] for f in rule["from"]]
    assert len(sources) == 1
    src = sources[0]
    gateway_pod = named(platform, "Deployment", "gateway")["spec"]["template"]["metadata"]["labels"]
    assert src["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == CHARTS["platform"]
    assert src["podSelector"]["matchLabels"].items() <= gateway_pod.items()  # the label the platform chart really sets
    assert [p["port"] for rule in policy["ingress"] for p in rule["ports"]] == [8000]


def test_the_database_accepts_connections_only_from_wordpress_and_the_setup_job(rendered):
    policy = named(rendered["store", "local"], "NetworkPolicy", "mariadb")["spec"]
    allowed = {f["podSelector"]["matchLabels"]["app.kubernetes.io/name"] for r in policy["ingress"] for f in r["from"]}
    assert allowed == {"wordpress", "store-setup"} and "namespaceSelector" not in str(policy["ingress"])


def test_the_gateway_may_only_call_the_mcp_server_of_each_listed_store(rendered):
    store = rendered["store", "local"]
    mcp_pod = named(store, "Deployment", "woo-mcp")["spec"]["template"]["metadata"]["labels"]
    values = yaml.safe_load((HELM / "platform" / "values.yaml").read_text())
    egress = named(rendered["platform", "local"], "NetworkPolicy", "gateway")["spec"]["egress"]
    to_stores = [r for r in egress if "namespaceSelector" in r["to"][0]]
    to_upstreams = [r for r in egress if "podSelector" in r["to"][0] and "namespaceSelector" not in r["to"][0]]
    assert len(to_stores) == len(values["networkPolicy"]["storeNamespaces"]) and len(to_upstreams) == 1
    assert to_upstreams[0]["to"] == [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "upstream"}}}]
    assert sorted(p["port"] for p in to_upstreams[0]["ports"]) == sorted(u["port"] for u in values["upstreams"])
    for rule in to_stores:
        target = rule["to"][0]
        assert target["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == CHARTS["store"]
        assert target["podSelector"]["matchLabels"].items() <= mcp_pod.items()
        assert [p["port"] for p in rule["ports"]] == [8000]


# -- hardening decisions --------------------------------------------------------------------------------------

@pytest.mark.parametrize("chart, name", [("store", "woo-mcp"), ("platform", "gateway"), ("platform", "gst"),
                                         ("platform", "everything"), ("platform", "dashboard")])
def test_our_own_services_run_non_root_with_a_read_only_filesystem(rendered, chart, name):
    w = named(rendered[chart, "local"], "Deployment", name)
    c = pod_spec(w)["containers"][0]
    assert pod_spec(w)["securityContext"]["runAsNonRoot"] is True
    assert pod_spec(w)["automountServiceAccountToken"] is False
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_the_gateway_can_write_only_to_its_data_volume_and_tmp(rendered):
    c = pod_spec(named(rendered["platform", "local"], "Deployment", "gateway"))["containers"][0]
    writable = {m["mountPath"] for m in c["volumeMounts"] if not m.get("readOnly")}
    assert writable == {"/data", "/tmp"}


def test_the_mcp_server_can_write_only_to_tmp(rendered):
    c = pod_spec(named(rendered["store", "local"], "Deployment", "woo-mcp"))["containers"][0]
    assert {m["mountPath"] for m in c["volumeMounts"] if not m.get("readOnly")} == {"/tmp"}


# -- the charts agree with the code the pods run -------------------------------------------------------------

def test_the_mcp_server_gets_every_setting_it_requires(rendered):
    from woo_mcp.config import REQUIRED
    c = pod_spec(named(rendered["store", "local"], "Deployment", "woo-mcp"))["containers"][0]
    assert set(REQUIRED) <= {e["name"] for e in c["env"]}


def test_the_mcp_server_reaches_wordpress_by_its_in_cluster_service_name(rendered):
    c = pod_spec(named(rendered["store", "local"], "Deployment", "woo-mcp"))["containers"][0]
    url = {e["name"]: e.get("value") for e in c["env"]}["WOO_BASE_URL"]
    assert url == "http://wordpress.store-main.svc.cluster.local"
    assert named(rendered["store", "local"], "Service", "wordpress")["spec"]["ports"][0]["port"] == 80


def test_the_gateway_gets_its_encryption_key_and_a_seed_file_it_can_load(rendered, tmp_path):
    from gateway.seed import load_seed
    docs = rendered["platform", "local"]
    c = pod_spec(named(docs, "Deployment", "gateway"))["containers"][0]
    env = {e["name"]: e for e in c["env"]}
    assert "GATEWAY_SECRET_KEY" in env and env["GATEWAY_SEED_FILE"]["value"] == "/config/seed.json"

    seed = tmp_path / "seed.json"
    seed.write_text(named(docs, "ConfigMap", "gateway-seed")["data"]["seed.json"])
    entries = load_seed(seed, {e: "token-value" for e in env})  # the gateway's own loader, with only the variables we set
    assert [e.slug for e in entries] == ["woo", "gst", "everything"]  # three upstream servers
    assert [e.credential_type for e in entries] == ["bearer", "bearer", "none"]
    assert entries[0].credential == entries[1].credential == "token-value" and entries[2].credential is None
    assert "token-value" not in seed.read_text()


def test_the_seed_url_points_at_the_store_chart_mcp_service(rendered):
    seed = json.loads(named(rendered["platform", "local"], "ConfigMap", "gateway-seed")["data"]["seed.json"])
    for server in seed["servers"][1:]:  # the upstreams that run next to the gateway
        svc = named(rendered["platform", "local"], "Service", server["slug"])
        assert server["url"] == f"http://{server['slug']}.{CHARTS['platform']}.svc.cluster.local:{svc['spec']['ports'][0]['port']}/mcp"
    svc = named(rendered["store", "local"], "Service", "woo-mcp")
    host = f"http://{svc['metadata']['name']}.{CHARTS['store']}.svc.cluster.local:{svc['spec']['ports'][0]['port']}/mcp"
    assert seed["servers"][0]["url"] == host


def test_changing_the_server_list_restarts_the_gateway_pod():
    """The seed file is read only at start, so its checksum on the pod must change with it."""
    def checksum(*sets):
        gw = named(render("platform", "local", *sets), "Deployment", "gateway")
        return gw["spec"]["template"]["metadata"]["annotations"]["checksum/seed"]
    assert checksum() == checksum()
    assert checksum() != checksum("servers[0].name=Renamed")


def test_the_setup_job_uses_exactly_the_scripts_the_compose_check_proved(rendered):
    data = named(rendered["store", "local"], "ConfigMap", "store-setup-scripts")["data"]
    compose = ROOT / "deploy" / "compose" / "store"
    assert set(data) == {"setup-store.sh", "create-api-key.php", "keep-url-scheme.php"}
    for name, text in data.items():
        assert text == (compose / name).read_text(), f"{name} differs from deploy/compose/store/{name}; copy it to helm/store/files/"


def test_wordpress_is_told_the_public_address_and_to_trust_forwarded_https(rendered):
    wp = pod_spec(named(rendered["store", "local"], "Deployment", "wordpress"))["containers"][0]
    extra = {e["name"]: e.get("value") for e in wp["env"]}["WORDPRESS_CONFIG_EXTRA"]
    assert "define('WP_HOME', 'http://shop.192.168.1.11.sslip.io');" in extra
    assert "HTTP_X_FORWARDED_PROTO" in extra


def test_wordpress_and_the_setup_job_share_the_same_database_settings(rendered):
    docs = rendered["store", "local"]
    wp = pod_spec(named(docs, "Deployment", "wordpress"))["containers"][0]["env"]
    job = pod_spec(kinds(docs, "Job")[0])["containers"][0]["env"]
    pick = lambda env: {e["name"]: e for e in env if e["name"].startswith("WORDPRESS_DB_")}
    assert pick(wp) == pick(job) and len(pick(wp)) == 4  # wp-config.php reads these at run time in both


# -- environments differ only by values --------------------------------------------------------------------

@pytest.mark.parametrize("chart", CHARTS)
def test_local_is_plain_http_and_prod_is_https_with_tls(rendered, chart):
    local = kinds(rendered[chart, "local"], "Ingress")[0]["spec"]
    prod = kinds(rendered[chart, "prod"], "Ingress")[0]["spec"]
    assert "tls" not in local and prod["tls"][0]["hosts"] == [prod["rules"][0]["host"]]


@pytest.mark.parametrize("chart", CHARTS)
def test_local_and_prod_render_the_same_set_of_objects(rendered, chart):
    objects = lambda env: sorted((d["kind"], d["metadata"]["name"]) for d in rendered[chart, env])
    assert objects("local") == objects("prod")  # "configuration changes only"


# -- the extra upstream servers ---------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["gst", "everything"])
def test_an_upstream_accepts_only_the_gateway_and_opens_no_connections(rendered, name):
    docs = rendered["platform", "local"]
    policy = named(docs, "NetworkPolicy", f"upstream-{name}")["spec"]
    assert policy["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": name}
    assert set(policy["policyTypes"]) == {"Ingress", "Egress"} and policy["egress"] == []
    (rule,) = policy["ingress"]
    assert rule["from"] == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "gateway"}}}]
    port = named(docs, "Service", name)["spec"]["ports"][0]["port"]
    assert rule["ports"] == [{"protocol": "TCP", "port": port}]


def test_the_untrusted_reference_server_is_given_no_secret(rendered):
    """Its get-env tool returns every environment variable, so nothing secret may be in its pod."""
    pod = pod_spec(named(rendered["platform", "local"], "Deployment", "everything"))
    assert "secretKeyRef" not in json.dumps(pod) and "secret" not in json.dumps(pod.get("volumes", []))


def test_the_gateway_and_the_gst_server_use_the_same_secret_key_for_their_token(rendered):
    docs = rendered["platform", "local"]
    gst = pod_spec(named(docs, "Deployment", "gst"))["containers"][0]["env"]
    pod_key = {e["name"]: e for e in gst}["MCP_SERVER_TOKEN"]["valueFrom"]["secretKeyRef"]
    gw = {e["name"]: e for e in pod_spec(named(docs, "Deployment", "gateway"))["containers"][0]["env"]}
    seed = json.loads(named(docs, "ConfigMap", "gateway-seed")["data"]["seed.json"])
    env_name = [s for s in seed["servers"] if s["slug"] == "gst"][0]["credential_env"]
    assert gw[env_name]["valueFrom"]["secretKeyRef"] == pod_key


# -- the dashboard ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("env", ENVS)
def test_the_dashboard_is_served_at_slash_and_api_and_mcp_go_to_the_gateway(rendered, env):
    routes = {p["path"]: p["backend"]["service"]["name"] for i in kinds(rendered["platform", env], "Ingress")
              for r in i["spec"]["rules"] for p in r["http"]["paths"]}
    assert routes == {"/mcp": "gateway", "/api": "gateway", "/": "dashboard"}  # one host, so the cookie needs no CORS


def test_the_dashboard_accepts_only_traefik_opens_nothing_holds_no_secret_and_writes_only_tmp(rendered):
    docs = rendered["platform", "local"]
    policy = named(docs, "NetworkPolicy", "dashboard")["spec"]
    (rule,) = policy["ingress"]
    assert rule["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "kube-system"
    assert policy["egress"] == [] and set(policy["policyTypes"]) == {"Ingress", "Egress"}
    pod = pod_spec(named(docs, "Deployment", "dashboard"))
    assert "secretKeyRef" not in json.dumps(pod)
    assert {m["mountPath"] for m in pod["containers"][0]["volumeMounts"] if not m.get("readOnly")} == {"/tmp"}


def test_the_dashboard_can_be_switched_off():
    docs = render("platform", "local", "dashboard.enabled=false")
    assert not [d for d in docs if d["metadata"]["name"] == "dashboard"]
    assert [p["path"] for i in kinds(docs, "Ingress") for r in i["spec"]["rules"] for p in r["http"]["paths"]] == ["/mcp", "/api"]


@pytest.mark.parametrize("chart, env", COMBOS)
def test_every_probe_sets_its_own_timeout(rendered, chart, env):
    """Kubernetes' default probe timeout is 1 second. A slow moment (a cold start, a resumed laptop) then looks like a
    dead container: seen on the HP, where WordPress was restarted although it answered 200 a little later."""
    missing = [f"{w['metadata']['name']}/{c['name']}.{p}" for w, c in containers(rendered[chart, env])
               for p in ("startupProbe", "readinessProbe", "livenessProbe")
               if p in c and c[p].get("timeoutSeconds", 1) < 3]
    assert not missing, missing


@pytest.mark.parametrize("chart, env", COMBOS)
def test_no_pod_gets_the_service_link_environment_variables(rendered, chart, env):
    """Kubernetes would inject <SERVICE>_PORT=tcp://ip:port and similar for every Service in the namespace. Our Service
    "gateway" thereby set GATEWAY_PORT, the gateway's own port setting, and the gateway crashed on the HP."""
    workloads = [d for d in rendered[chart, env] if d["kind"] in ("Deployment", "StatefulSet", "Job")]
    assert workloads
    missing = [d["metadata"]["name"] for d in workloads if pod_spec(d).get("enableServiceLinks") is not False]
    assert not missing, missing


# -- the chat agent's LLM -----------------------------------------------------------------------------------------

def test_without_a_model_the_gateway_gets_no_llm_settings_and_no_internet():
    docs = render("platform", "local", "llm.model=")
    env = [e["name"] for e in pod_spec(named(docs, "Deployment", "gateway"))["containers"][0]["env"]]
    assert not [n for n in env if n.startswith("GATEWAY_LLM_")]
    assert "ipBlock" not in json.dumps(named(docs, "NetworkPolicy", "gateway"))


def test_with_a_model_the_key_comes_from_the_secret_and_only_public_https_is_opened():
    docs = render("platform", "local", "llm.model=some-model")
    env = {e["name"]: e for e in pod_spec(named(docs, "Deployment", "gateway"))["containers"][0]["env"]}
    assert env["GATEWAY_LLM_MODEL"]["value"] == "some-model" and env["GATEWAY_LLM_BASE_URL"]["value"].startswith("https://")
    ref = env["GATEWAY_LLM_API_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "gateway-secrets", "key": "LLM_API_KEY", "optional": True}  # no key yet: chat off, pod starts
    (rule,) = [r for r in named(docs, "NetworkPolicy", "gateway")["spec"]["egress"] if "ipBlock" in r["to"][0]]
    block = rule["to"][0]["ipBlock"]
    assert block["cidr"] == "0.0.0.0/0" and rule["ports"] == [{"protocol": "TCP", "port": 443}]
    for private in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
        assert private in block["except"]  # the cluster, LANs and cloud metadata stay unreachable


# -- the WooCommerce consumer key stays out of Apache's access log ---------------------------------------------------

def test_wordpress_logs_without_the_basic_auth_user_name(rendered):
    """WooCommerce's Basic-auth user name IS the consumer key; Apache's standard formats would log it as %u."""
    docs = rendered["store", "local"]
    conf = named(docs, "ConfigMap", "wordpress-apache-conf")["data"]["hide-auth-user.conf"]
    formats = [line for line in conf.splitlines() if line.startswith("LogFormat ")]
    assert {f.rsplit(" ", 1)[1] for f in formats} == {"combined", "vhost_combined", "common"}
    for f in formats:
        assert "%u" not in f, f
        assert "%a " in f and "%h" not in f, f  # keep mod_remoteip's real client address (the image uses %a)
    pod = pod_spec(named(docs, "Deployment", "wordpress"))
    (mount,) = [m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "apache-conf"]
    # conf-enabled/ is read after Debian's own LogFormat lines and before the site that logs with "combined"
    assert mount == {"name": "apache-conf", "mountPath": "/etc/apache2/conf-enabled/zz-hide-auth-user.conf",
                     "subPath": "hide-auth-user.conf", "readOnly": True}
    assert named(docs, "Deployment", "wordpress")["spec"]["template"]["metadata"]["annotations"]["checksum/apache-conf"]


def test_the_compose_store_uses_the_same_apache_file():
    compose = HELM.parent / "compose"
    assert (HELM / "store" / "files" / "hide-auth-user.conf").read_text() == (compose / "store" / "hide-auth-user.conf").read_text()
    assert "./store/hide-auth-user.conf:/etc/apache2/conf-enabled/zz-hide-auth-user.conf:ro" in (compose / "docker-compose.yml").read_text()
