# Status codes and what they mean

A reference for every status, outcome and error value in the system, so I can answer "what does X mean, who produces it, and what should I do".

Values are grouped by the layer that produces them. A request travels:

```
MCP client ──► Gateway (backend/) ──► WooCommerce MCP server (woo-mcp/) ──► WooCommerce store
```

The same failure shows up differently at each layer. Section 11 traces common scenarios across all of them.

---

## 1. HTTP status codes

### 1a. Gateway (`/mcp`, `/healthz`, `/readyz`)

| Code | Where | Meaning | What to do |
|---|---|---|---|
| **200** | `/mcp` | The request was accepted and answered. **A tool failure is still HTTP 200**: the failure is inside the MCP reply as `is_error: true` (see sections 4-5). | Read the reply body. |
| **401** | `/mcp` | The API key is missing, malformed, unknown, **revoked**, or its user is **deactivated**. All five look identical on purpose, so a caller learns nothing about which keys exist. | Issue or use a valid key (`gateway-admin issue-key`). |
| **400** | `/mcp` | The request was rejected before authentication finished, for example an invalid HTTP header, or a body that is not a valid MCP message. | Fix the client. |
| **404** | any other path, e.g. `/` | There is nothing at that path. "Not found" at `http://localhost:8000/` is normal: the gateway has no web page. | Use `/mcp`, `/healthz` or `/readyz`. |
| **200** | `/healthz` | The process is up (Kubernetes liveness). Needs no key. | Nothing. |
| **200 / 503** | `/readyz` | 200 `ready`: the database answers. 503 `database unavailable`: it does not (Kubernetes readiness). Coded, not covered by a test. | Check the database file or connection. |

Some MCP clients show every rejection, including 401, as a generic "Server returned an error response". The real code is in the gateway's log.

### 1a-2. Dashboard API (`/api`)
[`api.py`](backend/src/gateway/api.py). Every error body is `{"error": "<kind>", "message": "<safe text>"}`.

| Code | `error` | Meaning |
|---|---|---|
| **200 / 201** | | Done. 201 = something was created (a server, a user, a key). |
| **401** | `unauthenticated` | No valid session: not logged in, logged out, expired (8 h), or the key used to log in was **revoked**, or the user was **deactivated**. Log in again. |
| **401** | `invalid_key` | `/api/login` only: the API key is not valid. Recorded as an `auth_failed` audit row with source `api`. |
| **403** | `forbidden` | Logged in, but this needs an admin. |
| **404** | `not_found` | Unknown server, user or key id. A member asking to revoke **someone else's** key also gets this, so they learn nothing about other people's keys. |
| **409** | `conflict` | The slug or email is already used. |
| **400** | `invalid` | Bad input, with the reason: bad slug, URL refused by the SSRF guard, missing credential, unknown field, an admin deactivating their own account, bad paging number. |
| **415** | `json_required` | A POST or PATCH without a JSON body. This is the cross-site request forgery guard: an HTML form on another site cannot send JSON. |

### 1b. WooCommerce MCP server

| Code | Where | Meaning |
|---|---|---|
| **200** | `/mcp` | Accepted. Tool failures are `is_error: true` inside the reply. |
| **401** | `/mcp` | The bearer token is not the one the gateway holds: missing, wrong, truncated, wrong case, wrong scheme, empty, whitespace-only, huge, non-ASCII, or a duplicated header with a wrong first value. Tested over a raw socket. |
| **400** | `/mcp` | Rejected at the HTTP layer (malformed request). |
| **200** | `/healthz` | The process is up. Needs no token. |
| **200 / 503** | `/readyz` | 200 `ready`: the store answered with its keys. 503 `store unavailable`: the store is down, or rejected the keys. The answer is cached for 5 seconds. No details are shown. |

### 1c. WooCommerce REST API, as seen by `woo-mcp`
How `client.py` translates what the store sends back:

| Store replies | Becomes (`WooError.kind`) | Retried? | Message to the caller |
|---|---|---|---|
| 2xx, valid JSON | (success) | | |
| 2xx, not JSON | `unexpected` | no | "The store returned a response that is not JSON." |
| **401, 403** | `auth` | no | "The store rejected the gateway's credentials. Ask an admin to check the WooCommerce API keys." |
| **404** | `not_found` | no | "Product 999 not found." / "Order 9999 not found." |
| other **4xx** (e.g. 400) | `invalid` | no | The store's own message, with HTML removed and cut to 300 characters |
| **502 / 503 / 504** | `unavailable` | **GET once**, never POST/PUT/DELETE | "The store is unavailable or returned an error." |
| other **5xx** | `unavailable` | no | same |
| timeout or network error | `unavailable` | **GET once**, never POST/PUT/DELETE | "The store is unavailable or did not answer in time." |

