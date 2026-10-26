# WooCommerce MCP Gateway on Kubernetes

**Author:** Prachi.

A WooCommerce store on Kubernetes, with a **team MCP gateway** in front of every MCP server the team uses. An admin
registers each server once in a dashboard. Every team member then uses all of them through **one MCP endpoint and their
own API key**, from Claude Code, Cursor or MCP Inspector, or through a **chat agent in the dashboard**. Members never see
the servers' credentials, and every tool call is in an audit log.

```
 Browser: dashboard + chat ─┐                      ┌─► gateway ─┬─► woo-mcp ─► WordPress + WooCommerce ─► MariaDB
 Claude Code / Cursor ──────┼─► Ingress (Traefik) ─┤            ├─► gst (GST calculator)
 Storefront shoppers ───────┘                      │            ├─► everything (official MCP reference server)
                                                   │            └─► LLM API (chat only)
                                                   ├─► dashboard (static React app)
                                                   └─► WordPress (storefront)
```

| Part | Folder |
|---|---|
| Gateway: MCP endpoint, admin API, audit log, chat agent | `backend/` |
| WooCommerce MCP server (written for this project) | `woo-mcp/` |
| Two more upstream servers: `gst` (mine), `everything` (official reference server) | `upstreams/` |
| Dashboard (React) | `frontend/` |
| Helm charts: `store` and `platform`, with `values-local.yaml` / `values-prod.yaml` | `deploy/helm/` |
| Scripts: cluster, images, secrets, deploy, demo client | `scripts/` |
| Docker Compose store used for the early contract tests | `deploy/compose/` |

Documents: [System design and trade-offs](docs/design.md) · [Threat model](docs/threat-model.md) ·
[Demo steps](docs/demo-steps.md) · [Status codes and error meanings](Status_Codes.md) · [Script reference](scripts/README.md)

---

## Local setup (k3d)
I run the cluster on an Ubuntu laptop (16 GB RAM, amd64) and edit the code on a Mac on the same network. One machine
works too: skip the sync step.

