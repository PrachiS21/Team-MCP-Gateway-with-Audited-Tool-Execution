"""Stand-in for kubectl, docker, k3d, helm, rsync, ip, hostname, curl, claude, gcloud and git in the script tests.

Every call is appended to $STUB_LOG as one line ("kubectl -n store-main get secret store-db"), so a test can check
what a script ran, in which order, and that no secret value ever appeared on a command line.
Secrets "created" with kubectl are kept as files under $STUB_STATE/<namespace>/<name>/<key>.
gcloud keeps a small Google Cloud under $STUB_STATE/gcloud (see gcloud() below).
Behaviour knobs (environment): STUB_CONTEXT, STUB_IMAGES, STUB_CLUSTER_EXISTS, STUB_IP_LINE, STUB_HOSTNAME_I,
STUB_NODE_CONDITIONS, STUB_NODE_TAINTS, STUB_HELM_RELEASES, STUB_HELM_VERSION, STUB_GATEWAY_DEPLOYED,
STUB_CURL_CODE, STUB_CLAUDE_HAS_GATEWAY, STUB_GCP_PROJECT, STUB_BILLING_ACCOUNT, STUB_BUDGETS_FAIL, STUB_GIT_STATUS.
"""

import base64
import json
import os
import shutil
import sys
from pathlib import Path


