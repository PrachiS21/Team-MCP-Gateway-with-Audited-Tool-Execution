# Google Cloud: Cloud Run on the free tier

A second way to run the platform, next to Kubernetes, with the same code and the same images. The gateway, the
dashboard and the GST server run on Cloud Run. The store and its MCP server stay on the machine that runs the
Kubernetes cluster (the HP), and only the MCP server is put on the internet. Everything is set up to stay inside
Google Cloud's free tiers. The Kubernetes deployment stays the full-featured one; the limits of free are listed below.

**Status:** tested against a stand-in for `gcloud` (`scripts/tests/`) and with real gateways run in-process. The first
deployment to a real project is pending; "Not verified yet" lists what only that can prove.

## What runs where
```
 Browser ──────────► dashboard (Cloud Run, nginx) ──/api/──┐
 Claude Code ───────────────────────────────/mcp───────────┤
 ADK agent (your machine) ──────────────────/mcp───────────┼──► gateway (Cloud Run, 0-1 instance) ─┬─► gst (Cloud Run)
 Dialogflow CX agent ───────────────/api/cx/webhook────────┘        │                             ├─► woo-mcp on the HP,
                                                                    │                             │   through Tailscale
                                                                    ├─► Groq (chat, CX)           │   Funnel ─► WordPress
                                                                    └─► JSON logs ─► Cloud Logging ─► metrics, dashboard
```

