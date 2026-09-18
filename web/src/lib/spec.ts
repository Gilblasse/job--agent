// What the search bar, the chips and the More-filters drawer know about a SearchSpec:
// which lists read which part of a posting, how each filter is named, and what "no
// filter" looks like so a chip can clear it.

import { DEFAULT_RANKING, SearchSpec, emptySpec } from "../types";

export type Scope = "TITLE" | "DUTIES" | "ANYWHERE" | "NAMES" | "PLACES";

export interface ListFieldSpec {
  key: keyof SearchSpec;
  label: string;
  scope: Scope;
  hint: string;
  /** Prose-scanned lists: an entry of three characters or fewer is warned about. */
  clarify?: boolean;
  chip: string;
}

// Wording follows the CLI wizard (src/jobagent/cli/wizard.py).
export const LIST_FIELDS: ListFieldSpec[] = [
  { key: "related_titles", label: "Other titles you would accept", scope: "TITLE", hint: "the same work under a different name", chip: "Also accept" },
  { key: "excluded_titles", label: "Titles to exclude outright", scope: "TITLE", hint: "any of these in the title rules the job out", chip: "Exclude titles" },
  { key: "responsibilities_include", label: "Duties the job should involve", scope: "DUTIES", hint: "a differently-titled role still qualifies if its work matches; keep phrases specific", clarify: true, chip: "Duties include" },
  { key: "responsibilities_exclude", label: "Duties you do not want", scope: "DUTIES", hint: "the work itself, not a stack listed in passing; one hit rules the job out", clarify: true, chip: "Duties exclude" },
  { key: "required_skills", label: "Skills or tools the job must mention", scope: "ANYWHERE", hint: "matched as a name in your casing", clarify: true, chip: "Must mention" },
  { key: "excluded_skills", label: "Skills or tools that rule a job out", scope: "ANYWHERE", hint: "as a name; one mention in the company blurb counts", clarify: true, chip: "Rule out" },
  { key: "keywords", label: "Keywords that rank a job higher", scope: "ANYWHERE", hint: "no job is excluded for lacking these", chip: "Rank higher" },
  { key: "required_keywords", label: "Words the posting must contain", scope: "ANYWHERE", hint: "a job without every one of these is out", clarify: true, chip: "Must contain" },
  { key: "excluded_keywords", label: "Words that rule a job out", scope: "ANYWHERE", hint: "one mention anywhere rules the job out", clarify: true, chip: "Exclude words" },
  { key: "required_credentials", label: "Credentials you hold and want the job to mention", scope: "NAMES", hint: "read in context", chip: "Mentions" },
  { key: "shift_exclude", label: "Shifts or schedules you cannot work", scope: "ANYWHERE", hint: "e.g. night shift, weekends, on-call", clarify: true, chip: "No shifts" },
  { key: "deal_breakers", label: "Anything else that rules a job out", scope: "ANYWHERE", hint: "including the company description; the widest filter here", clarify: true, chip: "Deal-breakers" },
  { key: "companies", label: "Employers you especially want", scope: "NAMES", hint: "ranked higher, never required", chip: "Prefer employers" },
  { key: "excluded_companies", label: "Employers to exclude", scope: "NAMES", hint: "", chip: "Not employers" },
  { key: "industries", label: "Industries you prefer", scope: "NAMES", hint: "ranked higher, never required", chip: "Industries" },
];

export const SENIORITY = ["intern", "entry", "mid", "senior", "staff", "management"];
export const WORKPLACES: Array<{ value: "remote" | "hybrid" | "onsite"; label: string }> = [
  { value: "remote", label: "Remote" },
  { value: "hybrid", label: "Hybrid" },
  { value: "onsite", label: "On-site" },
];
export const JOB_TYPES = ["full_time", "part_time", "contract", "internship"];

export function parseList(text: string, separator: RegExp = /\n/): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(separator)) {
    const value = raw.trim().replace(/\s+/g, " ");
    if (value && !seen.has(value.toLowerCase())) {
      seen.add(value.toLowerCase());
      out.push(value);
    }
  }
  return out;
}

