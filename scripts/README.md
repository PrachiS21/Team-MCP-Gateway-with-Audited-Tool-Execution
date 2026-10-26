# Cluster scripts

Run on the machine that hosts the cluster (my case - the HP). The Mac only edits code and runs tests.

## One-time: tools on the HP (Ubuntu)
Docker is already there. Add k3d, kubectl and helm:
```bash
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash   # k3d's official installer (worth reading before running)
sudo snap install kubectl --classic
sudo snap install helm --classic
```
Check: `k3d version && kubectl version --client && helm version`.

## The flow
```bash
# on the Mac: copy the project (secrets, .venv, databases are left behind)
scripts/sync-to-hp.sh <user>@192.168.1.11            # -> ~/wc-platform on the HP

# on the HP
cd ~/wc-platform
scripts/k3d-up.sh               # cluster "wc"; ports 80/443 of the HP go to Traefik
scripts/build-images.sh         # builds the 5 images here (amd64): gateway, woo-mcp, gst-mcp, everything-mcp, dashboard
scripts/bootstrap-secrets.sh    # namespaces + random Secrets, written straight into the cluster
scripts/deploy-local.sh         # helm upgrade --install store, then platform
```
`deploy-local.sh` prints the storefront and MCP URLs (`shop.<ip>.sslip.io`, `app.<ip>.sslip.io`). It is also the
command for every later change: edit, sync, rebuild if code changed, deploy again.

| Script | Does | Safety |
|---|---|---|
| `k3d-up.sh` | Creates or restarts the cluster, waits for the node and Traefik | Leaves an existing cluster alone |
| `build-images.sh` | `docker build` all 5 images, `k3d image import` | |
| `bootstrap-secrets.sh` | Namespaces and the 4 Secrets; shared MCP token in both namespaces | Never overwrites a Secret; values never on a command line or printed; refuses mismatched tokens |
| `deploy-local.sh` | Both charts with `--atomic --wait` (store also `--wait-for-jobs`) | Checks images and Secrets first |
| `set-llm-key.sh` | Stores the chat agent's LLM key in `platform/gateway-secrets` (hidden prompt) and restarts the gateway. Then set `llm.model` in `deploy/helm/platform/values-local.yaml` and run `deploy-local.sh` | Key never on a command line or printed; refuses without `gateway-secrets` |
| `teardown.sh [--yes] [--cluster]` | Uninstalls, deletes both namespaces (**all data**), optionally the cluster | Asks for `delete` to be typed |
| `sync-to-hp.sh user@host [dir]` | rsync of the project, exact copy (`--delete`) | Excludes `.env*`, keys, databases, logs, `.venv` |

Every script that changes the cluster refuses to run unless kubectl points at `k3d-wc`.
Settings: `CLUSTER`, `STORE_NS`, `PLATFORM_NS`, `HOST_IP` (see `lib.sh`).

## Testing the deployed system
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # once: uv runs the Python tests (the installer is worth reading before running)
scripts/cluster-test.sh                              # asks for the admin API key (hidden), runs backend/tests/cluster
```
Real pods, real Ingress, real NetworkPolicies: it lists and calls all three servers through `app.<ip>.sslip.io`,
checks the audit log, tries the internal services from a throwaway pod (they must be refused), times out a slow tool,
stops and deletes the `everything` and `woo-mcp` pods and checks the others keep working and the first call after
recovery succeeds, then revokes its own test user's key. It puts back whatever it changes.

## Reading a secret when needed
```bash
kubectl -n store-main get secret store-wordpress -o jsonpath='{.data.WP_ADMIN_PASSWORD}' | base64 -d; echo
```
Back up `platform/gateway-secrets` somewhere safe: without `GATEWAY_SECRET_KEY` the gateway cannot decrypt the
server credentials it has stored.

**Fixed (verified on the HP, 2026-10-25):** the WordPress log used to show the WooCommerce consumer key (`ck_…`) on every API call, because Apache logs the Basic-auth user name. The store chart now mounts `hide-auth-user.conf`, which logs `-` instead. Check after deploying: `kubectl -n store-main logs deploy/wordpress --tail=5` shows `- -`, no `ck_`. See `docs/threat-model.md`.

**Known limitation:** locally everything is plain HTTP (`http://app.<ip>.sslip.io`), so API keys and the dashboard session cookie cross the Wi-Fi unencrypted. Fine on a home network for the demo; Chrome's incognito mode warns ("Continue to site"). Production values turn on TLS and the Secure cookie. See `docs/threat-model.md`.

## If the HP's address changes
The host names contain the IP. Run `scripts/deploy-local.sh` again (it detects the new address). Better: reserve
the HP's address in the router (DHCP reservation) so it never changes.

## Tests
`scripts/tests/run.sh` (on the Mac; no cluster needed): the real scripts run against stand-ins for
kubectl/docker/k3d/helm/rsync, checking calls, order, safety checks and that no secret leaks.

## Before every push
```bash
python3 scripts/scan_secrets.py     # exit 1 + file:line if anything looks like a real key (the value is never printed)
```
Scans what git would push (it respects `.gitignore`). If it finds a real key: remove it, and rotate the key.
