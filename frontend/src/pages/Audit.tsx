import { useState } from "react";
import { api, User, when } from "../api";
import { ErrorBox, PageProps, useLoad } from "./common";

const PAGE = 50;

export function Audit({ user, onUnauthorized }: PageProps & { user: User }) {
  const admin = user.role === "admin";
  const [filter, setFilter] = useState({ user: "", server: "", outcome: "" });
  const [offset, setOffset] = useState(0);
  const calls = useLoad(() => api.audit({ ...filter, limit: PAGE, offset }), onUnauthorized);
  const events = useLoad(() => (admin ? api.events(30) : Promise.resolve({ events: [] })), onUnauthorized);

  const apply = (o = 0) => { setOffset(o); setTimeout(() => void calls.reload(), 0); };
  const set = (k: keyof typeof filter) => (e: { target: { value: string } }) => setFilter({ ...filter, [k]: e.target.value });

  return (
    <section>
      <h2>Audit log</h2>
      <p className="hint">{admin ? "Every tool call by everyone." : "Your own tool calls."} Calls from the chat show source "chat".</p>
      <div className="row card">
        {admin && <input placeholder="user email" value={filter.user} onChange={set("user")} />}
        <input placeholder="server slug" value={filter.server} onChange={set("server")} />
        <select value={filter.outcome} onChange={set("outcome")}>
          <option value="">any outcome</option>
          {["success", "tool_error", "error", "auth_failed", "denied", "started"].map((o) => <option key={o}>{o}</option>)}
        </select>
        <button onClick={() => apply(0)}>Filter</button>
      </div>
      <ErrorBox error={calls.error} />
      <table>
        <thead><tr><th>Time</th><th>User</th><th>Key</th><th>Server</th><th>Tool</th><th>ms</th><th>Outcome</th><th>Source</th></tr></thead>
        <tbody>
          {calls.data?.entries.map((e) => (
            <tr key={e.id}>
              <td>{when(e.ts)}</td><td>{e.user ?? <span className="muted">unknown</span>}</td>
              <td><code>{e.key_id ?? "—"}</code></td>
              <td>{e.server ?? "—"}</td><td>{e.tool ?? "—"}</td><td>{e.duration_ms}</td>
              <td><span className={`outcome ${e.outcome}`}>{e.outcome}</span>{e.error_kind ? ` (${e.error_kind})` : ""}</td>
              <td>{e.source}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {calls.data && (
        <div className="row">
          <button disabled={offset === 0} onClick={() => apply(Math.max(0, offset - PAGE))}>Newer</button>
          <span>{calls.data.total ? `${offset + 1}–${Math.min(offset + PAGE, calls.data.total)} of ${calls.data.total}` : "no calls"}</span>
          <button disabled={offset + PAGE >= calls.data.total} onClick={() => apply(offset + PAGE)}>Older</button>
        </div>
      )}

      {admin && (
        <>
          <h3>Configuration changes</h3>
          <table>
            <thead><tr><th>Time</th><th>Who</th><th>Action</th><th>Server</th><th>Details</th></tr></thead>
            <tbody>
              {events.data?.events.map((e) => (
                <tr key={e.id}><td>{when(e.ts)}</td><td>{e.actor}</td><td>{e.action}</td><td>{e.server ?? "—"}</td>
                  <td className="small">{JSON.stringify(e.details)}</td></tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