A deleted product that is already gone (404 on DELETE) is turned into **success** with `already_gone: true`, so the undo is safe to repeat.

---

## 2. Upstream server status (what the dashboard and `list_servers` show)

[`upstream_client.py`](backend/src/gateway/mcp/upstream_client.py)

| Status | Meaning |
|---|---|
| `connected` | The last list or call succeeded. |
| `unreachable` | The last attempt failed (dead, slow, rejected the credential, or never probed yet). A new client starts as `unreachable` until its first probe. `last_error` says why. |
| `disabled` | An admin turned it off. It is not contacted, its tools are hidden from everyone, and calls to it return `disabled`. |

## 3. Upstream error kinds (`ErrorKind`)
The reason a call to an upstream failed. They appear in `last_error`, in audit rows as `error_kind`, and in the client-facing messages below.

| Kind | Meaning | Client sees |
|---|---|---|
| `timeout` | The server did not answer within its timeout (default 10 s). Also when the MCP SDK's own request timeout fires first (error code -32001), which happens on a reused connection; found against the real `everything` server, where it used to be misreported as `unreachable`. | "The server did not answer in time." |
| `unreachable` | Could not connect, or an open connection dropped (pod killed). | "The server is currently unreachable." |
| `auth_failed` | The upstream answered 401/403 to the gateway's stored credential. | "The gateway could not authenticate to the server. Ask an admin to check its credential." |
| `upstream_error` | Anything else went wrong inside the upstream. | "The server returned an error." |
| `disabled` | The server is disabled. | "This server is disabled." |
| `unknown_tool` | No registered server owns that tool name (bad slug or tool). | "No such tool." |

Raw upstream error text is never sent to clients: it can contain URLs and other internals.

---

## 4. Gateway-only error kinds
Produced by [`service.py`](backend/src/gateway/service.py) in addition to section 3. The call may or may not have reached the upstream.

| Kind | Meaning | Did the action run? | Audited? |
|---|---|---|---|
| `audit_unavailable` | The write-ahead audit row could not be written, so the call was **refused**. | No | No (the audit store is what failed) |
| `precondition_failed` | The read taken before a changing call failed, so the call was refused (the gateway could not promise an undo). | No | Yes, as `error` |
| `resource_busy` | An earlier action on the same resource is waiting to be reversed. Retry shortly. | No | Yes, as `error` |
| `audit_failed_reversal_queued` | The action ran, but the final audit write failed. It is **being reversed automatically**. Do not rely on its result. | Yes, then undone | Row stays `started` until the database returns |
| `audit_failed_not_rolled_back` | The action ran, the audit failed, and the undo could not even be queued. An admin has been alerted. | Yes, **not** undone | Incident only |

If a changing tool has **no** registered undo and the final audit write fails, the caller still gets the normal result (fail open) and an incident is logged (section 7).

---

## 5. Audit log

[`audit/service.py`](backend/src/gateway/audit/service.py)

### Outcomes
| Outcome | Meaning |
|---|---|
| `started` | Write-ahead row: the call is about to run or is running. A row that **stays** `started` means the process died mid-call, or the final write failed and an undo is pending. |
| `success` | The tool ran and worked. |
| `tool_error` | The tool ran and reported its own error (e.g. a validation message). |
| `error` | The call never completed. `error_kind` says why. |
| `auth_failed` | A key was presented and rejected. No user is recorded; only the key's public id. |
| `denied` | A client tried to call an **internal tool** (one hidden from clients, such as `delete_product`). It was refused and never reached the upstream. Also reserved for per-role tool permissions later. |

Requests with **no key at all** are rejected with 401 but not audited, so nobody can flood the log.

### `error_kind` values that appear
Every kind from sections 3 and 4 that applies, plus:

| Value | Where | Meaning |
|---|---|---|
| `internal_tool` | `denied` rows | A client called a hidden tool such as `woo__delete_product`. Admins can see who tried. The client is told only "No such tool." |
| `invalid_key` | `auth_failed` rows | The presented key was not valid (unknown, revoked, deactivated owner, or wrong secret). |
| `reversed` | closed undo row | The action ran, could not be recorded, and was successfully **undone**. |
| `reversal_conflict` | closed undo row | The undo was **not** run because the resource changed meanwhile. Needs a human. |
| `reversal_failed` | closed undo row | The undo kept failing and was abandoned. Needs a human. |