export const shortEntries = (list: string[]): string[] => list.filter((item) => item.length <= 3);

export function specEquals(a: SearchSpec, b: SearchSpec): boolean {
  return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
}

function canonical(spec: SearchSpec): unknown {
  const keys = Object.keys(spec).sort() as Array<keyof SearchSpec>;
  return keys.map((key) => [key, spec[key]]);
}

export interface Chip {
  key: string;
  label: string;
  clear: (spec: SearchSpec) => SearchSpec;
}

const titleCase = (value: string) => value.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
const join = (list: string[]) => (list.length > 3 ? `${list.slice(0, 3).join(", ")} +${list.length - 3}` : list.join(", "));

/** Every non-default filter except the two in the bar (titles, location) as a chip. */
export function activeChips(spec: SearchSpec): Chip[] {
  const base = emptySpec(spec.name);
  const chips: Chip[] = [];
  if (spec.salary_min != null) {
    chips.push({
      key: "salary",
      label: `Pay ≥ $${spec.salary_min.toLocaleString()} / ${spec.salary_period}`,
      clear: (s) => ({ ...s, salary_min: null, salary_period: "year" }),
    });
  }
  if (spec.workplace.length) {
    chips.push({
      key: "workplace",
      label: spec.workplace.map((w) => WORKPLACES.find((x) => x.value === w)?.label ?? w).join(", "),
      clear: (s) => ({ ...s, workplace: [] }),
    });
  }
  if (spec.employment_types.length) {
    chips.push({
      key: "employment_types",
      label: spec.employment_types.map(titleCase).join(", "),
      clear: (s) => ({ ...s, employment_types: [] }),
    });
  }
  for (const field of LIST_FIELDS) {
    const list = spec[field.key] as string[];
    if (list.length) {
      chips.push({
        key: field.key,
        label: `${field.chip}: ${join(list)}`,
        clear: (s) => ({ ...s, [field.key]: [] }),
      });
    }
  }
  if (spec.seniority_exclude.length) {
    chips.push({
      key: "seniority_exclude",
      label: `Not ${spec.seniority_exclude.join(", ")}`,
      clear: (s) => ({ ...s, seniority_exclude: [] }),
    });
  }
  if (spec.excluded_requirements.length) {
    chips.push({
      key: "excluded_requirements",
      label: `Not if required: ${join(spec.excluded_requirements.map((r) => r.term))}`,
      clear: (s) => ({ ...s, excluded_requirements: [] }),
    });
  }
  if (spec.needs_visa_sponsorship) {
    chips.push({ key: "sponsorship", label: "Needs visa sponsorship", clear: (s) => ({ ...s, needs_visa_sponsorship: false }) });
  }
  if (spec.exclude_security_clearance) {
    chips.push({ key: "clearance", label: "No security clearance", clear: (s) => ({ ...s, exclude_security_clearance: false }) });
  }
  if (spec.max_age_days != null) {
    chips.push({ key: "max_age_days", label: `Posted within ${spec.max_age_days} days`, clear: (s) => ({ ...s, max_age_days: null }) });
  }
  if (spec.unverifiable_policy !== base.unverifiable_policy) {
    chips.push({ key: "policy", label: "Strict: exclude the unconfirmed", clear: (s) => ({ ...s, unverifiable_policy: "flag" }) });
  }
  if (spec.title_match !== base.title_match) {
    chips.push({ key: "title_match", label: "Soft title match", clear: (s) => ({ ...s, title_match: "hard" }) });
  }
  if (spec.source_budget !== base.source_budget) {
    chips.push({ key: "budget", label: `Budget ${spec.source_budget} boards`, clear: (s) => ({ ...s, source_budget: base.source_budget }) });
  }
  if (JSON.stringify(spec.ranking) !== JSON.stringify(DEFAULT_RANKING)) {
    chips.push({ key: "ranking", label: "Custom ranking", clear: (s) => ({ ...s, ranking: { ...DEFAULT_RANKING } }) });
  }
  return chips;
}

export function clearAll(spec: SearchSpec): SearchSpec {
  return { ...emptySpec(spec.name), description: spec.description, titles: spec.titles, locations: spec.locations };
}
