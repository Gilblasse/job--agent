import { useState } from "react";
import { ApiError, jobs } from "../api";
import { dateLabel, jobTypeLabel, salaryLabel, workplaceLabel } from "../lib/format";
import type { JobDetail as Detail } from "../types";
import DismissDialog from "./DismissDialog";
import Posting from "./Posting";
import WhyMatch from "./WhyMatch";

interface Props {
  detail: Detail | null;
  loading: boolean;
  searchUid: string | null;
  revision: number | null;
  onStatus: (status: string) => void;
  onDismissed: (revision: number, specChanged: boolean) => void;
  onBack?: () => void;
}

export default function JobDetail({ detail, loading, searchUid, revision, onStatus, onDismissed, onBack }: Props) {
  const [dismissing, setDismissing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (loading && !detail) return <section className="detail-pane muted">Loading…</section>;
  if (!detail) {
    return (
      <section className="detail-pane" aria-label="Selected job">
        <p className="muted">Select a job to read the posting.</p>
      </section>
    );
  }

  // The facts a screen shows come from the run's verdict when there is one -- frozen --
  // and from the current job row otherwise.
  const facts = detail.verdict ?? detail.job;
  const salary = salaryLabel(facts.salary_min, facts.salary_max, facts.salary_currency, facts.salary_period);
  const bits = [workplaceLabel(facts.workplace), jobTypeLabel(facts.employment_type), salary].filter(Boolean).join(" · ");
  const status = detail.status;

  async function setStatus(next: string) {
    setBusy(true);
    try {
      await jobs.setStatus(detail!.job.id, next);
      onStatus(next);
    } finally {
      setBusy(false);
    }
  }

  async function dismiss(reason: string, rules: Array<{ kind: string; value: string }>) {
    if (!searchUid || revision == null) return;
    setBusy(true);
    setError(null);
    try {
      const result = await jobs.dismiss(detail!.job.id, { search_uid: searchUid, revision, reason, rules });
      setDismissing(false);
      onDismissed(result.revision, result.spec_changed);
    } catch (e) {
      if (e instanceof ApiError && e.conflictRevision != null) {
        setError("This search was changed elsewhere. Reload the page and try again.");
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="detail-pane" aria-label="Selected job" role="region" aria-labelledby="job-title">
      {onBack && (
        <button className="back" onClick={onBack}>
          ← Back to results
        </button>
      )}
      <h2 id="job-title">{facts.title}</h2>
      <div className="meta">
        {facts.company}
        {facts.location_raw ? ` · ${facts.location_raw}` : ""}
        {bits && (
          <>
            <br />
            {bits}
          </>
        )}
        {facts.posted_at && (
          <>
            <br />
            Posted {dateLabel(facts.posted_at)}
          </>
        )}
      </div>
      <div className="actions">
        <a className="primary" href={facts.url} target="_blank" rel="noopener noreferrer" data-testid="apply-link">
          View job &amp; apply ↗
        </a>
        <button onClick={() => setStatus(status === "saved" ? "seen" : "saved")} disabled={busy} aria-pressed={status === "saved"}>
          {status === "saved" ? "Saved ✓" : "Save job"}
        </button>
        <button onClick={() => setStatus("applied")} disabled={busy || status === "applied"}>
          {status === "applied" ? "Applied ✓" : "Mark applied"}
        </button>
        {searchUid && (
          <button onClick={() => setDismissing(true)} disabled={busy || status === "dismissed"}>
            {status === "dismissed" ? "Dismissed" : "Not interested…"}
          </button>
        )}
      </div>
      {status === "dismissed" && detail.note && (
        <div className="notice info">Dismissed: {detail.note}</div>
      )}
      {detail.changed_since && detail.run && (
        <div className="notice warn">
          This posting changed after it was evaluated on {dateLabel(detail.run.finished_at)}; the reasons below refer
          to the earlier text.
        </div>
      )}
      <Posting sections={detail.sections} url={facts.url} />
      {detail.sources.length > 1 && (
        <div className="also-seen">
          <strong>Also seen at</strong>
          <ul>
            {detail.sources.map((s) => (
              <li key={s.url}>
                {s.source}:{" "}
                <a href={s.url} target="_blank" rel="noopener noreferrer">
                  {s.url}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
      {searchUid && (
        <WhyMatch
          explanation={detail.explanation}
          decision={detail.verdict?.decision ?? null}
          score={detail.verdict?.score ?? null}
        />
      )}
      <DismissDialog
        open={dismissing}
        suggestions={detail.suggested_rules}
        busy={busy}
        error={error}
        onCancel={() => {
          setDismissing(false);
          setError(null);
        }}
        onSubmit={dismiss}
      />
    </section>
  );
}
