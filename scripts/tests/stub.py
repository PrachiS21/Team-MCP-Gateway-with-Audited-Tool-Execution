"""Stand-in for kubectl, docker, k3d, helm, rsync, ip, hostname, curl and claude in the script tests.

Every call is appended to $STUB_LOG as one line ("kubectl -n store-main get secret store-db"), so a test can check
what a script ran, in which order, and that no secret value ever appeared on a command line.
Secrets "created" with kubectl are kept as files under $STUB_STATE/<namespace>/<name>/<key>.
Behaviour knobs (environment): STUB_CONTEXT, STUB_IMAGES, STUB_CLUSTER_EXISTS, STUB_IP_LINE, STUB_HOSTNAME_I,
STUB_NODE_CONDITIONS, STUB_NODE_TAINTS, STUB_HELM_RELEASES, STUB_HELM_VERSION, STUB_GATEWAY_DEPLOYED,
STUB_CURL_CODE, STUB_CLAUDE_HAS_GATEWAY.
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


def main():
    name, args = sys.argv[1], sys.argv[2:]
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(" ".join([name, *args]) + "\n")
    if name == "kubectl":
        return kubectl(args)
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
