
# System design and trade-offs

## What the system is
A WooCommerce store on Kubernetes, plus a team MCP gateway in front of every MCP server the team uses. An admin
registers each server once; every member then reaches all of them through **one MCP endpoint with their own API key**,
from an external client (Claude Code, Cursor, MCP Inspector) or from a chat agent in the dashboard.

```
 Browser (dashboard, chat) ─┐                         ┌─► dashboard  (app.<host>/)
 Claude Code / Cursor ──────┼─► Ingress (Traefik) ────┼─► gateway    (app.<host>/mcp, /api) ─┬─► woo-mcp ─► WordPress + WooCommerce ─► MariaDB
 Shoppers ──────────────────┘                         └─► WordPress  (shop.<host>)           │    (namespace store-main)
                                                                                             ├─► gst          (namespace platform)
 Not routed from outside: woo-mcp, gst, everything, MariaDB.                                 ├─► everything   (namespace platform)
 Only the gateway may call the MCP servers (NetworkPolicy + bearer token).                   └─► LLM API (Groq), chat only
```

| Component | Code | Runs as | Reachable from |
|---|---|---|---|
| Store | official `wordpress:7.1.3-apache` + WooCommerce 11.2.1, `mariadb:11` | Deployment + StatefulSet, both on PVCs | WordPress: Ingress `shop.<host>`. MariaDB: WordPress and the setup Job only |
| WooCommerce MCP server | `woo-mcp/` (Python, FastMCP) | Deployment, ClusterIP | the gateway only (NetworkPolicy + bearer token) |
| GST calculator | `upstreams/gst/` (mine) | Deployment, ClusterIP | the gateway only |
| MCP reference server | `upstreams/everything/` (official `@modelcontextprotocol/server-everything`, pinned) | Deployment, ClusterIP | the gateway only |
| Gateway | `backend/` (Python, FastMCP + Starlette) | Deployment, 1 replica, `Recreate`, PVC for SQLite | Ingress `app.<host>/mcp` and `/api` |
| Dashboard | `frontend/` (React + Vite, static, unprivileged nginx) | Deployment | Ingress `app.<host>/` |
| Chat agent | `backend/src/gateway/chat.py`, inside the gateway | (gateway) | `POST /api/chat` |

Two Helm charts: `deploy/helm/store` (one store, its own namespace) and `deploy/helm/platform` (gateway, dashboard, the
two extra servers). Local and production differ only by values files.

## Architecture choices
- **Python + FastMCP everywhere.** One language for the gateway and every server I wrote; FastMCP gives both an MCP
  client (to upstreams) and an MCP server (to clients) over Streamable HTTP.
- **The gateway is an MCP server whose tools are not registered in code.** Upstreams come and go at runtime, so a
  middleware answers `tools/list` and `tools/call` from the live server registry.
- **One service class, `GatewayService`, records and routes every call**, whether it comes from `/mcp` or the chat. So
  the audit log, timeouts and permissions cannot differ between the two paths.
- **The chat agent lives in the gateway, not the browser.** It acts as the logged-in user by building the same
  `Caller` the MCP path builds, and the LLM key never reaches the browser.
- **SQLite on a volume** for registry, users, keys, audit log and undo jobs. Simple and enough for one replica; the cost
  is a single gateway pod with a `Recreate` rollout (a few seconds of downtime per upgrade). Several replicas would need
  Postgres.
- **The store is configured by a Job, not by hand.** It runs `setup-store.sh` (WP-CLI): installs WordPress and
  WooCommerce, sets IST, INR, Cash on delivery, free shipping, makes the store live, and registers the REST API key the
  MCP server uses, with a value I generate into a Secret. Idempotent; the Job name carries the Helm revision, so every
  upgrade re-runs it. A Job and not an init container, because the WordPress container itself writes the files and
  `wp-config.php` when it starts.

