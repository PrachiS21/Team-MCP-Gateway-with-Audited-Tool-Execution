# WooCommerce MCP Gateway

A team gateway for [Model Context Protocol (MCP)](https://modelcontextprotocol.io) servers, with a WooCommerce store as its first data source.

An admin registers MCP servers once. Every team member then reaches all of them through **one MCP endpoint** with their **own API key**, without ever seeing the upstream credentials. Every tool call is attributed to a user and written to an audit log.

> **Status:** the gateway, the WooCommerce MCP server and their tests are built and run locally. The Kubernetes/Helm deployment, the real WooCommerce store, the dashboard and the chat agent are not built yet. See [Status](#status).

---

## Contents
- [Architecture](#architecture)
- [Status](#status)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Run the gateway](#run-the-gateway)
- [Try it: register a server and call a tool](#try-it-register-a-server-and-call-a-tool)
- [Run the WooCommerce MCP server](#run-the-woocommerce-mcp-server)
- [Connect an MCP client](#connect-an-mcp-client)
- [Admin command reference](#admin-command-reference)
- [Configuration](#configuration)
- [Run the tests](#run-the-tests)
- [Design notes](#design-notes)
- [Security model](#security-model)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```
                                        ┌──────────────────────────────────────────────┐
  Claude Code / Cursor /                │                  GATEWAY                     │
  MCP Inspector ── API key ───────────► │  MCP server  ← clients connect here          │
                                        │  Auth        hashed API keys, roles          │
  (later) dashboard chat agent ───────► │  Audit log   user · server · tool · outcome  │
                                        │  Undo worker reverses actions it can't log   │
                                        │  Manager     merge · namespace · route       │
                                        │  Clients     one per upstream, own timeouts  │
                                        │  Registry    servers + encrypted credentials │
                                        └───────┬──────────────────┬───────────────────┘
                              Streamable HTTP + │ stored credential│
                                                ▼                  ▼
                                 ┌─────────────────────┐   ┌──────────────┐
                                 │ WooCommerce MCP     │   │ Any other    │
                                 │ server (this repo)  │   │ MCP server   │
                                 └──────────┬──────────┘   └──────────────┘
                                            │ WooCommerce REST API (consumer key)
                                            ▼
                                     WooCommerce store
```

**How a tool call travels:** the client sends `woo__list_orders` with its API key → the gateway hashes and checks the key → the manager resolves the `woo` prefix to a registered server → that server's client calls it with the gateway's *own* stored credential and a timeout → the result comes back and an audit row is written.

**Key properties**
- **Name collisions:** tools are exposed as `<slug>__<tool>`, so two servers that both have `search` become `docs__search` and `woo__search`.
- **Failure isolation:** a slow or dead server adds no tools and its calls return a clear error. Other servers are unaffected.
- **No credential leaks:** upstream credentials stay on the gateway. A client's key is never forwarded upstream.
- **Every call audited:** a write-ahead row is written before each call. If the final write fails after a state-changing call, the action is queued for automatic reversal.

## Status

| Component | State |
|---|---|
| Gateway MCP endpoint (`/mcp`), health endpoints | Built, tested |
| Upstream client and manager (routing, timeouts, collisions) | Built, tested |
| Server registry with encrypted credentials and URL (SSRF) checks | Built, tested |
| Users, roles, hashed revocable API keys | Built, tested |
| Audit log and write-ahead recording | Built, tested |
| Async undo of unrecorded actions (retries, locks, conflict detection) | Built, tested |
| Admin CLI (`gateway-admin`) | Built, tested |
| WooCommerce MCP server (7 tools) | Built, tested against a **fake** WooCommerce API |
| Real WooCommerce store on Kubernetes | Not built |
| Helm charts, Ingress, NetworkPolicies, probes, Secrets | Not built |
| React dashboard (admin UI) | Not built |
| Chat agent (LLM tool loop) | Not built |
| Two further example upstream servers | Not built |

Behaviours that need a real store (key authentication over in-cluster HTTP, order date-filter semantics, WooCommerce install automation) are verified only against the fake so far.

## Repository layout

```
.
├── backend/                 the gateway (Python, FastMCP)
│   ├── src/gateway/
│   │   ├── server.py        MCP endpoint, key verifier, startup wiring
│   │   ├── service.py       one place that routes and audits every call
│   │   ├── mcp/             upstream client + manager (routing, timeouts)
│   │   ├── registry/        saved servers, credential encryption, URL policy
│   │   ├── auth/            users, API keys
│   │   ├── audit/           audit log
│   │   ├── jobs.py          durable undo-job store
│   │   ├── undo_worker.py   background reversal of unrecorded actions
│   │   ├── compensation.py  undo rules and hidden internal tools
│   │   ├── woo_compensation.py  undo rules for the WooCommerce server
│   │   └── cli.py           gateway-admin
│   ├── scripts/             demo upstream, reference spike
│   └── tests/
├── woo-mcp/                 the WooCommerce MCP server (separate project, own dependencies)
│   ├── src/woo_mcp/         client, tools, server
│   └── tests/               includes a fake WooCommerce REST API
└── frontend/                dashboard (empty, planned)
```

`woo-mcp` is deliberately a separate project. To the gateway it is just another upstream reached over the network, and it will run as its own container.

## Prerequisites
- **Python 3.11 or newer**
- **[uv](https://docs.astral.sh/uv/)** (manages the virtual environments)
- For the later Kubernetes work: Docker, k3d (or kind) and Helm on a machine with enough memory (16 GB recommended)

## Setup

```bash
git clone <this repository>
cd WooCommerce_MCP

# install both projects' dependencies
cd backend  && uv sync && cd ..
cd woo-mcp  && uv sync && cd ..
```

Create the gateway's settings file. It holds a secret key, so it must never be committed (`.env.*` is git-ignored):

```bash
cd backend
cat > .env.local <<EOF
export GATEWAY_SECRET_KEY=$(uv run gateway-admin generate-secret-key)
export GATEWAY_ALLOW_LOOPBACK=1
export GATEWAY_SYNC_INTERVAL=2
EOF
chmod 600 .env.local
```

- `GATEWAY_SECRET_KEY` encrypts stored upstream credentials. **Every terminal and process must use the same value.** If you change it, credentials stored under the old key become unreadable and must be re-entered.
- `GATEWAY_ALLOW_LOOPBACK=1` lets you register servers on `localhost`. This is for development only.
- `GATEWAY_SYNC_INTERVAL=2` makes the gateway pick up CLI changes within 2 seconds (default is 5).

**Load these settings in every new terminal before running any `gateway` or `gateway-admin` command:**
```bash
cd backend
source .env.local
```
A file does not set variables by itself. Each new terminal starts without them.

## Run the gateway

Create the first admin and an API key (the key is shown **once**):

```bash
cd backend && source .env.local
uv run gateway-admin create-user you@example.com --role admin
uv run gateway-admin issue-key you@example.com
```

Start the gateway (leave it running):

```bash
uv run gateway
```

It listens on port 8000. Check it:

| URL | Expected |
|---|---|
| `http://localhost:8000/healthz` | `{"status":"ok"}` |
| `http://localhost:8000/readyz` | `{"status":"ready"}` |
| `http://localhost:8000/mcp` | the MCP endpoint; needs an API key, so a browser sees an error |
| `http://localhost:8000/` | `Not found`. This is normal: the gateway has no web page |

Data is stored in `gateway.db` and `gateway_jobs.db` in the directory you start it from. Always run both `gateway` and `gateway-admin` from `backend/`, or they will use different databases.

## Try it: register a server and call a tool

There is a small demo upstream with two tools in `backend/scripts/demo_upstream.py`.

**Terminal 2: the demo upstream**
```bash
cd backend
uv run python scripts/demo_upstream.py        # listens on 127.0.0.1:9001
```

**Terminal 1: register it and call it**
```bash
cd backend && source .env.local
uv run gateway-admin add-server --slug demo --url http://127.0.0.1:9001/mcp
uv run gateway-admin list-servers             # the running gateway picks it up within the sync interval
```

```bash
KEY=wmcp_...paste-your-key... uv run python - <<'EOF'
import asyncio, os
from fastmcp import Client

async def main():
    async with Client("http://localhost:8000/mcp", auth=os.environ["KEY"]) as c:
        print([t.name for t in await c.list_tools()])
        print((await c.call_tool("demo__greet", {"name": "Ada"})).data)
        print((await c.call_tool("demo__add", {"a": 2, "b": 3})).data)

asyncio.run(main())
EOF
```

Expected output:
```
['demo__add', 'demo__greet']
Hello, Ada!
5
```

Then see the control features:
```bash
uv run gateway-admin audit                         # every call: user, tool, time, outcome
uv run gateway-admin disable-server demo           # its tools disappear within the sync interval
uv run gateway-admin enable-server demo            # and come back
uv run gateway-admin create-user bob@example.com
uv run gateway-admin issue-key bob@example.com
uv run gateway-admin list-keys bob@example.com     # shows the key id
uv run gateway-admin revoke-key KEY_ID             # Bob's very next call is rejected
```

## Run the WooCommerce MCP server

It talks to a WooCommerce store through the REST API (`/wp-json/wc/v3`) using a consumer key and secret. REST is WooCommerce's supported, versioned interface, needs no access to the database or the container, and its keys are scoped and revocable.

**Tools:** `list_products`, `get_product`, `create_product`, `delete_product` (internal, hidden from clients), `list_orders`, `get_order`, `update_order_status`. Order results omit customer contact details. "Today" means the store's local day (default `Asia/Kolkata`).

```bash
cd woo-mcp
export WOO_BASE_URL=http://your-store.example        # no /wp-json
export WOO_CONSUMER_KEY=ck_...
export WOO_CONSUMER_SECRET=cs_...
export MCP_SERVER_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
uv run python -m woo_mcp                              # listens on port 8000
```

Register it in the gateway. The token is passed through the environment and never appears as a command argument:

```bash
cd backend && source .env.local
export MCP_SERVER_TOKEN=...same value as above...
uv run gateway-admin add-server --slug woo --url http://localhost:8000/mcp \
    --credential-type bearer --credential-env MCP_SERVER_TOKEN
```

(If the gateway also runs on port 8000, give one of them a different port with `GATEWAY_PORT` or `MCP_PORT`.)

The server also exposes `/healthz` and `/readyz` without a token.

## Connect an MCP client

Any client that supports Streamable HTTP can use the gateway with only a URL and a key.

- **URL:** `http://localhost:8000/mcp`
- **Header:** `Authorization: Bearer <your API key>`

For example, with MCP Inspector (needs Node):
```bash
npx @modelcontextprotocol/inspector
```
Choose Streamable HTTP, enter the URL, and add the header.

## Admin command reference

Run as `uv run gateway-admin <command>` from `backend/` with `.env.local` loaded.

| Area | Commands |
|---|---|
| Setup | `generate-secret-key` |
| Users | `create-user EMAIL [--role admin\|member]`, `list-users`, `deactivate-user EMAIL`, `activate-user EMAIL` |
| Keys | `issue-key EMAIL`, `list-keys EMAIL`, `revoke-key KEY_ID` |
| Servers | `add-server --slug SLUG --url URL [--credential-type none\|bearer\|header] [--credential-env VAR \| --credential-stdin]`, `list-servers`, `enable-server SLUG`, `disable-server SLUG`, `remove-server SLUG`, `set-credential SLUG` |
| Audit | `audit [--user EMAIL] [--server SLUG] [--outcome OUTCOME] [--limit N]` |

Use `--help` on any command for details. Credentials are accepted only through an environment variable, stdin or a hidden prompt, never as an argument. Errors print as `error: ...` with exit code 1.

## Configuration

All settings come from environment variables, so the same build runs unchanged in Kubernetes with values from Secrets and ConfigMaps.

**Gateway**

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY_SECRET_KEY` | *(required)* | Encrypts stored upstream credentials |
| `GATEWAY_DB_URL` | `sqlite:///gateway.db` | Registry, users, keys, audit log |
| `GATEWAY_JOBS_DB_URL` | `sqlite:///gateway_jobs.db` | Undo jobs (a separate file on purpose) |
| `GATEWAY_ADMIN_EMAIL` | none | Create this admin at startup; prints a key once if it has none |
| `GATEWAY_ALLOW_LOOPBACK` | off | Allow upstream URLs on localhost (development only) |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `0.0.0.0` / `8000` | Listen address |
| `GATEWAY_INCIDENT_LOG` | none | JSON-lines file for operator alerts that survives an audit-database outage |
| `GATEWAY_WORKER_INTERVAL` | `2` | Seconds between undo-worker passes |
| `GATEWAY_SYNC_INTERVAL` | `5` | Seconds between syncing live servers with the registry |
| `GATEWAY_WOO_SLUG` | `woo` | Slug of the WooCommerce server, so its undo rules apply |

**WooCommerce MCP server**

| Variable | Default | Purpose |
|---|---|---|
| `WOO_BASE_URL` | *(required)* | Store address, without `/wp-json` |
| `WOO_CONSUMER_KEY` / `WOO_CONSUMER_SECRET` | *(required)* | WooCommerce REST API credentials |
| `MCP_SERVER_TOKEN` | *(required)* | Bearer token the gateway must present |
| `STORE_TIMEZONE` | `Asia/Kolkata` | IANA zone used for "today" and for displayed times |
| `WOO_TIMEOUT` | `10` | Seconds per store request |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Listen address |

## Run the tests

```bash
cd backend && uv run pytest        # gateway, including end-to-end tests through the real MCP server
cd woo-mcp && uv run pytest        # WooCommerce MCP server, against a fake WooCommerce API
```

The backend end-to-end tests start the gateway, the WooCommerce MCP server and a fake store as separate servers on real local ports. They cover the full flow: two users, tool listing, calls, revocation, disabled and dead upstreams, and automatic reversal when the audit write fails.

## Design notes

- **Tool-name scheme:** `<slug>__<tool>`, split on the first `__`. Slugs allow only lowercase letters, digits and single hyphens, so the split is never ambiguous.
- **Live tool listing:** every listing asks each enabled server, so a disabled or dead server disappears immediately. There is no cache. A cache can be added if listing becomes slow.
- **Stateless MCP sessions:** each request carries its key and is checked against the database, so revocation applies inside an open session and several gateway replicas could serve the same key.
- **The registry is the source of truth:** a background sync applies server changes made by the CLI or another process to the running gateway.
- **Audit with write-ahead:** a `started` row is written before each call; if that fails, the call is refused. If the final write fails after a state-changing call, the gateway queues a reversal instead of leaving an unrecorded action.
- **Reversal rules:** an undo runs in the background with retries and backoff, under the same per-resource lock as normal calls, and only if the resource still matches the state our action left (otherwise it ends as a conflict for an admin). While an undo is pending, other changes to the same resource are refused. Undo tools must be idempotent.
- **Internal tools:** `delete_product` exists only so a product creation can be undone. It is hidden from every client and refused if called; attempts are audited.
- **Audit stores metadata only:** who, which server and tool, when, how long and the outcome. Arguments and results are not stored because they can contain customer data.

## Security model

- API keys have the form `wmcp_<id>_<secret>`. Only a SHA-256 hash is stored, comparison is constant-time, and the secret is shown once.
- Revoking a key or deactivating a user takes effect on the very next request.
- Upstream credentials are encrypted at rest (Fernet) and are never returned by any API, printed by the CLI or sent to clients.
- A client's key is never forwarded to an upstream. The gateway presents its own stored credential.
- Upstream URLs are checked when saved: only `http` and `https`; the cloud-metadata range and link-local addresses are always blocked; loopback is blocked unless explicitly allowed; private cluster addresses are allowed.
- Error messages to clients are fixed strings without upstream URLs or raw exception text.
- Tool descriptions and results from upstreams are untrusted data and are passed through unchanged. An upstream could try to steer a model through them.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `GATEWAY_SECRET_KEY is not set` | The terminal has not loaded the settings. Run `source .env.local` from `backend/`. |
| `Not found` at `http://localhost:8000/` | Normal. Use `/healthz` or an MCP client on `/mcp`. |
| `address already in use` | Another gateway is on that port. Stop it, or set `GATEWAY_PORT`. |
| `error: a user with email ... already exists` | The user was created before. Run only `issue-key`. |
| `parse error near '\n'` in zsh | Do not type the angle brackets in `<placeholder>` examples. |
| Server shows `unreachable` | `list-servers` and the audit log show the reason. Check the URL and credential. |
| Tools list is empty | No server is registered or enabled, or the sync interval has not elapsed yet. |
| Stored credentials cannot be decrypted | `GATEWAY_SECRET_KEY` differs from the one used when they were saved. |
