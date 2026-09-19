import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, download, jobs, runs, searches } from "../api";
import JobDetail from "../components/JobDetail";
import JobList from "../components/JobList";
import MoreFilters from "../components/MoreFilters";
import SearchBar from "../components/SearchBar";
import StatusBanner from "../components/StatusBanner";
import { number, whenLabel } from "../lib/format";
import { specEquals } from "../lib/spec";
import { type JobDetail as Detail, type ResultsPage, type RunSummary, type SearchDetail, type SearchSummary, type SearchSpec, type StatusResponse, emptySpec } from "../types";

const PAGE = 25;
const EMPTY: ResultsPage = { run: null, total: 0, offset: 0, items: [] };

const isMobile = () => window.matchMedia("(max-width: 767px)").matches;

/** The search screen. Everything needed to come back to the same place lives in the
 *  URL: search, run, job, sort, decision, rows loaded (n) and, on a phone, the view. */
export default function Find() {
  const [params, setParams] = useSearchParams();
  const uid = params.get("search");
  const runParam = Number(params.get("run")) || null;
  const jobParam = Number(params.get("job")) || null;
  const sort = params.get("sort") ?? "score";
  const decision = params.get("decision") === "rejected" ? "rejected" : "match";
  const nParam = Number(params.get("n")) || 0;
  const view = params.get("view") === "detail" ? "detail" : "list";

  const [list, setList] = useState<SearchSummary[]>([]);
  const [current, setCurrent] = useState<SearchDetail | null>(null);
  const [draft, setDraft] = useState<SearchSpec>(() => emptySpec());
  const [resetKey, setResetKey] = useState("init");
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [page, setPage] = useState<ResultsPage>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [expired, setExpired] = useState<RunSummary | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const listLoaded = useRef(false);

  const update = useCallback(
    (patch: Record<string, string | null>, replace = false) => {
      const next = new URLSearchParams(params);
      for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === "") next.delete(key);
        else next.set(key, value);
      }
      setParams(next, { replace });
    },
    [params, setParams],
  );

  const replaceSpec = (spec: SearchSpec) => {
    setDraft(spec);
    setResetKey(`${Date.now()}`);
  };

  // The saved searches; with no search in the URL, the most recently updated one.
  useEffect(() => {
    searches.list().then((rows) => {
      setList(rows);
      listLoaded.current = true;
      if (!uid) {
        const pick = [...rows].sort((a, b) => (b.updated_at > a.updated_at ? 1 : -1))[0];
        if (pick) update({ search: pick.uid }, true);
        else {
          setCurrent(null);
          replaceSpec(emptySpec());
          setReady(true);
        }
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The current search.
  useEffect(() => {
    if (!uid) return;
    let alive = true;
    setPage(EMPTY);
    setExpired(null);
    setDetail(null);
    searches
      .get(uid)
      .then((found) => {
        if (!alive) return;
        setCurrent(found);
        replaceSpec(found.spec);
        setReady(true);
      })
      .catch(() => update({ search: null, run: null, job: null, n: null }, true));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uid]);

  // Status: polled quickly while something is queued or running, slowly otherwise.
  const waiting = Boolean(status?.queued_request || status?.active_request);
  useEffect(() => {
    if (!uid) return;
    let alive = true;
    const tick = () =>
      searches
        .status(uid)
        .then((s) => alive && setStatus(s))
        .catch(() => undefined);
    tick();
    const id = window.setInterval(tick, waiting ? 2000 : 20000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [uid, waiting]);

  // Results for one run: the first page, then as many as the URL says were loaded.
  useEffect(() => {
    if (!uid) return;
    let alive = true;
    (async () => {
      setLoading(true);
      setExpired(null);
      try {
        const first = await searches.results(uid, { run: runParam, sort, decision, limit: PAGE, offset: 0 });
        let items = first.items;
        while (first.run && items.length < Math.min(nParam, first.total)) {
          const more = await searches.results(uid, { run: first.run.id, sort, decision, limit: PAGE, offset: items.length });
          if (!more.items.length) break;
          items = items.concat(more.items);
        }
        if (!alive) return;
        setPage({ ...first, items });
        if (!runParam && first.run) update({ run: String(first.run.id) }, true);
      } catch (e) {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 410) {
          setExpired(e.expiredLatestRun ?? { id: 0 } as RunSummary);
          setPage(EMPTY);
        } else setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uid, runParam, sort, decision]);

  // The selected job, in the context of the run on screen.
  useEffect(() => {
    if (!jobParam || !uid) {
      setDetail(null);
      return;
    }
    let alive = true;
    setDetailLoading(true);
    jobs
      .get(jobParam, uid, runParam)
      .then((d) => alive && setDetail(d))
      .catch((e) => {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 410) setExpired(e.expiredLatestRun ?? { id: 0 } as RunSummary);
        setDetail(null);
      })
      .finally(() => alive && setDetailLoading(false));
    return () => {
      alive = false;
    };
  }, [jobParam, uid, runParam]);

  // Nothing on screen and a completed run exists: show it. Only when nothing is on
  // screen -- a run the user is reading is never swapped out from under them.
  const latestId = status?.latest_run?.id ?? null;
  useEffect(() => {
    if (!uid || runParam || page.run || loading || expired || !latestId) return;
    update({ run: String(latestId) }, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uid, runParam, page.run, loading, expired, latestId]);

  const dirty = useMemo(() => (current ? !specEquals(draft, current.spec) : true), [draft, current]);

  async function loadMore() {
    if (!uid || !page.run) return;
    setLoading(true);
    try {
      const more = await searches.results(uid, { run: page.run.id, sort, decision, limit: PAGE, offset: page.items.length });
      const items = page.items.concat(more.items);
      setPage({ ...page, items, total: more.total });
      update({ n: String(items.length) }, true);
    } catch (e) {
      if (e instanceof ApiError && e.status === 410) setExpired(e.expiredLatestRun ?? { id: 0 } as RunSummary);
    } finally {
      setLoading(false);
    }
  }

  function select(id: number) {
    update({ job: String(id), ...(isMobile() ? { view: "detail" } : {}) });
  }

  async function search() {
    setBusy(true);
    setError(null);
    try {
      let target = uid;
      if (!current) {
        const created = await searches.create({ ...draft, name: draft.name.trim() || "Untitled search" });
        target = created.uid;
        setList(await searches.list());
        await runs.request(target);
        update({ search: target, run: null, job: null, n: null });
        return;
      }
      if (dirty) {
        const saved = await searches.update(current.uid, draft, current.revision);
        setCurrent({ ...current, spec: draft, name: draft.name, revision: saved.revision });
        setList(await searches.list());
      }
      await runs.request(target);
      setStatus(await searches.status(target!));
    } catch (e) {
      if (e instanceof ApiError && e.conflictRevision != null) {
        setError("This search was changed elsewhere. Reload the page to see the latest rules.");
      } else if (e instanceof ApiError && e.status === 409) {
        setError(String(e.detail));
      } else setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveAs(name: string) {
    try {
      const created = await searches.create({ ...draft, name });
      setList(await searches.list());
      update({ search: created.uid, run: null, job: null, n: null, view: null });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  function showRun(run: RunSummary) {
    setExpired(null);
    update({ run: run.id ? String(run.id) : null, job: null, n: null, view: null });
  }

  async function refreshStatus() {
    if (uid) setStatus(await searches.status(uid).catch(() => status));
  }

  const shownRun = page.run;
  // Only the run on screen describes the run on screen; /status speaks for the latest.
  const coverage = shownRun?.coverage ?? [];
  const answered = coverage.filter((c) => c.status === "ok").length;
  const scrollKey = `${uid}:${shownRun?.id ?? "none"}:${decision}:${sort}`;

  return (
    <>
      <SearchBar
        draft={draft}
        setDraft={setDraft}
        list={list}
        currentUid={current?.uid ?? null}
        dirty={dirty}
        busy={busy}
        resetKey={resetKey}
        onSearch={search}
        onOpenFilters={() => setDrawer(true)}
        onSelectSearch={(next) => update({ search: next, run: null, job: null, n: null, view: null })}
        onSaveAs={saveAs}
        onNew={() => {
          setCurrent(null);
          replaceSpec(emptySpec());
          update({ search: null, run: null, job: null, n: null, view: null });
          setPage(EMPTY);
          setStatus(null);
        }}
      />
      {error && (
        <div className="banner">
          <div className="notice error" role="alert">
            <div className="row">
              <span>{error}</span>
              <button className="link" onClick={() => setError(null)}>
                Dismiss
              </button>
            </div>
          </div>
        </div>
      )}
      <StatusBanner
        status={status}
        shownRun={shownRun}
        expired={expired}
        onShowLatest={showRun}
        onRetry={() => uid && runs.request(uid).then(refreshStatus)}
        onCancel={(id) => runs.cancel(id).then(refreshStatus)}
      />
      {ready && (
        <div className={`find view-${view}`}>
          <JobList
            items={page.items}
            total={page.total}
            selectedId={jobParam}
            onSelect={select}
            onLoadMore={loadMore}
            loading={loading}
            scrollKey={scrollKey}
          >
            <div className="line" data-testid="results-line">
              {shownRun ? (
                <>
                  <strong>Results from {whenLabel(shownRun.finished_at)}</strong>
                  <span>
                    · {number(shownRun.found)} postings read
                    {coverage.length ? ` from ${answered} of ${coverage.length} sources` : ""}
                  </span>
                </>
              ) : (
                <strong>{current ? "Not run yet" : "New search"}</strong>
              )}
            </div>
            <div className="line">
              <span data-testid="showing">
                {shownRun ? `Showing ${page.items.length} of ${page.total}` : ""}
              </span>
              <label htmlFor="sort" style={{ margin: "0 0 0 auto", fontWeight: 400 }}>
                Sort
              </label>
              <select id="sort" value={sort} onChange={(e) => update({ sort: e.target.value, n: null, job: null })}>
                <option value="score">Relevance</option>
                <option value="date">Date</option>
                <option value="company">Company</option>
                <option value="title">Title</option>
              </select>
              <button
                className="chip"
                aria-pressed={decision === "rejected"}
                onClick={() => update({ decision: decision === "rejected" ? null : "rejected", n: null, job: null })}
              >
                {decision === "rejected" ? `Rejected (${number(page.total)}) — back to matches` : `Rejected${shownRun ? ` (${number(shownRun.rejected)})` : ""}`}
              </button>
            </div>
            {shownRun && (
              <div className="line">
                <span>Export</span>
                {["csv", "json", "md"].map((format) => (
                  <button
                    key={format}
                    className="link"
                    onClick={() =>
                      uid && download(searches.exportUrl(uid, shownRun.id, format, decision), `${current?.name ?? "jobs"}-${decision}.${format}`).catch((e) => {
                        if (e instanceof ApiError && e.status === 410) setExpired(e.expiredLatestRun ?? { id: 0 } as RunSummary);
                      })
                    }
                  >
                    {format.toUpperCase()}
                  </button>
                ))}
              </div>
            )}
            {!loading && shownRun && page.total === 0 && (
              <div className="empty" data-testid="empty">
                <p>
                  Nothing matched. {number(shownRun.found)} postings were read
                  {coverage.length ? `; ${coverage.length - answered} of ${coverage.length} sources did not fully answer` : ""}.
                </p>
                <div className="row">
                  <button onClick={() => setDrawer(true)}>Loosen a filter</button>
                  {decision === "match" && (
                    <button onClick={() => update({ decision: "rejected", n: null, job: null })}>See rejected</button>
                  )}
                </div>
              </div>
            )}
            {!loading && !shownRun && !expired && (
              <div className="empty" data-testid="empty">
                <p>{current ? "Search jobs to run this search." : "Add a title or two, then Search jobs."}</p>
              </div>
            )}
          </JobList>
          <JobDetail
            detail={detail}
            loading={detailLoading}
            searchUid={current?.uid ?? null}
            revision={current?.revision ?? null}
            onStatus={(next) => {
              setDetail((d) => (d ? { ...d, status: next } : d));
              setPage((p) => ({ ...p, items: p.items.map((i) => (i.id === jobParam ? { ...i, user_status: next } : i)) }));
            }}
            onDismissed={(revision, specChanged) => {
              setCurrent((c) => (c ? { ...c, revision } : c));
              setDetail((d) => (d ? { ...d, status: "dismissed" } : d));
              if (specChanged && uid) {
                searches.get(uid).then((found) => {
                  setCurrent(found);
                  replaceSpec(found.spec);
                });
              }
              refreshStatus();
            }}
            onBack={view === "detail" ? () => update({ view: null }) : undefined}
          />
        </div>
      )}
      <MoreFilters open={drawer} draft={draft} setDraft={setDraft} onClose={() => setDrawer(false)} />
    </>
  );
}
