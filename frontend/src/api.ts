// The only code that talks to the gateway. Same origin as the dashboard, so the HttpOnly session cookie is sent
// automatically; this code never sees it. POST/PATCH always send JSON (the API refuses anything else: CSRF defence).

export type Role = "admin" | "member";
export interface User { id: string; email: string; role: Role; active: boolean; created_at: string }
export interface KeyInfo { key_id: string; name: string; created_at: string; last_used_at: string | null; revoked_at: string | null }
export interface Tool { name: string; description: string }
export interface Server {
  id: string; slug: string; name: string; enabled: boolean;
  status: "connected" | "unreachable" | "disabled";
  error: { kind: string; message: string } | null;
  tools: Tool[];
  // admins only:
  url?: string; credential_type?: string; credential_header?: string | null; has_credential?: boolean; timeout?: number;
}
export interface AuditEntry {
  id: number; ts: string; user: string | null; key_id: string | null; server: string | null; tool: string | null;
  duration_ms: number; outcome: string; error_kind: string | null; source: string;
}
export interface AdminEvent { id: number; ts: string; actor: string; action: string; server: string | null; details: Record<string, unknown> }
export interface ChatToolCall { name: string; arguments: Record<string, unknown>; ok: boolean; result: string }
export interface ChatReply { answer: string; tool_calls: ChatToolCall[] }
export interface ChatMessage { role: "user" | "assistant"; content: string }

export class ApiError extends Error {
  constructor(public status: number, public kind: string, message: string) { super(message); }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, credentials: "same-origin", headers: {} };
  if (method === "POST" || method === "PATCH") {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body ?? {});
  }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data.error ?? "error", data.message ?? `request failed (HTTP ${res.status})`);
  return data as T;
}

const q = (params: Record<string, string | number | undefined>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") s.set(k, String(v));
  return s.toString() ? `?${s}` : "";
};

export const api = {
  login: (api_key: string) => request<{ user: User }>("POST", "/api/login", { api_key }),
  logout: () => request<{ ok: boolean }>("POST", "/api/logout"),
  me: () => request<{ user: User; key_id: string }>("GET", "/api/me"),

  servers: () => request<{ servers: Server[] }>("GET", "/api/servers"),
  createServer: (s: Record<string, unknown>) => request<{ server: { id: string } }>("POST", "/api/servers", s),
  editServer: (id: string, s: Record<string, unknown>) => request("PATCH", `/api/servers/${id}`, s),
  setServerEnabled: (id: string, on: boolean) => request("POST", `/api/servers/${id}/${on ? "enable" : "disable"}`),
  deleteServer: (id: string) => request("DELETE", `/api/servers/${id}`),

  users: () => request<{ users: (User & { keys: KeyInfo[] })[] }>("GET", "/api/users"),
  invite: (email: string, role: Role) =>
    request<{ user: User; api_key: string; key_id: string }>("POST", "/api/users", { email, role }),
  issueKeyFor: (userId: string, name: string) =>
    request<{ api_key: string; key_id: string; name: string }>("POST", `/api/users/${userId}/keys`, { name }),
  setUserActive: (id: string, on: boolean) => request("POST", `/api/users/${id}/${on ? "activate" : "deactivate"}`),

  myKeys: () => request<{ keys: KeyInfo[] }>("GET", "/api/keys"),
  newKey: (name: string) => request<{ api_key: string; key_id: string; name: string }>("POST", "/api/keys", { name }),
  revokeKey: (keyId: string) => request("DELETE", `/api/keys/${keyId}`),

  audit: (f: { user?: string; server?: string; outcome?: string; limit?: number; offset?: number }) =>
    request<{ total: number; entries: AuditEntry[] }>("GET", `/api/audit${q(f)}`),
  events: (limit = 50) => request<{ events: AdminEvent[] }>("GET", `/api/events${q({ limit })}`),

  chat: (messages: ChatMessage[]) => request<ChatReply>("POST", "/api/chat", { messages }),
};

export const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "—");
