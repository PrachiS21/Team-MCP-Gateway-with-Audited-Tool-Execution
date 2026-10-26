import { useEffect, useState } from "react";
import { api, User } from "./api";
import { Login } from "./pages/Login";
import { Servers } from "./pages/Servers";
import { Members } from "./pages/Members";
import { Keys } from "./pages/Keys";
import { Audit } from "./pages/Audit";
import { Chat } from "./pages/Chat";

type Page = "servers" | "chat" | "keys" | "audit" | "members";
const PAGES: { id: Page; label: string; adminOnly?: boolean }[] = [
  { id: "servers", label: "Servers" },
  { id: "chat", label: "Chat" },
  { id: "keys", label: "My API keys" },
  { id: "audit", label: "Audit log" },
  { id: "members", label: "Members", adminOnly: true },
];

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checked, setChecked] = useState(false);
  const [page, setPage] = useState<Page>("servers");
  // Leaving an admin-only page when the logged-in user is no longer an admin.
  useEffect(() => {
    if (user && user.role !== "admin" && page === "members") setPage("servers");
  }, [user, page]);

  // Is there already a live session (cookie)? The server decides; we only ask.
  useEffect(() => {
    api.me().then((r) => setUser(r.user)).catch(() => setUser(null)).finally(() => setChecked(true));
    // Coming back to this tab: re-check, in case another tab logged in or out meanwhile.
    const onVisible = () => {
      if (document.visibilityState === "visible") api.me().then((r) => setUser(r.user)).catch(() => setUser(null));
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);

  if (!checked) return null;
  if (!user) return <Login onLogin={setUser} />;

  const logout = async () => {
    await api.logout().catch(() => undefined);
    setUser(null);
  };
  // A 401 or 403 means the session is not what this page thinks: it ended (key revoked, user deactivated, expired), or
  // another tab of this browser logged in as someone else (the cookie is shared). Ask the server who we are now.
  const onUnauthorized = () => {
    api.me().then((r) => setUser(r.user)).catch(() => setUser(null));
  };

  return (
    <div className="shell">
      <header>
        <strong>MCP Gateway</strong>
        <nav>
          {PAGES.filter((p) => !p.adminOnly || user.role === "admin").map((p) => (
            <button key={p.id} className={page === p.id ? "tab active" : "tab"} onClick={() => setPage(p.id)}>
              {p.label}
            </button>
          ))}
        </nav>
        <span className="who">
          {user.email} <span className="badge">{user.role}</span>
          <button className="link" onClick={logout}>Log out</button>
        </span>
      </header>
      <main>
        {page === "servers" && <Servers user={user} onUnauthorized={onUnauthorized} />}
        {page === "chat" && <Chat onUnauthorized={onUnauthorized} />}
        {page === "keys" && <Keys onUnauthorized={onUnauthorized} />}
        {page === "audit" && <Audit user={user} onUnauthorized={onUnauthorized} />}
        {page === "members" && user.role === "admin" && <Members user={user} onUnauthorized={onUnauthorized} />}
      </main>
    </div>
  );
}