| Part | Where | Why there |
|---|---|---|
| gateway | Cloud Run, at most 1 instance | One instance means one database (it lives in the instance's memory). |
| dashboard | Cloud Run (nginx) | It forwards `/api/` to the gateway, so the login cookie works as in Kubernetes. |
| gst | Cloud Run | Stateless; scales to zero. |
| woo-mcp | the HP, next to the store, published with Tailscale Funnel | The store's API and its WooCommerce keys never leave the HP, and a tool call crosses the internet once instead of once per store request. |
| WordPress and MariaDB | the HP (k3d) | Unchanged. |
| Dialogflow CX agent (optional) | Google | Customers' messages go to the gateway's webhook. |
| ADK agent (optional) | your machine | A Google ADK agent whose only tools are the gateway's (`adk-agent/`). |

## Staying inside the free tiers
Prices as checked on 2026-11-05 and 2026-11-06; check Google's pricing pages before relying on them.

| Service | What the scripts do | Why |
|---|---|---|
| Cloud Run | No instance while idle; CPU only while a request is served; at most 1 gateway instance, 2 of the others | An always-on gateway would be about $40 a month. Idle instances are not billed. |
| Secret Manager | 3 secrets (5 with woo-mcp on Cloud Run); the gateway's own values grouped in one, `gateway-env`; old versions destroyed | 6 secret versions are free; every version not destroyed is billed. |
| Artifact Registry | A cleanup policy keeps the 2 newest versions of each image | 0.5 GB is free. Two, so a rollback has something to go back to. |
| Cloud Build | Default machine; source upload to a regional bucket that deletes it after a day; logs only in Cloud Logging | The free build minutes are for the default machine. The default upload bucket is multi-region, which the Cloud Storage free tier does not cover. |
| Cloud Monitoring | Logs-based metrics, a dashboard and an uptime check; **no alert policies** | Alert policies are billed per condition. |
| Billing | A budget alert at 1 unit of the billing account's currency (50% and 100%) | It warns; it never stops spending. |
| Dialogflow CX | Nothing automatic | **Not free:** a $600 trial credit, then about $0.007 per text request. Delete the CX agent after a demo. |

## What free costs you
- **The gateway's database lives in its instance's memory.** Cloud Run stops an instance after about 15 idle minutes,
  and the database goes with it: the dashboard's audit view, sessions and members invited from the dashboard reset.
  What comes back by itself: the admin key (`GATEWAY_ADMIN_KEY`), the servers and the service users of the ADK agent
  and the CX webhook (the seed). Every audit line is also in Cloud Logging. The uptime check calls `/health` every 15
  minutes, so the gateway rarely sits idle long enough to stop.
- **Revoking a seeded key lasts only until the next restart,** because the seed registers it again from `gateway-env`.
  Real revocation means putting a new value in `gateway-env` and redeploying (by hand today; a rotation command is
  planned).
- **The first request after an idle spell** waits a few seconds while the service starts.
- **The undo worker gets CPU only while a request is being served,** and a pending undo is lost if the instance stops
  first. It only runs when an audit write failed after a successful action.
- **The woo tools work only while the HP is on.** The gateway's other tools keep working.
- **Upgrade paths, not free:** `--min-instances=1` (always on), or Cloud SQL for PostgreSQL (set `GATEWAY_DB_URL` and add
  the `psycopg` driver; the undo-job queue already claims jobs with a portable optimistic `UPDATE`).

## How the pieces fit
**Settings and secrets.** Cloud Run gives the gateway:
- `gateway-env` as a file, read with `GATEWAY_SECRETS_FILE`: `NAME=value` lines, where real environment variables win
  and nothing is expanded or run;
- the two upstream tokens as environment variables;
- the seed as plain config in `GATEWAY_SEED_JSON`. The seed loader refuses any secret written into a seed, so it holds
  only the names of the variables that hold them.

**Identities.** Each service runs as its own service account, which may read only its own secrets and has no project
roles; Cloud Run's default account would be a project Editor. The gateway cannot read the WooCommerce keys, and the
dashboard can read nothing.

**Logs.** With `GATEWAY_LOG_FORMAT=json` every line is one JSON object with `severity`, and with `GATEWAY_GCP_PROJECT`
also its request's trace, so Logs Explorer shows a request's lines together. One line per finished tool call
(`event=tool_call`: outcome, source, server, tool, duration_ms, user_id, key_id), per CX turn (`event=cx_turn`) and
per configuration change (`event=admin_event`). No line carries an API key, an email or a customer's words.

**The dashboard** (`frontend/nginx.cloudrun.conf.template`, the `cloudrun` stage of `frontend/Dockerfile`) serves the
same files with the same security headers as in Kubernetes, and forwards `/api/` to the gateway's Cloud Run address,
checking its certificate. To the browser, `/api` is on the dashboard's own address, so the `SameSite=Strict` login
cookie works and no cross-site requests are needed. Google's HTTPS load balancer could do the same for about $18 a
month.

**woo-mcp through Tailscale Funnel** (store chart, `publicMcp`; `scripts/tailscale-operator.sh`). Tailscale's
Kubernetes operator runs a proxy pod for an Ingress whose only backend is woo-mcp. woo-mcp's NetworkPolicy admits that
proxy and the local gateway, and nothing else.

Why not point Funnel at Traefik: Funnel passes the visitor's own `Host` header on, and Traefik picks the site by `Host`,
so a forged header would reach the storefront or the dashboard. Why not publish the store instead: the operator cannot
limit paths, so all of WordPress, including the wp-admin login, would be public.

**The Dialogflow CX webhook** (`backend/src/gateway/cx.py`, `POST /api/cx/webhook`) runs the chat agent on a
customer's message and answers in CX's format. The conversation travels in the CX session parameter `gw_history`, so
the gateway keeps no state between turns. `gw_status` says how the turn went: `ok`, `error`, `invalid`, `no_input` or
`unavailable`.

Guardrails:
- Only tools the upstream marks read-only are offered.
- An admin's key is refused.
- `gw_history` is checked like any input.
- A model failure is a polite answer with HTTP 200, so the conversation goes on.

| Setting | Default | |
|---|---|---|
| `GATEWAY_CX_ENABLED` | off | The route exists only when on. |
| `GATEWAY_CX_TIMEOUT` | 25 s | CX waits at most 30 s. |
| `GATEWAY_CX_HISTORY_TURNS` | 6 | Earlier exchanges sent back to the model (0-10). |
| `GATEWAY_CX_TOOLS` | every read-only tool | A comma-separated allowlist; it can narrow the read-only tools, never add one that changes something. |

**The ADK agent** (`adk-agent/`): reads run at once, and changes wait for the person's yes. Its key is a member's; on
Cloud Run it is `ADK_AGENT_KEY` in `gateway-env`, and its user is seeded. See [adk-agent/README.md](../adk-agent/README.md).

**Monitoring** (`deploy/gcp/observability/`):
- four logs-based metrics: tool calls by outcome, server, tool and source; tool-call latency; refused API keys; CX turns;
- the "MCP gateway" dashboard;
- an uptime check on `/health`.

`/healthz` would not work for the check: Cloud Run reserves URL paths ending in "z".

## Setup order
The commands are in [scripts/README.md](../scripts/README.md#cloud-run-google-cloud-free-tier).
1. On the HP: a free Tailscale account, `scripts/tailscale-operator.sh`, `publicMcp.enabled: true`,
   `scripts/deploy-local.sh`. From any network, `https://woo-mcp.<tailnet>.ts.net/mcp` answers 401.
2. On the Mac: a Google Cloud project with billing linked, `gcloud auth login`, then with `WOO_MCP_URL` set:
   `scripts/gcp-secrets.sh`, `scripts/gcp-deploy.sh`, `scripts/gcp-observability.sh`.
3. Optional: a Dialogflow CX agent whose webhook is `https://<gateway>/api/cx/webhook` with the CX member's key, and the
   ADK agent with its key.

`scripts/gcp-teardown.sh` deletes the Cloud Run services and the uptime check. It keeps the secrets, so a redeploy
brings back the same keys.

## Not verified yet
- **The first real deployment:** every `gcloud` flag was checked against Google's reference pages, not run.
- **The dashboard's nginx config:** run `nginx -t` in the built `cloudrun` image before deploying.
- **The Monitoring dashboard JSON and its filter syntax** (`resource.label.*`, as in Google's exported dashboards).
- **Dialogflow CX:** whether the console accepts an `Authorization` header for the webhook. If not, the gateway would
  need to take the key in another header.
- **The Tailscale operator's proxy labels**, which woo-mcp's NetworkPolicy relies on. If they were wrong, the policy
  would block the traffic rather than let more in.

## Next steps
- **Per-user tool permissions,** so that the CX and ADK keys are read-only on `/mcp` too, not only through the webhook.
- **A key rotation command** in `gcp-secrets.sh`.
- **For the CX webhook,** Dialogflow's service-agent authentication (a Google-signed token) instead of a static key.
- **For gst,** Cloud Run's own IAM authentication between services instead of a shared token.
