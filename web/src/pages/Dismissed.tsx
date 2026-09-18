import { useEffect, useState } from "react";
import { searches } from "../api";
import { dateLabel } from "../lib/format";
import type { DismissedItem, SearchSummary } from "../types";

export default function Dismissed() {
  const [list, setList] = useState<SearchSummary[]>([]);
  const [uid, setUid] = useState<string>("");
  const [items, setItems] = useState<DismissedItem[]>([]);

  useEffect(() => {
    searches.list().then((rows) => {
      setList(rows);
      if (rows[0]) setUid(rows[0].uid);
    });
  }, []);

  useEffect(() => {
    if (uid) searches.dismissed(uid).then(setItems);
  }, [uid]);

  return (
    <div className="page">
      <h1>Dismissed</h1>
      <p className="muted">Every job you turned away, with your reason and the rules it added to the search.</p>
      <label htmlFor="dismissed-search">Search</label>
      <select id="dismissed-search" value={uid} onChange={(e) => setUid(e.target.value)} style={{ width: "auto" }}>
        {list.map((s) => (
          <option key={s.uid} value={s.uid}>
            {s.name}
          </option>
        ))}
      </select>
      {items.length === 0 ? (
        <p className="muted">Nothing dismissed from this search yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th>Because</th>
              <th>Rules added</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <td>
                  {item.title}
                  <div className="muted small">{item.company}</div>
                </td>
                <td>{item.reason}</td>
                <td>{item.rules.length ? item.rules.map((r) => <div key={r.kind + r.value}>{r.label}</div>) : "—"}</td>
                <td>{dateLabel(item.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
