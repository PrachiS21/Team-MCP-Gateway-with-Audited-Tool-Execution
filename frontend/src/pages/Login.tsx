import { FormEvent, useState } from "react";
import { api, ApiError, User } from "../api";

export function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const { user } = await api.login(key.trim());
      setKey(""); // the key is not kept anywhere in the browser; the session cookie (HttpOnly) takes over
      onLogin(user);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the gateway.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <h1>MCP Gateway</h1>
      <p>Log in with your API key. An admin gives you your first key when they invite you.</p>
      <form onSubmit={submit}>
        <input type="password" autoComplete="off" placeholder="wmcp_…" value={key} onChange={(e) => setKey(e.target.value)} />
        <button disabled={busy || !key.trim()}>Log in</button>
      </form>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
