import { FormEvent, useEffect, useState } from "react";
import { meta } from "../api";
import type { RegistryResponse, SourcesResponse } from "../types";

export default function Sources() {
  const [sources, setSources] = useState<SourcesResponse | null>(null);
  const [registry, setRegistry] = useState<RegistryResponse | null>(null);
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => meta.registry().then(setRegistry);

  useEffect(() => {
    meta.sources().then(setSources);
    load();
  }, []);

  async function add(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const result = await meta.addBoard(url.trim(), name.trim() || undefined);
      setMessage(`Registered ${result.boards.map((b) => `${b.platform}:${b.token}`).join(", ")}.`);
      setUrl("");
      setName("");
      await load();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const total = registry ? Object.values(registry.counts).reduce((a, b) => a + b, 0) : 0;

  return (
    <div className="page">
      <h1>Sources &amp; registry</h1>
      <p className="muted">
        The registry is the reach: no ATS offers cross-company search, so a search reaches exactly the boards listed
        here. Add an employer by its board or careers URL.
      </p>
      <form onSubmit={add} className="saved-select">
        <div>
          <label htmlFor="board-url">Board or careers URL</label>
          <input id="board-url" type="text" value={url} onChange={(e) => setUrl(e.target.value)} required style={{ width: 360 }} />
        </div>
        <div>
          <label htmlFor="board-name">Employer name (optional)</label>
          <input id="board-name" type="text" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <button type="submit" className="primary" disabled={busy || !url.trim()}>
          {busy ? "Adding…" : "Add board"}
        </button>
      </form>
      {message && <p role="status">{message}</p>}

      <h2 style={{ fontSize: 17, margin: "8px 0 0" }}>Registry — {total.toLocaleString()} healthy boards</h2>
      {registry && (
        <p className="muted small">
          {Object.entries(registry.counts)
            .sort((a, b) => b[1] - a[1])
            .map(([platform, count]) => `${platform} ${count}`)
            .join(" · ")}
        </p>
      )}
      {registry && (
        <table>
          <thead>
            <tr>
              <th>Company</th>
              <th>Platform</th>
              <th>Token</th>
              <th>US</th>
              <th>Fails</th>
              <th>Last success</th>
            </tr>
          </thead>
          <tbody>
            {registry.boards.map((b) => (
              <tr key={b.id}>
                <td>{b.company}</td>
                <td>{b.ats}</td>
                <td>{b.token}</td>
                <td>{b.us_signal ? "yes" : ""}</td>
                <td>{b.consecutive_failures}</td>
                <td>{b.last_success ? b.last_success.slice(0, 10) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 style={{ fontSize: 17, margin: "8px 0 0" }}>What is read, and what is not</h2>
      {sources && (
        <>
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Kind</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {sources.sources.map((s) => (
                <tr key={s.name}>
                  <td>{s.name}</td>
                  <td>{s.kind}</td>
                  <td>{s.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <table>
            <thead>
              <tr>
                <th>Not used</th>
                <th>Why</th>
              </tr>
            </thead>
            <tbody>
              {sources.excluded.map((e) => (
                <tr key={e.name}>
                  <td>{e.name}</td>
                  <td>{e.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
