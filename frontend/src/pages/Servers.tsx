import { FormEvent, useState } from "react";
import { api, Server, User } from "../api";
import { ErrorBox, PageProps, useAction, useLoad } from "./common";

const EMPTY = { name: "", slug: "", url: "", credential_type: "none", credential: "", credential_header: "", timeout: "10" };
type Form = typeof EMPTY;

/** Build the request body. The credential is sent only when typed: leaving it empty on edit keeps the stored one. */
function body(f: Form, editing: boolean): Record<string, unknown> {
  const b: Record<string, unknown> = { name: f.name || f.slug, slug: f.slug, url: f.url,
    credential_type: f.credential_type, timeout: Number(f.timeout) };
  if (f.credential_type === "header") b.credential_header = f.credential_header;
  if (f.credential_type === "none") { if (editing) b.credential = null; }
  else if (f.credential) b.credential = f.credential;
  return b;
}

export function Servers({ user, onUnauthorized }: PageProps & { user: User }) {
  const { data, error, loading, reload, setError } = useLoad(() => api.servers(), onUnauthorized);
  const act = useAction(onUnauthorized, setError);
  const admin = user.role === "admin";
  const [form, setForm] = useState<Form | null>(null);
  const [editing, setEditing] = useState<Server | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});

  const startEdit = (s: Server) => {
    setEditing(s);
    setForm({ name: s.name, slug: s.slug, url: s.url ?? "", credential_type: s.credential_type ?? "none", credential: "",
      credential_header: s.credential_header ?? "", timeout: String(s.timeout ?? 10) });
  };
  const save = (e: FormEvent) => {
    e.preventDefault();
    if (!form) return;
    void act(() => (editing ? api.editServer(editing.id, body(form, true)) : api.createServer(body(form, false))),
      () => { setForm(null); setEditing(null); return reload(); });
  };
  const set = (k: keyof Form) => (e: { target: { value: string } }) => setForm({ ...form!, [k]: e.target.value });

  return (
    <section>
      <div className="row between">
        <h2>Servers</h2>
        <div className="row">
          <button onClick={() => void reload()} disabled={loading}>{loading ? "Checking…" : "Refresh status"}</button>
          {admin && !form && <button onClick={() => { setEditing(null); setForm({ ...EMPTY }); }}>Add server</button>}
        </div>
      </div>
      <p className="hint">Status is checked live each time this page loads. Tools are named <code>&lt;server&gt;__&lt;tool&gt;</code>, so two servers can have a tool with the same name.</p>
      <ErrorBox error={error} />

      {form && (
        <form className="card" onSubmit={save}>
          <h3>{editing ? `Edit ${editing.slug}` : "Add a server"}</h3>
          <label>Name <input value={form.name} onChange={set("name")} /></label>
          <label>Slug (tool prefix) <input value={form.slug} onChange={set("slug")} placeholder="e.g. crm" required /></label>
          <label>URL (Streamable HTTP) <input value={form.url} onChange={set("url")} placeholder="http://…/mcp" required /></label>
          <label>Credential
            <select value={form.credential_type} onChange={set("credential_type")}>
              <option value="none">none</option><option value="bearer">bearer token</option><option value="header">custom header</option>
            </select>
          </label>
          {form.credential_type === "header" && (
            <label>Header name <input value={form.credential_header} onChange={set("credential_header")} placeholder="X-Api-Key" /></label>
          )}
          {form.credential_type !== "none" && (
            <label>{editing?.has_credential ? "New credential (leave empty to keep the current one)" : "Credential"}
              <input type="password" autoComplete="off" value={form.credential} onChange={set("credential")} />
            </label>
          )}
          <label>Timeout (seconds) <input type="number" min="1" max="120" value={form.timeout} onChange={set("timeout")} /></label>
          <p className="hint">The credential is stored encrypted on the gateway and never shown again, to anyone.</p>
          <div className="row">
            <button type="submit">Save</button>
            <button type="button" onClick={() => { setForm(null); setEditing(null); }}>Cancel</button>
          </div>
        </form>
      )}

      {data?.servers.map((s) => (
        <div className="card" key={s.id}>
          <div className="row between">
            <div>
              <span className={`status ${s.status}`}>{s.status}</span> <strong>{s.name}</strong> <code>{s.slug}</code>
              {admin && <div className="hint">{s.url} · credential: {s.has_credential ? s.credential_type : "none"} · timeout {s.timeout}s</div>}
              {s.error && <div className="error small">{s.error.kind}: {s.error.message}</div>}
            </div>
            {admin && (
              <div className="row">
                <button onClick={() => void act(() => api.setServerEnabled(s.id, !s.enabled), reload)}>{s.enabled ? "Disable" : "Enable"}</button>
                <button onClick={() => startEdit(s)}>Edit</button>
                <button className="danger" onClick={() => {
                  if (confirm(`Remove ${s.slug}? Its tools disappear for everyone.`)) void act(() => api.deleteServer(s.id), reload);
                }}>Remove</button>
              </div>
            )}
          </div>
          <button className="link" onClick={() => setOpen({ ...open, [s.id]: !open[s.id] })}>
            {s.tools.length} tool{s.tools.length === 1 ? "" : "s"} {open[s.id] ? "▾" : "▸"}
          </button>
          {open[s.id] && (
            <ul className="tools">
              {s.tools.map((t) => <li key={t.name}><code>{t.name}</code> {t.description}</li>)}
            </ul>
          )}
        </div>
      ))}
    </section>
  );
}
