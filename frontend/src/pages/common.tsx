import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api";

export interface PageProps { onUnauthorized: () => void }

/** Load data for a page; reload() refetches. A 401/403 makes the app re-check who is logged in (see App.tsx). */
export function useLoad<T>(load: () => Promise<T>, onUnauthorized: () => void) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // reload() is created once, so it must call the LATEST load function (which sees the page's current filters),
  // not the one from the first render.
  const loadRef = useRef(load);
  loadRef.current = load;
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) onUnauthorized();
      if (!(e instanceof ApiError && e.status === 401)) setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { void reload(); }, [reload]);
  return { data, error, loading, reload, setError };
}

/** Run an action (button click); show its error, or call done() after success. */
export function useAction(onUnauthorized: () => void, setError: (e: string | null) => void) {
  return async (fn: () => Promise<unknown>, done?: () => unknown) => {
    try {
      await fn();
      setError(null);
      await done?.();
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) onUnauthorized();
      if (!(e instanceof ApiError && e.status === 401)) setError(e instanceof Error ? e.message : String(e));
    }
  };
}

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <p className="error">{error}</p> : null;
}

/** A newly created API key. It exists in the browser only now; the gateway keeps just its hash. */
export function ShowOnce({ value, onClose }: { value: string; onClose: () => void }) {
  const [copied, setCopied] = useState<"" | "yes" | "no">("");
  return (
    <div className="once">
      <p><strong>Copy this key now.</strong> It is shown once and cannot be recovered.</p>
      <code className="selectable">{value}</code>
      <div className="row">
        <button onClick={async () => setCopied((await copyText(value)) ? "yes" : "no")}>Copy</button>
        <button onClick={onClose}>I have saved it</button>
        {copied === "yes" && <span className="hint">Copied.</span>}
        {copied === "no" && <span className="error small">Could not copy. Click the key to select it, then press Cmd+C.</span>}
      </div>
    </div>
  );
}

/** Copy text to the clipboard. The modern API exists only on HTTPS or localhost; the local setup is plain HTTP, so
 *  fall back to the older copy command on a temporary text field, which still works there. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the older method
  }
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.appendChild(field);
  field.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(field);
  }
}