### Sources
`mcp` = an external client such as Claude Code or Cursor. `chat` = the dashboard chat agent. `api` = a dashboard login attempt (only `auth_failed` rows).

---

## 5b. Admin events (configuration changes)

[`audit/models.py`](backend/src/gateway/audit/models.py) `admin_events`, shown with `gateway-admin events`. Separate from the tool-call audit log. Never holds a secret: only which fields changed, and old/new URLs with any password and query string removed.

Every gateway **start and restart** is recorded, whether or not anything changed.

| Action | Actor | Meaning |
|---|---|---|
| `gateway_started` | `system` | The gateway process started. Details: whether a seed file was used, and which servers were `created`, `updated` or `unchanged`. |
| `server_created` | `system:seed` | A server from the seed file did not exist yet (matched by **slug**) and was created. |
| `server_updated` | `system:seed` | A server with that slug existed but differed from the file. `changed` lists the field names (`name`, `url`, `credential_type`, `credential_header`, `timeout`, `credential`). `credential` means the secret changed; its value is never recorded. |
| `server_unchanged` | `system:seed` | The server was checked and already matched the file. |

**Actions from the CLI** are recorded too, with actor `cli:<operating-system user>` (the CLI has no login, so this is the best identity available). Only successful actions leave an event; a failed command leaves none. Secrets are never stored: keys appear only by their public id, and a credential change is recorded as `changed: ["credential"]`.

| Action | Target / details |
|---|---|
| `user_created`, `user_deactivated`, `user_activated` | email (and role for created) |
| `key_issued` | email, public key id, key name |
| `key_revoked` | public key id, owner's email |
| `server_created` | slug, URL without password or query string, credential type, enabled |
| `server_updated` | slug, `changed` (e.g. `credential`) |
| `server_enabled`, `server_disabled`, `server_removed` | slug |

If an event cannot be written, the command still succeeds and prints a warning on stderr.

The admin's enabled/disabled switch is never changed by the seed. Servers missing from the file are never deleted.

Logging: `created` and `updated` go to the gateway log at **WARNING** (`server_created_from_seed`, `server_updated_from_seed`) and to the incident JSONL file. `unchanged` is logged at INFO only.

A bad seed file (malformed JSON, a missing environment variable, a secret written into the file, an unsafe URL, a duplicate slug) makes the gateway **refuse to start** with a message beginning `refusing to start:` and applies nothing.

---

## 6. Undo jobs

[`jobs.py`](backend/src/gateway/jobs.py), [`undo_worker.py`](backend/src/gateway/undo_worker.py)

| State | Meaning |
|---|---|
| `pending` | Queued, or waiting out a retry delay (5 s, 10 s, 20 s ... up to 5 min). **Blocks other changes to that resource** (`resource_busy`). |
| `running` | A worker has claimed it. If the worker dies, the claim expires after 120 s and another worker takes over. |
| `done` | The action was reversed. The audit row is closed as `reversed`. |
| `conflict` | The resource was changed by someone else after the action, so the undo was **skipped**. Nothing was overwritten. Admin needed. |
| `failed` | Gave up (after 50 attempts, or no undo registered any more). Admin needed. |

`done`, `conflict` and `failed` unblock the resource. Each finished job also closes its audit row, retried until the audit database is back.

---

## 7. Incident events (operator alerts)
Logged at CRITICAL to the `gateway.incident` log and optionally to a JSON-lines file (`GATEWAY_INCIDENT_LOG`) that does not depend on the audit database. They never contain arguments or results.

| Event | Meaning | Action |
|---|---|---|
| `undo_queued` | Final audit write failed after a successful changing call; an undo job was queued. | None unless it is not followed by `compensated`. |
| `compensated` | An undo job completed. | None. |
| `UNDO_CONFLICT_manual_reconciliation_needed` | The undo was skipped because the resource changed. | Check the resource by hand. |
| `UNDO_FAILED_manual_reconciliation_needed` | The undo gave up. | Check the resource by hand. |
| `COMPENSATION_NOT_QUEUED_manual_reconciliation_needed` | The audit write failed and the undo could not be queued. The action stands. | Check the resource by hand. |
| `UNRECORDED_ACTION_no_undo_registered` | A changing tool with no undo ran and its audit row could not be closed. The action stands. | Reconcile the audit log. |
| `audit_write_failed_call_had_failed` | The final audit write failed, but the call itself had failed, so nothing needed undoing. | Fix the audit database. |
| `audit_write_failed_nothing_to_undo` | The audit write failed and the registered undo said there was nothing to reverse. | Fix the audit database. |