## How the MCP server talks to the store
WooCommerce's **REST API v3** (`/wp-json/wc/v3`) with a consumer key and secret. It is WooCommerce's supported,
versioned interface; keys are scoped and revocable; it needs only HTTP between two pods. Rejected: WP-CLI through
`kubectl exec` (needs Kubernetes API rights, slow), direct database access (bypasses WooCommerce's logic), WordPress
application passwords (not scoped to WooCommerce).
WooCommerce accepts key authentication only over HTTPS. The in-cluster hop is HTTP, so woo-mcp sends
`X-Forwarded-Proto: https` and `wp-config.php` trusts it. Verified against the real store: without the header, 401.
Tools: `list_products`, `get_product`, `create_product`, `list_orders` (with "today" computed in the store's time zone,
IST), `get_order`, `update_order_status`. Order results never include customer names, emails, phones or addresses.

## Tool aggregation, routing and failures
- **Names.** Every upstream tool is exposed as `<server slug>__<tool>` (`woo__list_orders`, `gst__calculate_gst`). The
  gateway splits on the first `__` to route. Two servers may both have `search`; they become `a__search` and
  `b__search`, and one server can never shadow another's tool.
- **Listing.** `tools/list` asks every **enabled** server in parallel, each with its own timeout. A server that fails
  contributes no tools and is marked `unreachable` with the reason; the others are unaffected. Disabled servers are
  never asked, so their tools disappear at once.
- **Calling.** The name resolves to one server; the call has that server's timeout (default 10 s). Failures return a
  fixed, safe message (`The server did not answer in time.`, `…currently unreachable.`) and never an upstream URL or
  raw exception.
- **Credentials.** The gateway uses its own per-server credential (bearer token or custom header), stored encrypted.
  The client's key is never forwarded.
- **A dead pod.** When a connection breaks, the client to that server is dropped and rebuilt on the next request, so
  the first call after the pod is replaced succeeds (tested by deleting pods in the cluster).
- **Live configuration.** Admin changes (dashboard or CLI) are applied to the running gateway at once from the
  dashboard, and within 5 s from the CLI (a sync loop reads the registry).
- **Status.** The dashboard's server list asks every server live, so Connected / Unreachable / Disabled is current
  when the page loads.
- **Audit.** Every call writes a "started" row first (if that write fails, the call is refused), then the outcome,
  duration, user, key id, server, tool and source (`mcp` or `chat`). Failed logins are recorded too.
- **An extra: undo when the audit write fails after a call succeeded.** For tools with a registered undo
  (creating a product, changing an order status), the gateway queues a durable job that reverses the action, with
  retries and a lock on the resource. The undo tool (`delete_product`) is hidden from every client.

## The chat agent
For each question: list the tools this user can use now → send the conversation and those tools to the LLM (any
OpenAI-compatible API; I use Groq, `openai/gpt-oss-120b`) → run each requested call through `GatewayService` as the
user → feed results back → repeat until it answers. Limits: 8 rounds, 12 tool calls, 180 s. The model may call only
tools offered in that turn. Tool output reaches it only as tool messages, never as instructions, and is truncated. The
reply lists every tool call, with arguments and result, so a user can tell a computed answer from a guessed one.

## Kubernetes: what is public, what is internal, how it is enforced
- **Public:** `shop.<host>` → WordPress; `app.<host>` → dashboard (`/`), gateway (`/mcp`, `/api`). Nothing else has an
  Ingress route. `/healthz` and `/readyz` are not routed, only the kubelet uses them.
- **Internal:** woo-mcp, gst, everything, MariaDB: ClusterIP Services only.
- **Enforced by NetworkPolicies** in both namespaces: default deny, then one rule per allowed connection (Traefik →
  WordPress, Traefik → gateway and dashboard, gateway → each MCP server, woo-mcp → WordPress, WordPress and the setup
  Job → MariaDB, DNS). The extra servers and the dashboard may open no connections at all. The gateway may reach the
  internet only on HTTPS to public addresses, and only when the chat is configured.
- **Plus a bearer token on every MCP server**, so a pod that gets through still cannot call tools.
- **Every workload** has requests and limits, readiness and liveness probes (startup probes for WordPress and
  MariaDB), runs non-root where the image allows, and my own containers have a read-only root filesystem with only
  `/tmp` (and the gateway's `/data` volume) writable. `enableServiceLinks: false` everywhere: Kubernetes' injected
  `GATEWAY_PORT=tcp://…` variable once collided with my own setting.
- **Secrets:** no secret in a chart, values file or image. `scripts/bootstrap-secrets.sh` generates them straight into
  Kubernetes Secrets; the shared MCP token exists in both namespaces; `set-llm-key.sh` stores the LLM key.

## Local versus production
Same charts; only values change (`values-local.yaml` → `values-prod.yaml`):

| | Local (k3d on a LAN machine) | Production (VPS with k3s) |
|---|---|---|
| DNS | `shop.<ip>.sslip.io`, `app.<ip>.sslip.io` (a public DNS service that resolves to the IP; no hosts files) | real domain, DNS A records to the VPS |
| Ingress / TLS | Traefik, plain HTTP | Traefik, TLS with a certificate Secret (e.g. from cert-manager); session cookie marked `Secure` |
| Storage class | k3s `local-path` | `local-path` or the provider's class; larger PVCs |
| Images | built on the cluster machine, `k3d image import`, tag `dev` | pushed to a registry, pinned tags |
| Secrets | generated by `bootstrap-secrets.sh` | the same script, or an external secrets manager; the charts only reference Secrets by name |
| Resources | small | larger requests and limits |

## Upgrade and rollback
`scripts/deploy-local.sh` runs `helm upgrade --install` for both charts, with `--wait` (and `--wait-for-jobs` for the
store). An upgrade that fails is rolled back automatically (`--rollback-on-failure`, Helm 4); a failed **first** install
is left in place to debug. Manual rollback: `helm history <release> -n <ns>`, then `helm rollback <release> <rev>`.
Volumes are untouched by both. Limits: no database schema migrations (tables are created on first start), and the
gateway's `Recreate` rollout has a short outage.

## Trade-offs and what I did not do
- One gateway replica (SQLite). Scaling would need Postgres and shared locks.
- Dashboard login is "paste an API key", exchanged for an HttpOnly session cookie. Faster to build than invite links
  with passwords, and it works with the same keys MCP clients use.
- No per-tool permissions per role: a member can call every enabled tool. The audit log and instant revocation are the
  controls.
- MCP clients learn about a disabled server when they next list tools; the gateway does not push change notifications.
  Calls to a disabled server are refused immediately either way.
- The GST server estimates tax from totals; a real store would configure WooCommerce's own tax engine.
- See `docs/threat-model.md` for the security trade-offs and known limitations.