**Needs on the cluster machine:** Docker, [k3d](https://k3d.io), kubectl, Helm (3 or 4), and about 25 GB of free disk on
a 100 GB disk (Kubernetes stops starting pods when free space falls below about 15%). `scripts/README.md` lists install
commands.

```bash
# from the machine with the code, if it is not the cluster machine (secrets, .venv and databases are left behind)
scripts/sync-to-hp.sh user@<cluster-machine-ip>       # copies to ~/wc-platform

# on the cluster machine
cd ~/wc-platform
scripts/k3d-up.sh               # cluster "wc"; ports 80 and 443 of this machine go to Traefik
scripts/build-images.sh         # builds the 5 images here and imports them into the cluster
scripts/bootstrap-secrets.sh    # namespaces and random Secrets, written straight into the cluster
scripts/set-llm-key.sh          # optional: the chat agent's LLM key (hidden prompt)
scripts/deploy-local.sh         # installs the store chart, then the platform chart
```
The first deploy downloads WooCommerce (a few minutes). `deploy-local.sh` prints the addresses:

| | Address |
|---|---|
| Storefront | `http://shop.<ip>.sslip.io` (WooCommerce admin: `/wp-admin`, user `admin`) |
| Dashboard | `http://app.<ip>.sslip.io` |
| MCP endpoint | `http://app.<ip>.sslip.io/mcp` |

**The local domain approach.** [sslip.io](https://sslip.io) is a public DNS service in which `shop.192.168.1.11.sslip.io`
resolves to `192.168.1.11`. So the Ingress gets stable host names that every device on the network can use, with no
hosts-file editing and no DNS server of my own. The IP comes from `deploy-local.sh`, which detects it (override with
`HOST_IP=…`); if the machine's address changes, run it again. Local traffic is plain HTTP (see the threat model).

**The first admin's API key** is printed once, when the gateway starts for the first time:
```bash
kubectl -n platform logs deploy/gateway | grep -A3 "First admin"
```
Store it in a password manager. If it is lost:
`kubectl -n platform exec -it deploy/gateway -- gateway-admin issue-key admin@example.com --name dashboard`.

**The WordPress admin password:**
```bash
kubectl -n store-main get secret store-wordpress -o jsonpath='{.data.WP_ADMIN_PASSWORD}' | base64 -d; echo
```

**Chat agent.** Uses any OpenAI-compatible API; set the model in `deploy/helm/platform/values-local.yaml` (`llm.model`,
default base URL Groq's) and the key with `scripts/set-llm-key.sh`. Without a model or key, the chat answers
"not configured" and everything else works.

**After changing code:** sync, then `scripts/build-images.sh` (it restarts the affected pods). After changing charts or
values: `scripts/deploy-local.sh`. To remove everything: `scripts/teardown.sh` (asks before deleting data).

## Production-like setup (VPS with k3s)
Same charts; only values change. I have not deployed it to a VPS yet; these are the steps.

1. **Server and DNS.** A VPS with k3s (`curl -sfL https://get.k3s.io | sh -`; Traefik and `local-path` storage come
   with it). DNS A records for `shop.<domain>` and `app.<domain>` pointing to it.
2. **Images.** Build the five images (`backend/`, `woo-mcp/`, `upstreams/gst/`, `upstreams/everything/`, `frontend/`)
   for the server's architecture, push them to a registry, and set `images.*` (and the `upstreams[].image` entries) in
   the `values-prod.yaml` files to those names with pinned tags.
3. **TLS.** Install cert-manager (or bring certificates) so the Secrets `store-main-tls` and `platform-tls` exist; the
   prod values turn on TLS on both Ingresses and mark the session cookie `Secure`.
4. **Values.** Set `host` in both `values-prod.yaml` files to the real names, and `llm.model` if the chat is wanted.
5. **Secrets.** The local scripts refuse any cluster but the local k3d one, on purpose. Create the same Secrets with
   `kubectl create secret generic`: in `store-main`, `store-db` (`DB_PASSWORD`, `DB_ROOT_PASSWORD`), `store-wordpress`
   (`WP_ADMIN_PASSWORD`, `WC_CONSUMER_KEY`, `WC_CONSUMER_SECRET`), `store-mcp` (`MCP_SERVER_TOKEN`); in `platform`,
   `gateway-secrets` (`GATEWAY_SECRET_KEY`, a Fernet key; `WOO_MCP_TOKEN`, the same value as `MCP_SERVER_TOKEN`;
   `GST_MCP_TOKEN`; optional `LLM_API_KEY`). Or use an external secrets manager: the charts only reference Secrets by
   name.
6. **Install:**
   ```bash
   helm upgrade --install store deploy/helm/store -n store-main -f deploy/helm/store/values-prod.yaml --wait --wait-for-jobs --timeout 15m
   helm upgrade --install platform deploy/helm/platform -n platform -f deploy/helm/platform/values-prod.yaml --wait --timeout 10m
   ```
What changes from local: domain and DNS, TLS, image registry, larger PVCs and resources, the storage class if the
provider has a better one, how Secrets are created. Details: [docs/design.md](docs/design.md#local-versus-production).

**Upgrade and rollback:** `helm upgrade` as above (failed upgrades roll back with `--rollback-on-failure`, Helm 4, or
`--atomic`, Helm 3); `helm history <release> -n <ns>`; `helm rollback <release> <revision> -n <ns>`. Volumes keep their
data through both.

---

## How to use it

### Place an order
Open the storefront, add a product to the cart, check out with **Cash on delivery**. It appears in WooCommerce admin
under **WooCommerce → Orders**, and in `woo__list_orders` with "today" computed in the store's time zone (IST).

### Team members and keys
- Admin, in the dashboard → **Members**: **Invite** creates a member and shows their first API key once; **Issue new
  key** gives an existing member another; keys can be revoked; members can be deactivated.
- A member logs in to the dashboard with their key and creates more under **My API keys** (one per device or tool, so
  one can be revoked without the others).

### Connect an external MCP client
Only the gateway URL and the member's key are needed.
- **Claude Code:**
  `claude mcp add --transport http gateway http://app.<ip>.sslip.io/mcp --header "Authorization: Bearer <key>"`
  then `/mcp` shows the tools of every enabled server. (`scripts/demo-user-b.sh setup` does this with a hidden prompt,
  for a separate demo folder.)
- **Cursor / other clients:** Streamable HTTP transport, URL `…/mcp`, header `Authorization: Bearer <key>`.
- **MCP Inspector:** `npx @modelcontextprotocol/inspector`, transport Streamable HTTP, same URL and header.

Tools are named `<server>__<tool>`: `woo__list_orders`, `woo__create_product`, `gst__calculate_gst`,
`everything__get-sum`, and so on. Example prompts: *What orders came in today?* · *How much GST is included in today's
orders? Get the totals from the store, then use the GST calculator.*

### Use the chat agent
Dashboard → **Chat**. Ask *Create a product called "Lego Crown" priced at 1299 rupees*, or *What orders came in today?*
Each answer lists the tool calls it made (expand to see arguments and results). The agent acts as the logged-in user: its calls
are in the audit log under that user's name with source `chat`.

### Admin controls
- **Servers:** live status (Connected / Unreachable / Disabled) and tools of each; add a server by URL with an optional
  bearer token or custom header; edit, disable, enable, remove. The WooCommerce server is registered at startup.
- **Audit log:** every call with user, key, server, tool, time, duration, outcome and source; filterable. Members see
  only their own. Configuration changes are listed below it.
- Command line inside the gateway pod: `kubectl -n platform exec deploy/gateway -- gateway-admin --help`.

---

## Tests
```bash
cd backend && uv run pytest              # gateway: endpoint, auth, audit, routing, failures, admin API, chat (fake LLM)
cd woo-mcp && uv run pytest              # WooCommerce MCP server against a fake WooCommerce API
cd upstreams/gst && uv run pytest        # GST server
deploy/helm/tests/run.sh                 # charts: rendered for local and prod, checked without a cluster
scripts/tests/run.sh                     # scripts, against stand-ins for kubectl/docker/k3d/helm; and a scan
                                         # that fails if any real-looking key is in a file git would publish
```
Against real systems:
- `woo-mcp/tests/contract/`: the MCP server against a real WooCommerce (set `WOO_CONTRACT_URL`, `_KEY`, `_SECRET`).
- `scripts/cluster-test.sh` on the cluster machine: real pods, Ingress and NetworkPolicies; it calls all three servers,
  checks the audit log, tries internal services from a throwaway pod (they must be refused), times out a slow tool,
  stops and deletes pods, and revokes its own test user's key.

## Development without Kubernetes
```bash
cd backend && uv sync
export GATEWAY_SECRET_KEY=$(uv run gateway-admin generate-secret-key) GATEWAY_ALLOW_LOOPBACK=1
uv run gateway-admin create-user admin@example.com --role admin && uv run gateway-admin issue-key admin@example.com
uv run gateway                                       # http://localhost:8000/mcp ; databases in the current folder
cd ../frontend && npm ci && npm run dev              # dashboard on http://localhost:5173, forwarding /api and /mcp
```
`GATEWAY_SECRET_KEY` encrypts stored server credentials: keep the same value, or stored credentials become unreadable.
Settings are listed in `backend/src/gateway/config.py`.

---

## Beyond the core
Not built yet: provisioning more stores from the dashboard (each store is already a separate chart install in its
own namespace).
| Item | What exists | Where |
|---|---|---|
| Security hardening | SSRF checks on server URLs (metadata and link-local blocked); NetworkPolicies deny by default in both namespaces; non-root containers, read-only root filesystems; upstream credentials encrypted at rest (Fernet). No component uses the Kubernetes API, so no RBAC is needed. | `backend/src/gateway/registry/`, `deploy/helm/*/templates/networkpolicy.yaml` |
| Observability (partial) | Clear reasons for unreachable servers and failed calls, in the dashboard and the audit log; no metrics. | `Status_Codes.md`, Servers and Audit log pages |
| Upgrades and rollback | `helm upgrade`, automatic rollback of failed upgrades, `helm rollback`; setup Job re-run per revision. | `scripts/deploy-local.sh`, [design](docs/design.md#upgrade-and-rollback) |
| Undo layer | Automatic undo of a successful action whose audit record failed (durable job queue with retries). | `backend/src/gateway/compensation.py`, `undo_worker.py` |