---

## 8. WooCommerce MCP tool errors
What a caller sees from the seven tools. All are `is_error: true` with a short message ([`tools.py`](woo-mcp/src/woo_mcp/tools.py)).

| Situation | Message (shortened) |
|---|---|
| Missing product or order | "Product 999 not found." / "Order 9999 not found." |
| Price not a non-negative number, or more than 4 decimals | "regular_price must be a non-negative number with at most 4 decimals, got ..." |
| Name blank, empty, or over 200 characters | A validation message naming the field |
| Unknown order status, or `per_page` above 50 | A validation message naming the field and allowed values |
| `today=true` together with dates | "use either today=true or created_after/created_before, not both." |
| A date that is not ISO 8601 | "... is not an ISO 8601 date, e.g. 2026-10-19 or 2026-10-19T14:30:00+05:30" |
| `created_after` not earlier than `created_before` | "created_after must be earlier than created_before." |
| Store rejects the API keys | "The store rejected the gateway's credentials. ..." |
| Store down or too slow | "The store is unavailable or did not answer in time." |
| An unexpected bug inside a tool | "Error calling tool '...'". The real cause is hidden from the caller and printed in the server's own log. |

Order and time notes: "today" means the store's local day in **IST (`Asia/Kolkata`, UTC+05:30)** from midnight to midnight, and times are shown with that offset.

---

## 9. Command-line and startup exit codes

| Where | Result | Meaning |
|---|---|---|
| `gateway-admin` | exit **0** | Command succeeded. |
| `gateway-admin` | exit **1** | An expected problem, printed as `error: ...` on stderr (user exists, unknown user or server, unsafe URL, bad slug, missing credential, wrong secret key). |
| `gateway-admin` | exit **2** | Wrong usage (unknown command or option). |
| `gateway`, `gateway-admin` | exits with a message | `GATEWAY_SECRET_KEY is not set`. |
| `woo_mcp` | exits with a message | A required variable is missing (named), `WOO_BASE_URL` is not http(s), `STORE_TIMEZONE` is not a valid IANA name, or `WOO_TIMEOUT` / `MCP_PORT` is not a number. |

---

## 10. API keys
Format: `wmcp_<8 hex id>_<64 hex secret>`. The id is public and appears in the audit log and CLI. The secret is shown once and only its hash is stored.

| Situation | Result |
|---|---|
| Valid key, active user | Accepted. |
| Malformed, unknown id, or wrong secret | 401, audited as `auth_failed`. |
| Revoked | 401 on the very next request, even inside an open session. |
| User deactivated | 401; keys come back when the user is reactivated. |

---

## 11. "What do I see when ...?"

| Scenario | Client sees | Audit | Dashboard / status |
|---|---|---|---|
| Normal call | The result | `success` | `connected` |
| An upstream pod is killed | `unreachable` message; other servers keep working | `error` / `unreachable` | `unreachable` |
| An upstream is slow | `timeout` message after its timeout | `error` / `timeout` | `unreachable` |
| An admin disables a server | Its tools vanish from the list; a direct call gets `disabled` | `error` / `disabled` | `disabled` |
| An admin removes a server | Its tools vanish; a direct call gets `unknown_tool` | `error` / `unknown_tool` | gone from the list |
| A key is revoked | 401 | `auth_failed` / `invalid_key` | |
| Wrong credential stored for an upstream | `auth_failed` message | `error` / `auth_failed` | `unreachable` |
| Product name does not exist | "Product N not found." | `tool_error` | `connected` |
| Store down (WooCommerce itself) | "The store is unavailable ..." | `tool_error` (the MCP server is up and reports the failure itself) | `connected` |
| Audit database down **before** a call | `audit_unavailable`; nothing ran | none | |
| Audit database down **after** a changing call, undo registered | `audit_failed_reversal_queued`; later the action is undone | `started` until it recovers, then `error` / `reversed` | |
| Same, but someone edits the order before the undo | The action stays | `error` / `reversal_conflict` | incident logged |
| Another user changes a resource whose undo is pending | `resource_busy` | `error` / `resource_busy` | |
| A client calls a hidden internal tool (`woo__delete_product`) | "No such tool." | `denied` / `internal_tool` | |

---
Known gaps and limitations (what is untested, unsupported or accepted) are in `docs/threat-model.md`.
