import { ago, whenLabel } from "../lib/format";
import type { RunSummary, StatusResponse } from "../types";

interface Props {
  status: StatusResponse | null;
  shownRun: RunSummary | null;
  expired: RunSummary | null;
  onShowLatest: (run: RunSummary) => void;
  onRetry: () => void;
  onCancel: (id: string) => void;
}

/** Waiting, searching, failed, stale rules, newer results, expired results: each state
 *  is named, and the run on screen is never swapped out from under the user. */
export default function StatusBanner({ status, shownRun, expired, onShowLatest, onRetry, onCancel }: Props) {
  if (!status) return null;
  const queued = status.queued_request;
  const active = status.active_request;
  const latestRequest = status.latest_request;
  const latest = status.latest_run;
  const newer = latest && shownRun && latest.id > shownRun.id ? latest : null;
  const priorityIsThis = (id: string | null) => id === status.search.uid;

  return (
    <div className="banner">
      {expired && (
        <div className="notice warn" role="status">
          <div className="row">
            <span>The results you were reading have expired.</span>
            {expired.id ? (
              <button className="primary" onClick={() => onShowLatest(expired)}>
                Results expired — show latest
              </button>
            ) : null}
          </div>
        </div>
      )}
      {queued && (
        <div className="notice info" role="status">
          <div className="row">
            <span>
              Waiting for an available runner
              {priorityIsThis(queued.priority_uid) ? " — your search runs first" : ""}.
            </span>
            <button className="link" onClick={() => onCancel(queued.id)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {active && (
        <div className="notice info" role="status">
          Searching… started {whenLabel(active.started_at)}, last heartbeat{" "}
          {ago(active.lease_expires_at ? new Date(new Date(active.lease_expires_at).getTime() - 300000).toISOString() : active.started_at)}.
        </div>
      )}
      {!queued && !active && latestRequest?.status === "failed" && (!latest || (latest.finished_at ?? "") < (latestRequest.finished_at ?? "")) && (
        <div className="notice error" role="alert">
          <div className="row">
            <span>Search failed: {latestRequest.note || "no details recorded"}.</span>
            <button onClick={onRetry}>Retry</button>
          </div>
        </div>
      )}
      {newer && (
        <div className="notice info" role="status">
          <div className="row">
            <span>Newer results are ready ({whenLabel(newer.finished_at)}).</span>
            <button className="primary" onClick={() => onShowLatest(newer)}>
              Newer results are ready — Show
            </button>
          </div>
        </div>
      )}
      {!newer && shownRun?.stale && (
        <div className="notice warn" role="status">
          Rules changed since these results — Search jobs to apply them.
        </div>
      )}
    </div>
  );
}
