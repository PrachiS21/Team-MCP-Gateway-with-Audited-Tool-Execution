import { FormEvent, useState } from "react";
import { api, ApiError, ChatMessage, ChatToolCall } from "../api";
import { PageProps } from "./common";

interface Turn { message: ChatMessage; tools?: ChatToolCall[] }

export function Chat({ onUnauthorized }: PageProps) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = async (e: FormEvent) => {
    e.preventDefault();
    const question = text.trim();
    if (!question) return;
    const history = [...turns.map((t) => t.message), { role: "user" as const, content: question }];
    setTurns([...turns, { message: history[history.length - 1] }]);
    setText("");
    setBusy(true);
    try {
      // The gateway runs the tool loop as YOU: every tool call it makes is in the audit log under your name.
      const r = await api.chat(history);
      setTurns((t) => [...t, { message: { role: "assistant", content: r.answer }, tools: r.tool_calls }]);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return onUnauthorized();
      setError(err instanceof ApiError && err.status === 404 ? "The chat agent is not available on this gateway yet."
        : err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="chat">
      <h2>Chat</h2>
      <p className="hint">Ask about the store. The agent answers with the same tools your API key gives you, and shows which ones it called.</p>
      <div className="turns">
        {turns.map((t, i) => (
          <div key={i} className={`turn ${t.message.role}`}>
            {t.tools && t.tools.length > 0 && (
              <details className="toolcalls">
                <summary>{t.tools.length} tool call{t.tools.length === 1 ? "" : "s"}: {t.tools.map((c) => c.name).join(", ")}</summary>
                {t.tools.map((c, j) => (
                  <div key={j} className="toolcall">
                    <code className={c.ok ? "" : "error"}>{c.name}</code> <code className="small">{JSON.stringify(c.arguments)}</code>
                    <pre>{c.result}</pre>
                  </div>
                ))}
              </details>
            )}
            <div className="bubble">{t.message.content}</div>
          </div>
        ))}
        {busy && <div className="turn assistant"><div className="bubble muted">Thinking and calling tools…</div></div>}
      </div>
      {error && <p className="error">{error}</p>}
      <form className="row" onSubmit={send}>
        <input placeholder="e.g. What orders came in today?" value={text} onChange={(e) => setText(e.target.value)} disabled={busy} />
        <button disabled={busy || !text.trim()}>Send</button>
      </form>
    </section>
  );
}