def arg_after(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


def kubectl(args):
    state = Path(os.environ["STUB_STATE"])
    ns = arg_after(args, "-n")
    rest = [a for i, a in enumerate(args) if a != "-n" and (i == 0 or args[i - 1] != "-n")]
    if rest[:2] == ["get", "nodes"]:  # deploy-local's pressure and taint checks
        out = arg_after(rest, "-o") or ""
        print(os.environ.get("STUB_NODE_CONDITIONS" if "conditions" in out else "STUB_NODE_TAINTS", ""), end="")
    elif rest[:2] == ["config", "current-context"]:
        print(os.environ.get("STUB_CONTEXT", "k3d-wc"))
    elif rest[:2] == ["create", "namespace"]:
        print(f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {rest[2]}")
    elif rest[:2] == ["apply", "-f"]:
        sys.stdin.read()
    elif rest[:2] == ["get", "secret"]:
        folder = state / ns / rest[2]
        if not folder.is_dir():
            print(f'Error from server (NotFound): secrets "{rest[2]}" not found', file=sys.stderr)
            return 1
        out = arg_after(rest, "-o")
        if out and out.startswith("jsonpath={.data."):
            key = out[len("jsonpath={.data."):-1]
            if (folder / key).exists():  # like kubectl: a missing key prints nothing and still succeeds
                print(base64.b64encode((folder / key).read_bytes()).decode(), end="")
    elif rest[:2] == ["get", "deployment"]:
        return 0 if os.environ.get("STUB_GATEWAY_DEPLOYED", "1") == "1" else 1
    elif rest[:2] == ["patch", "secret"]:
        patch = json.loads(Path(arg_after(rest, "--patch-file")).read_text())
        for key, value in patch["data"].items():
            (state / ns / rest[2] / key).write_bytes(base64.b64decode(value))
    elif rest[:3] == ["create", "secret", "generic"]:
        folder = state / ns / rest[3]
        if folder.exists():
            print("AlreadyExists", file=sys.stderr)
            return 1
        folder.mkdir(parents=True)
        for a in rest[4:]:
            key, path = a[len("--from-file="):].split("=", 1)
            shutil.copy(path, folder / key)
    return 0


def opt(args, name):
    """The value of --name=VALUE (the gcp-*.sh scripts always pass gcloud flags in that form)."""
    prefix = f"--{name}="
    return next((a[len(prefix):] for a in args if a.startswith(prefix)), None)


def fail(message):
    print(f"ERROR: (gcloud) {message}", file=sys.stderr)
    return 1


def gcloud(args):
    """Secrets are folders of numbered versions (secrets/<name>/<n>, plus <n>.destroyed once destroyed). Repositories,
    buckets, service accounts and Cloud Run services are files that exist or not; a deployed service's file holds its
    URL, and the YAML given with --env-vars-file is kept as vars/<service>.yaml. IAM bindings go to iam.txt and budget
    names to budgets.txt, one per line."""
    root = Path(os.environ["STUB_STATE"]) / "gcloud"
    root.mkdir(exist_ok=True)
    w = [a for a in args if not a.startswith("-")]  # the command words and positional arguments

    def versions(folder, live_only=True):
        numbers = [int(p.name) for p in folder.iterdir() if p.name.isdigit()]
        return sorted(n for n in numbers if not live_only or not (folder / f"{n}.destroyed").exists())

    if w[:3] == ["config", "get-value", "project"]:
        print(os.environ.get("STUB_GCP_PROJECT", "demo-proj"))
    elif w[:2] == ["secrets", "describe"]:
        return 0 if (root / "secrets" / w[2]).is_dir() else fail(f"NOT_FOUND: secret {w[2]}")
    elif w[:2] == ["secrets", "create"]:
        folder = root / "secrets" / w[2]
        if folder.exists():
            return fail(f"ALREADY_EXISTS: secret {w[2]}")
        folder.mkdir(parents=True)
        shutil.copy(opt(args, "data-file"), folder / "1")
    elif w[:3] == ["secrets", "versions", "add"]:
        folder = root / "secrets" / w[3]
        shutil.copy(opt(args, "data-file"), folder / str(max(versions(folder, live_only=False)) + 1))
    elif w[:3] == ["secrets", "versions", "access"]:
        folder = root / "secrets" / opt(args, "secret")
        print((folder / str(versions(folder)[-1])).read_text(), end="")
    elif w[:3] == ["secrets", "versions", "list"]:
        project = os.environ.get("STUB_GCP_PROJECT", "demo-proj")
        for n in reversed(versions(root / "secrets" / w[3])):  # newest first, like gcloud
            print(f"projects/{project}/secrets/{w[3]}/versions/{n}")
    elif w[:3] == ["secrets", "versions", "destroy"]:
        (root / "secrets" / opt(args, "secret") / f"{w[3]}.destroyed").touch()
    elif w[:2] == ["secrets", "add-iam-policy-binding"]:
        with open(root / "iam.txt", "a") as f:
            f.write(f"{w[2]} {opt(args, 'member')} {opt(args, 'role')}\n")
    elif w[:3] == ["billing", "projects", "describe"]:
        print(os.environ.get("STUB_BILLING_ACCOUNT", "billingAccounts/0000AA-1111BB-2222CC"))
    elif w[:2] == ["billing", "budgets"]:
        if os.environ.get("STUB_BUDGETS_FAIL") == "1":
            return fail("PERMISSION_DENIED: billing.budgets")
        names = root / "budgets.txt"
        if w[2] == "list" and names.exists():
            print(names.read_text(), end="")
        elif w[2] == "create":
            with open(names, "a") as f:
                f.write(opt(args, "display-name") + "\n")
    elif w[:2] == ["run", "deploy"]:
        (root / "services").mkdir(exist_ok=True)
        (root / "services" / w[2]).write_text(f"https://{w[2]}-abc123-uc.a.run.app")
        if opt(args, "env-vars-file"):
            (root / "vars").mkdir(exist_ok=True)
            shutil.copy(opt(args, "env-vars-file"), root / "vars" / f"{w[2]}.yaml")
    else:
        kinds = {("artifacts", "repositories"): "repos", ("storage", "buckets"): "buckets",
                 ("iam", "service-accounts"): "accounts", ("run", "services"): "services"}
        kind = kinds.get(tuple(w[:2]))
        if kind and w[2] in ("describe", "create", "delete"):
            thing = root / kind / w[3].split("@")[0].replace("/", "_")  # an account's email -> its name
            if w[2] == "describe":
                if not thing.exists():
                    return fail(f"NOT_FOUND: {w[3]}")
                if opt(args, "format") == "value(status.url)":
                    print(thing.read_text())
            elif w[2] == "create":
                thing.parent.mkdir(exist_ok=True)
                thing.write_text("")
            else:
                thing.unlink()
    return 0  # anything else (services enable, builds submit, set-cleanup-policies, buckets update): success


def main():
    name, args = sys.argv[1], sys.argv[2:]
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(" ".join([name, *args]) + "\n")
    if name == "kubectl":
        return kubectl(args)
    if name == "gcloud":
        return gcloud(args)
    if name == "git" and "rev-parse" in args:
        print("abc1234")
    if name == "git" and "status" in args:
        print(os.environ.get("STUB_GIT_STATUS", ""), end="")
    if name == "docker" and args[:2] == ["image", "inspect"]:
        return 0 if args[2] in os.environ.get("STUB_IMAGES", "").split() else 1
    if name == "k3d" and args[:2] == ["cluster", "list"]:
        return 0 if os.environ.get("STUB_CLUSTER_EXISTS") == "1" else 1
    if name == "helm" and args[:1] == ["status"]:
        return 0 if args[1] in os.environ.get("STUB_HELM_RELEASES", "").split() else 1
    if name == "helm" and args[:1] == ["version"]:
        print(os.environ.get("STUB_HELM_VERSION", "v4.3.1"), end="")
    if name == "curl":  # demo-user-b.sh only asks for the HTTP status code
        print(os.environ.get("STUB_CURL_CODE", "401"), end="")
    if name == "claude":
        with open(os.environ["STUB_LOG"], "a") as log:
            log.write(f"  (claude ran in {os.getcwd()})\n")
        if args[:2] == ["mcp", "remove"]:
            return 0 if os.environ.get("STUB_CLAUDE_HAS_GATEWAY") == "1" else 1
    if name == "ip":
        line = os.environ.get("STUB_IP_LINE")
        if not line:
            return 1
        print(line)
    if name == "hostname":
        value = os.environ.get("STUB_HOSTNAME_I")
        if not value:
            return 1
        print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
