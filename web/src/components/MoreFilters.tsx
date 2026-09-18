import { useEffect } from "react";
import { LIST_FIELDS, SENIORITY, parseList } from "../lib/spec";
import { DEFAULT_RANKING, type RankingWeights, type SearchSpec } from "../types";
import ListField from "./ListField";

interface Props {
  open: boolean;
  draft: SearchSpec;
  setDraft: (next: SearchSpec) => void;
  onClose: () => void;
}

const ROLE_KEYS = new Set([
  "related_titles", "excluded_titles", "responsibilities_include", "responsibilities_exclude",
  "required_skills", "excluded_skills", "keywords", "required_keywords", "excluded_keywords",
]);
const EMPLOYER_KEYS = new Set(["companies", "excluded_companies", "industries"]);
const CONSTRAINT_KEYS = new Set(["required_credentials", "shift_exclude", "deal_breakers"]);

/** Everything the wizard asks, in its sections and its words. Edits apply to the draft
 *  as they are made; "Search jobs" saves and runs them. */
export default function MoreFilters({ open, draft, setDraft, onClose }: Props) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const fields = (keys: Set<string>) =>
    LIST_FIELDS.filter((f) => keys.has(f.key)).map((f) => (
      <ListField
        key={f.key}
        label={f.label}
        scope={f.scope}
        hint={f.hint}
        clarify={f.clarify}
        value={draft[f.key] as string[]}
        onChange={(value) => setDraft({ ...draft, [f.key]: value })}
      />
    ));

  const setRanking = (key: keyof RankingWeights, value: number) =>
    setDraft({ ...draft, ranking: { ...draft.ranking, [key]: value } });

  return (
    <div className="drawer" role="dialog" aria-modal="true" aria-labelledby="filters-heading" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="panel">
        <h2 id="filters-heading">More filters</h2>
        <p className="legend">
          Each filter reads one part of the posting: <strong>TITLE</strong>, <strong>DUTIES</strong> (the
          responsibilities section, or the whole posting when there is none) or <strong>ANYWHERE</strong> (the entire
          posting, company blurb included). Anything that rules a job out does so on a single hit, so the wider the
          scope, the shorter and more deliberate the list should be.
        </p>

        <section>
          <h3>The role</h3>
          {fields(ROLE_KEYS)}
        </section>

        <section>
          <h3>Level</h3>
          <p className="hint">Exclude these seniority levels.</p>
          <div className="grid">
            {SENIORITY.map((level) => (
              <label key={level} className="check">
                <input
                  type="checkbox"
                  checked={draft.seniority_exclude.includes(level)}
                  onChange={() =>
                    setDraft({
                      ...draft,
                      seniority_exclude: draft.seniority_exclude.includes(level)
                        ? draft.seniority_exclude.filter((l) => l !== level)
                        : [...draft.seniority_exclude, level],
                    })
                  }
                />
                {level}
              </label>
            ))}
          </div>
        </section>

        <section>
          <h3>Terms</h3>
          <label htmlFor="max-age">Only show jobs posted within how many days? (blank for any)</label>
          <input
            id="max-age"
            type="number"
            min={1}
            value={draft.max_age_days ?? ""}
            onChange={(e) => setDraft({ ...draft, max_age_days: e.target.value ? Number(e.target.value) : null })}
          />
        </section>

        <section>
          <h3>Constraints</h3>
          <ListField
            label="Credentials that should rule a job OUT when required"
            scope="NAMES"
            hint="a licence or certification the posting REQUIRES rules it out; jobs that merely prefer it are kept"
            value={draft.excluded_requirements.map((r) => r.term)}
            onChange={(terms) =>
              setDraft({
                ...draft,
                excluded_requirements: terms.map((term) => ({ term, when: "required", unverifiable: "flag" })),
              })
            }
          />
          {fields(CONSTRAINT_KEYS)}
          <label className="check">
            <input type="checkbox" checked={draft.needs_visa_sponsorship} onChange={(e) => setDraft({ ...draft, needs_visa_sponsorship: e.target.checked })} />
            I need visa sponsorship
          </label>
          <label className="check">
            <input type="checkbox" checked={draft.exclude_security_clearance} onChange={(e) => setDraft({ ...draft, exclude_security_clearance: e.target.checked })} />
            Exclude jobs requiring a security clearance
          </label>
        </section>

        <section>
          <h3>Employers</h3>
          {fields(EMPLOYER_KEYS)}
        </section>

        <section>
          <h3>When a posting does not say</h3>
          <p className="hint">Many postings omit location, pay or working arrangement.</p>
          <select
            aria-label="Unverifiable policy"
            value={draft.unverifiable_policy}
            onChange={(e) => setDraft({ ...draft, unverifiable_policy: e.target.value as SearchSpec["unverifiable_policy"] })}
          >
            <option value="flag">Keep them, marked as unconfirmed (recommended)</option>
            <option value="strict">Exclude anything that cannot be confirmed</option>
          </select>
        </section>

        <section>
          <details>
            <summary>Advanced</summary>
            <label htmlFor="budget">Boards to read per search</label>
            <input id="budget" type="number" min={0} value={draft.source_budget} onChange={(e) => setDraft({ ...draft, source_budget: Number(e.target.value) || 0 })} />
            <label htmlFor="title-match">Title match</label>
            <select id="title-match" value={draft.title_match} onChange={(e) => setDraft({ ...draft, title_match: e.target.value as SearchSpec["title_match"] })}>
              <option value="hard">Hard: a job must be one of the titles or duties</option>
              <option value="soft">Soft: titles only rank, never exclude</option>
            </select>
            <label htmlFor="description">Description (for you)</label>
            <textarea id="description" value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
            <p className="hint" style={{ marginTop: 8 }}>
              Ranking weights: how much each kind of evidence adds to a job's score.
            </p>
            <div className="grid">
              {(Object.keys(DEFAULT_RANKING) as Array<keyof RankingWeights>).map((key) => (
                <div key={key}>
                  <label htmlFor={`rank-${key}`}>{key.replace(/_/g, " ")}</label>
                  <input id={`rank-${key}`} type="number" step={0.5} value={draft.ranking[key]} onChange={(e) => setRanking(key, Number(e.target.value) || 0)} />
                </div>
              ))}
            </div>
            <label htmlFor="countries">Countries</label>
            <input id="countries" type="text" value={draft.countries.join(", ")} onChange={(e) => setDraft({ ...draft, countries: parseList(e.target.value, /,/) })} />
            <p className="hint">This build searches US sources only.</p>
          </details>
        </section>

        <div className="actions">
          <button type="button" className="primary" onClick={onClose}>
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
