import { useEffect, useState } from "react";
import { jobs } from "../api";
import { dateLabel, salaryLabel, workplaceLabel } from "../lib/format";
import type { ResultItem } from "../types";

export default function SavedJobs() {
  const [items, setItems] = useState<ResultItem[] | null>(null);

  useEffect(() => {
    jobs.saved().then(setItems).catch(() => setItems([]));
  }, []);

  async function setStatus(id: number, status: string) {
    await jobs.setStatus(id, status);
    setItems((rows) => (rows ?? []).map((r) => (r.id === id ? { ...r, user_status: status } : r)).filter((r) => r.user_status !== "seen"));
  }

  return (
    <div className="page">
      <h1>Saved jobs</h1>
      {items === null ? (
        <p className="muted">Loading…</p>
      ) : items.length === 0 ? (
        <p className="muted">Nothing saved yet. Save a job from its details.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th>Where</th>
              <th>Status</th>
              <th>Seen</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td>
                  <a href={item.url} target="_blank" rel="noopener noreferrer">
                    {item.title}
                  </a>
                  <div className="muted small">{item.company}</div>
                </td>
                <td>
                  {item.location_raw || "—"}
                  {workplaceLabel(item.workplace) ? ` · ${workplaceLabel(item.workplace)}` : ""}
                  {salaryLabel(item.salary_min, item.salary_max, item.salary_currency, item.salary_period)
                    ? ` · ${salaryLabel(item.salary_min, item.salary_max, item.salary_currency, item.salary_period)}`
                    : ""}
                </td>
                <td>{item.user_status}</td>
                <td>{dateLabel(item.last_seen)}</td>
                <td>
                  {item.user_status !== "applied" && (
                    <button className="link" onClick={() => setStatus(item.id, "applied")}>
                      Mark applied
                    </button>
                  )}
                  <button className="link" onClick={() => setStatus(item.id, "seen")}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
