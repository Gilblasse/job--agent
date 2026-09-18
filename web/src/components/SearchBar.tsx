import { useEffect, useRef, useState } from "react";
import { JOB_TYPES, WORKPLACES, activeChips, clearAll, parseList } from "../lib/spec";
import type { SearchSpec, SearchSummary } from "../types";

interface Props {
  draft: SearchSpec;
  setDraft: (next: SearchSpec) => void;
  list: SearchSummary[];
  currentUid: string | null;
  dirty: boolean;
  busy: boolean;
  resetKey: string;
  onSearch: () => void;
  onOpenFilters: () => void;
  onSelectSearch: (uid: string) => void;
  onSaveAs: (name: string) => void;
  onNew: () => void;
}

type Popover = "salary" | "workplace" | "jobtype" | null;

const titleCase = (value: string) => value.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

export default function SearchBar(props: Props) {
  const { draft, setDraft, list, currentUid, dirty, busy, resetKey, onSearch, onOpenFilters } = props;
  const [titleText, setTitleText] = useState(draft.titles.join(", "));
  const [placeText, setPlaceText] = useState(draft.locations.join(", "));
  const [open, setOpen] = useState<Popover>(null);
  const [salaryText, setSalaryText] = useState(draft.salary_min ? String(draft.salary_min) : "");
  const bar = useRef<HTMLDivElement>(null);

  // The text fields are free-typed (commas mid-entry must survive), so they are only
  // re-seeded when the whole spec is replaced: another search, clear all, a reload.
  useEffect(() => {
    setTitleText(draft.titles.join(", "));
    setPlaceText(draft.locations.join(", "));
    setSalaryText(draft.salary_min ? String(draft.salary_min) : "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (bar.current && !bar.current.contains(e.target as Node)) setOpen(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(null);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const chips = activeChips(draft);
  const toggle = <T extends string>(list: T[], value: T): T[] =>
    list.includes(value) ? list.filter((v) => v !== value) : [...list, value];

  function saveAs() {
    const name = window.prompt("Name for the new search", draft.name === "Untitled search" ? "" : `${draft.name} (copy)`);
    if (name && name.trim()) props.onSaveAs(name.trim());
  }

  return (
    <div className="searchbar" ref={bar}>
      <div className="saved-select">
        <label htmlFor="saved-search" style={{ margin: 0 }}>
          Saved search
        </label>
        <select
          id="saved-search"
          value={currentUid ?? ""}
          onChange={(e) => (e.target.value ? props.onSelectSearch(e.target.value) : props.onNew())}
        >
          <option value="">New search…</option>
          {list.map((s) => (
            <option key={s.uid} value={s.uid}>
              {s.name}
            </option>
          ))}
        </select>
        <label htmlFor="search-name" style={{ margin: 0 }}>
          Name
        </label>
        <input
          id="search-name"
          type="text"
          value={draft.name}
          onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          aria-label="Search name"
        />
        <button type="button" onClick={saveAs}>
          Save as new search
        </button>
      </div>

      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          onSearch();
        }}
      >
        <div className="field">
          <label htmlFor="titles">Job titles</label>
          <input
            id="titles"
            type="search"
            placeholder="Accountant, Accounts Payable"
            value={titleText}
            onChange={(e) => {
              setTitleText(e.target.value);
              setDraft({ ...draft, titles: parseList(e.target.value, /,/) });
            }}
            aria-describedby="titles-hint"
          />
          <p className="hint" id="titles-hint">
            Matched against the posting's title only; separate titles with commas.
          </p>
        </div>
        <div className="field narrow">
          <label htmlFor="location">Location</label>
          <input
            id="location"
            type="search"
            placeholder="Dallas, Fort Worth"
            value={placeText}
            onChange={(e) => {
              setPlaceText(e.target.value);
              setDraft({ ...draft, locations: parseList(e.target.value, /,/) });
            }}
            aria-describedby="location-hint"
          />
          <p className="hint" id="location-hint">
            Cities or metros; remote roles are exempt when Remote is allowed.
          </p>
        </div>
        <div>
          <button type="submit" className="primary" disabled={busy} data-testid="search-jobs">
            {busy ? "Queuing…" : dirty || !currentUid ? "Search jobs" : "Search again"}
          </button>
        </div>
      </form>

      <div className="chips">
        <div className="popover-wrap">
          <button type="button" className={`chip${draft.salary_min ? " on" : ""}`} aria-expanded={open === "salary"} onClick={() => setOpen(open === "salary" ? null : "salary")}>
            Salary{draft.salary_min ? `: ≥ $${draft.salary_min.toLocaleString()} / ${draft.salary_period}` : ""}
          </button>
          {open === "salary" && (
            <div className="popover" role="group" aria-label="Minimum pay">
              <label htmlFor="salary-min">Minimum pay</label>
              <input
                id="salary-min"
                type="number"
                min={0}
                value={salaryText}
                onChange={(e) => {
                  setSalaryText(e.target.value);
                  const amount = Number(e.target.value.replace(/,/g, ""));
                  setDraft({ ...draft, salary_min: amount > 0 ? amount : null });
                }}
              />
              <label htmlFor="salary-period">Per</label>
              <select id="salary-period" value={draft.salary_period} onChange={(e) => setDraft({ ...draft, salary_period: e.target.value as SearchSpec["salary_period"] })}>
                <option value="year">year</option>
                <option value="hour">hour</option>
              </select>
              <button type="button" onClick={() => { setSalaryText(""); setDraft({ ...draft, salary_min: null }); setOpen(null); }}>
                Clear
              </button>
            </div>
          )}
        </div>

        <div className="popover-wrap">
          <button type="button" className={`chip${draft.workplace.length ? " on" : ""}`} aria-expanded={open === "workplace"} onClick={() => setOpen(open === "workplace" ? null : "workplace")}>
            Remote / On-site{draft.workplace.length ? `: ${draft.workplace.map((w) => WORKPLACES.find((x) => x.value === w)?.label).join(", ")}` : ""}
          </button>
          {open === "workplace" && (
            <div className="popover" role="group" aria-label="Acceptable working arrangements">
              {WORKPLACES.map((w) => (
                <label key={w.value} className="check">
                  <input type="checkbox" checked={draft.workplace.includes(w.value)} onChange={() => setDraft({ ...draft, workplace: toggle(draft.workplace, w.value) })} />
                  {w.label}
                </label>
              ))}
              <p className="hint">A posting that does not say is kept and marked unconfirmed.</p>
            </div>
          )}
        </div>

        <div className="popover-wrap">
          <button type="button" className={`chip${draft.employment_types.length ? " on" : ""}`} aria-expanded={open === "jobtype"} onClick={() => setOpen(open === "jobtype" ? null : "jobtype")}>
            Job type{draft.employment_types.length ? `: ${draft.employment_types.map(titleCase).join(", ")}` : ""}
          </button>
          {open === "jobtype" && (
            <div className="popover" role="group" aria-label="Employment types">
              {JOB_TYPES.map((t) => (
                <label key={t} className="check">
                  <input type="checkbox" checked={draft.employment_types.includes(t)} onChange={() => setDraft({ ...draft, employment_types: toggle(draft.employment_types, t) })} />
                  {titleCase(t)}
                </label>
              ))}
            </div>
          )}
        </div>

        <button type="button" className={`chip${chips.length > 3 ? " on" : ""}`} onClick={onOpenFilters} data-testid="more-filters">
          More filters
        </button>
      </div>

      {chips.length > 0 && (
        <div className="active-filters" aria-label="Active filters">
          <span>Active filters:</span>
          {chips.map((chip) => (
            <span key={chip.key} className="tag" data-testid="filter-chip">
              <span className="label">{chip.label}</span>
              <button type="button" aria-label={`Remove filter ${chip.label}`} onClick={() => setDraft(chip.clear(draft))}>
                ×
              </button>
            </span>
          ))}
          <button type="button" className="link" onClick={() => setDraft(clearAll(draft))}>
            Clear all
          </button>
        </div>
      )}
    </div>
  );
}
