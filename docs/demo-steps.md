# Demo steps

How to demonstrate every feature end to end with two users, then walk through each part of the system.
Never type or paste a real key into a file, a chat or a screen recording: keys go into hidden prompts only.

Addresses (the HP is `192.168.1.11`; if it changes, run `scripts/deploy-local.sh` again):
- Storefront: `http://shop.192.168.1.11.sslip.io` (WooCommerce admin: `/wp-admin`, user `admin`)
- Dashboard: `http://app.192.168.1.11.sslip.io`
- MCP endpoint: `http://app.192.168.1.11.sslip.io/mcp`

## 0. Before a demo
On the HP:
```bash
cd ~/wc-platform
df -h /                          # plenty free (Kubernetes stops starting pods below ~15% free)
kubectl get pods -A              # everything Running and 1/1
```
- Turn off automatic suspend (Settings → Power) and keep the HP plugged in.
- Delete test products in WooCommerce admin (Products) so the store looks clean.
- Deactivate test users in the dashboard (**Members**), and keep personal email addresses off the screen.
- At checkout, type a realistic customer name (WooCommerce's order list shows the customer's name, not the products).
- Check the store is live: open the storefront in an incognito window; it must show the shop, not "coming soon".
- No `scripts/build-images.sh` during a demo: it restarts the pods.

Windows to have open on the Mac:
1. **Admin** dashboard (normal Chrome window), logged in with the admin key.
2. **User A** dashboard (incognito window), logged in with A's key. Chrome warns about HTTPS: "Continue to site".
3. **User B** Claude Code in a terminal. Set it up once with `scripts/demo-user-b.sh setup` (hidden prompt for B's
   `claude-code` key), check with `scripts/demo-user-b.sh check` (expect `✓ Connected`), then start it with the
   command the script prints. If `claude` is not on the PATH, the script uses the copy inside the VS Code extension:
   `cd ~/demo-user-b && ~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude`.
4. Storefront in a normal tab.
5. A terminal on the HP (or `ssh <user>@192.168.1.11`) for the Kubernetes parts.

Create the users once (admin window → **Members** → Invite): `user-a@example.com` and `user-b@example.com`, both
**member**. Each invite shows the first key once. B then creates a second key (**My API keys** → `claude-code`) for
Claude Code, so revoking it later does not end B's dashboard session.

## System design
Open the diagram in `docs/design.md`.
Components and responsibilities, one sentence each:
- **Store** (WordPress + WooCommerce + MariaDB, namespace `store-main`): the shop; set up unattended by a Job.
- **woo-mcp** (mine): turns the store's REST API into MCP tools; reachable only by the gateway.
- **gst** (mine) and **everything** (official, third party): the two other upstream MCP servers.
- **Gateway** (namespace `platform`): one MCP endpoint for everyone; API keys, audit log, routing, timeouts; the
  admin API; the chat agent.
- **Dashboard**: admin and member pages, and the chat.
Then the end-to-end flow with two users: sections 1 to 4 below.

## 1. Store
1. Storefront: add a product to the cart, check out with **Cash on delivery**.
2. WooCommerce admin → **Orders**: the order is there.

## 2. Chat agent as user A
In A's window → **Chat**:
1. *Create a product called "Lego Crown" priced at 1299 rupees.*
   The reply shows **1 tool call: `woo__create_product`**; expand it to show the arguments.
2. Storefront: the Lego Crown is listed at ₹1,299. Order it by hand (Cash on delivery).
3. *What orders came in today?* The reply lists that order (tool `woo__list_orders`).
4. Optional cross-server: *How much GST is in today's orders?* (`woo__list_orders` then `gst__calculate_gst`).

Say: the agent runs inside my gateway and acts **as A**: every call is in the audit log under A with source `chat`.

## 3. External MCP client as user B
In B's Claude Code:
1. `/mcp` → **gateway**: connected, **21 tools** from 3 servers (woo 6, gst 2, everything 13). The internal
   `woo__delete_product` is not listed.
2. *What orders came in today in the WooCommerce store?*
3. Two servers in one prompt: *How much GST is included in today's WooCommerce orders? Get the order totals from the
   store, then use the GST calculator.* Two tool calls: `woo__list_orders`, `gst__calculate_gst`.

Say: B configured **one URL and one key** and never saw any server's credentials.

## 4. Team and control

### Audit log
Admin window → **Audit log**: filter by `user-a@example.com` (source `chat`), then `user-b@example.com` (source `mcp`).
Every call: user, server, tool, time, duration, outcome. **Configuration changes** below it list every admin action.
In A's or B's own window, the audit log shows only their own calls.

### Disable a server; its tools disappear from the chat and the client
Either way works; both are recorded as admin events.
- Dashboard: admin window → **Servers** → `gst` → **Disable**.
- Command line (on the HP), the gateway's admin CLI inside its pod:
  ```bash
  kubectl -n platform exec deploy/gateway -- gateway-admin disable-server gst
  kubectl -n platform exec deploy/gateway -- gateway-admin list-servers
  ```
Then:
- B's Claude Code: `/mcp` → gateway → **Reconnect**: 19 tools, no `gst__`. (Claude Code reads the tool list when it
  connects; a call to a disabled server's tool is refused immediately even before reconnecting.)
- A's chat: *What GST would apply to 1000 rupees?* The agent has no GST tool any more and says so.
- Dashboard **Servers**: `gst` shows **disabled**.
- Verified on the HP (2026-10-25): with `woo` and `gst` disabled, A's chat said it had no tool to query orders. Nothing
  was attempted, so the audit log has no failed call; the **Configuration changes** list shows `server_disabled`.
  Watch out: asked about GST with no GST tool, the model may answer from its own knowledge (₹180) or call an unrelated
  tool (`everything__echo`). Point at the tool-call list: no `gst__calculate_gst` ran, so the figure was not computed.
