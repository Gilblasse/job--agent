import { FormEvent, useState } from "react";
import { ApiError, meta, setPassword } from "../api";

export default function Login({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setPassword(value);
    try {
      // Any authenticated call proves the password; the search list is the cheapest.
      await fetch("/api/searches", { headers: { Authorization: `Bearer ${value}` } }).then(async (r) => {
        if (r.status === 401) throw new ApiError(401, "That password was not accepted.");
        if (r.status === 503) throw new ApiError(503, "The server has no password configured yet.");
        if (!r.ok) throw new ApiError(r.status, await r.text());
      });
      await meta.health();
      onDone();
    } catch (e) {
      setPassword("");
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <h1>Jobagent</h1>
        <p className="muted">Enter the site password to continue.</p>
        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoFocus
        />
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button className="primary" type="submit" disabled={busy || !value}>
          {busy ? "Checking…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
