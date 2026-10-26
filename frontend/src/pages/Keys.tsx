import { FormEvent, useState } from "react";
import { api, when } from "../api";
import { ErrorBox, PageProps, ShowOnce, useAction, useLoad } from "./common";

export function Keys({ onUnauthorized }: PageProps) {
  const { data, error, reload, setError } = useLoad(() => api.myKeys(), onUnauthorized);
  const act = useAction(onUnauthorized, setError);
  const [name, setName] = useState("");
  const [issued, setIssued] = useState<string | null>(null);
  const mcpUrl = `${window.location.origin}/mcp`;

  const create = (e: FormEvent) => {
    e.preventDefault();
    void act(async () => {
      const r = await api.newKey(name.trim() || "dashboard");
      setIssued(r.api_key);
      setName("");
    }, reload);
  };

  return (
    <section>
      <h2>My API keys</h2>
      <div className="card">
        <p>Connect Claude Code, Cursor or MCP Inspector with <strong>one URL and one key</strong>. You never need the servers' own credentials.</p>
        <p>Gateway URL: <code>{mcpUrl}</code></p>
        <p className="hint">Claude Code: <code>claude mcp add --transport http gateway {mcpUrl} --header "Authorization: Bearer &lt;your key&gt;"</code></p>
      </div>
      <form className="row card" onSubmit={create}>
        <input placeholder="name, e.g. laptop-claude-code" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
        <button>Create key</button>
      </form>
      {issued && <ShowOnce value={issued} onClose={() => setIssued(null)} />}
      <ErrorBox error={error} />
      <table>
        <thead><tr><th>Key id</th><th>Name</th><th>Created</th><th>Last used</th><th></th></tr></thead>
        <tbody>
          {data?.keys.map((k) => (
            <tr key={k.key_id} className={k.revoked_at ? "muted" : ""}>
              <td><code>{k.key_id}</code></td><td>{k.name}</td><td>{when(k.created_at)}</td><td>{when(k.last_used_at)}</td>
              <td>{k.revoked_at ? `revoked ${when(k.revoked_at)}` : (
                <button onClick={() => {
                  if (confirm(`Revoke key ${k.key_id}? Anything using it stops working now. If you logged in with it, you are logged out.`))
                    void act(() => api.revokeKey(k.key_id), reload);
                }}>Revoke</button>
              )}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
