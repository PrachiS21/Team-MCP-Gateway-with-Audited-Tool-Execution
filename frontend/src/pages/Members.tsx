import { FormEvent, useState } from "react";
import { api, Role, User, when } from "../api";
import { ErrorBox, PageProps, ShowOnce, useAction, useLoad } from "./common";

export function Members({ user, onUnauthorized }: PageProps & { user: User }) {
  const { data, error, reload, setError } = useLoad(() => api.users(), onUnauthorized);
  const act = useAction(onUnauthorized, setError);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [issued, setIssued] = useState<{ email: string; key: string; invited: boolean } | null>(null);

  const invite = (e: FormEvent) => {
    e.preventDefault();
    void act(async () => {
      const r = await api.invite(email.trim(), role);
      setIssued({ email: r.user.email, key: r.api_key, invited: true });
      setEmail("");
    }, reload);
  };

  return (
    <section>
      <h2>Members</h2>
      <form className="row card" onSubmit={invite}>
        <input type="email" placeholder="email of the person to invite" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="member">member</option><option value="admin">admin</option>
        </select>
        <button>Invite</button>
      </form>
      {issued && (
        <div>
          <p>{issued.invited ? "Invited" : "New key for"} <strong>{issued.email}</strong>. Give them this key over a safe channel; they log in with it and can then create their own keys.</p>
          <ShowOnce value={issued.key} onClose={() => setIssued(null)} />
        </div>
      )}
      <ErrorBox error={error} />
      <table>
        <thead><tr><th>Email</th><th>Role</th><th>Status</th><th>Keys</th><th></th></tr></thead>
        <tbody>
          {data?.users.map((u) => (
            <tr key={u.id} className={u.active ? "" : "muted"}>
              <td>{u.email}</td>
              <td>{u.role}</td>
              <td>{u.active ? "active" : "deactivated"}</td>
              <td>
                {u.keys.map((k) => (
                  <div key={k.key_id} className="small">
                    <code>{k.key_id}</code> {k.name} · last used {when(k.last_used_at)}
                    {k.revoked_at ? <span className="muted"> · revoked</span> : (
                      <button className="link" onClick={() => {
                        if (confirm(`Revoke key ${k.key_id} of ${u.email}? Their next call with it is rejected.`)) void act(() => api.revokeKey(k.key_id), reload);
                      }}>revoke</button>
                    )}
                  </div>
                ))}
              </td>
              <td>
                {u.active && (
                  <button onClick={() => void act(async () => {
                    const r = await api.issueKeyFor(u.id, "issued by admin");
                    setIssued({ email: u.email, key: r.api_key, invited: false });
                  }, reload)}>Issue new key</button>
                )}{" "}
                {u.id !== user.id && (
                  <button onClick={() => void act(() => api.setUserActive(u.id, !u.active), reload)}>{u.active ? "Deactivate" : "Activate"}</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