Turn it back on: **Enable** in the dashboard, or
`kubectl -n platform exec deploy/gateway -- gateway-admin enable-server gst`.

### Kill an upstream pod
On the HP:
```bash
kubectl -n platform scale deploy/everything --replicas=0     # the server goes away and stays away
```
- Dashboard **Servers** → **Refresh status**: `everything` is **unreachable**, with the reason; `woo` and `gst`
  connected.
- B: *What orders came in today?* still works (woo is unaffected).
```bash
kubectl -n platform scale deploy/everything --replicas=1     # back
kubectl -n platform get pods -w                              # Ready again, then Ctrl+C
```
Verified on the HP (2026-10-25).
Alternative, the literal "kill the pod" (it comes back by itself in seconds):
`kubectl -n platform delete pod -l app.kubernetes.io/name=everything`.

### A locked-out member gets a new key
If a member revokes the key they log in with, they cannot log in. Admin → **Members** → **Issue new key** on their row:
a new key, shown once, recorded as `key_issued` by the admin.

### Revoke B's key (do this last for B)
Admin window → **Members** → user-b → **revoke** the `claude-code` key. In B's Claude Code, ask anything needing a
tool: the call is **rejected**. Audit log: the **Key** column shows the same key id on B's earlier calls and on the
`auth_failed` row.
After the demo: `scripts/demo-user-b.sh setup` with a new key, or `scripts/demo-user-b.sh remove`.

## 5. Kubernetes
On the HP:
```bash
kubectl get ns                                         # platform, store-main
kubectl get pods,svc,ingress,pvc -n store-main
kubectl get pods,svc,ingress,pvc -n platform
kubectl get secrets -n store-main; kubectl get secrets -n platform     # names only; never print values
kubectl -n store-main describe deploy/woo-mcp | grep -A3 -i -E "limits|requests|readiness|liveness"
kubectl get networkpolicy -A
```
Public vs internal: only the storefront (`shop.`) and the dashboard + `/api` + `/mcp` (`app.`) have Ingress routes.
woo-mcp, gst, everything and MariaDB are ClusterIP only, and NetworkPolicies let only the gateway reach the MCP
servers. Proof: `scripts/cluster-test.sh` (a throwaway pod is refused; a control request through Traefik succeeds).
`http://app.192.168.1.11.sslip.io/healthz` answers 404 from outside: health endpoints are for the kubelet only.

## 6. MCP internals
- A tool call's path: client → Ingress → gateway (`/mcp`, key → user, audit row) → `woo-mcp` (gateway's own bearer
  token) → WooCommerce REST API → back. Diagram: `docs/design.md`.
- Name collisions: every tool is `<server>__<tool>`, so two servers' `search` tools never clash.
- Timeouts and a dead upstream: each server has its own timeout (default 10 s); the scale-to-zero demo above.
  Live timeout, in B's Claude Code: *Call everything__trigger-long-running-operation with duration 20 and steps 4.*
  After about 10 s the call fails with **"The server did not answer in time."**, and the next call to `everything`
  works normally. The audit log shows the call with outcome `error` (timeout). (Proved by `scripts/cluster-test.sh`.)
- How the gateway connects to upstreams: one MCP client per registered server over Streamable HTTP, with that server's
  stored credential (bearer token or header); tools are listed live from every enabled server in parallel.

## 7. security posture
- API keys: only a SHA-256 hash stored; shown once; revocation instant (demo 4).
- Upstream credentials: encrypted at rest, never returned (dashboard shows only "credential: bearer").
- What a member can and cannot do: B's window has no **Members** tab; a member calling an admin API gets 403.
- A malicious or compromised server: the `everything` server has risky tools (`get-env`, URL fetch); its pod has no
  Secrets and no outbound network. Its tool descriptions could still try to steer a model, so the chat treats them
  as data. See `docs/threat-model.md`.
- Known limitations: local traffic is plain HTTP; tool results go to the client's LLM provider.

## 8. chat agent
The tool loop (`backend/src/gateway/chat.py`): list A's tools → LLM (Groq, `openai/gpt-oss-120b`) → run each requested
call as A → results back → answer. Limits: 8 rounds, 12 calls, 180 s. The reply lists every tool it called.

## 9. local to VPS
- Same charts; differences only in `values-local.yaml` vs `values-prod.yaml`: host names (sslip.io → real domain),
  TLS on with a certificate Secret, Secure cookie, image registry, sizes, storage class.
- Ingress and domains: Traefik in both; locally `shop.`/`app.<ip>.sslip.io` over HTTP, in production real DNS A
  records and HTTPS.
- Storage: PVCs on k3s `local-path` locally; the provider's storage class and larger sizes in production.
- Secrets strategy: no secret in any chart, values file or image; the charts only name Secrets. Locally
  `bootstrap-secrets.sh` generates them straight into the cluster (never on a command line or printed) and
  `set-llm-key.sh` adds the LLM key; in production the same Secrets come from `kubectl create secret` or an external
  secrets manager. `python3 scripts/scan_secrets.py` checks the repository before every push.
- Upgrade and rollback, on the HP:
  ```bash
  scripts/deploy-local.sh                 # an upgrade: a new Helm revision
  helm history platform -n platform
  helm rollback platform <previous revision> -n platform
  helm history platform -n platform       # a new revision that restores the old one; data volumes untouched
  scripts/deploy-local.sh                 # back to the current version (do this before showing anything else)
  ```
  Do this section **last**: a rollback brings back the previous settings, which may be an older dashboard or chat.
