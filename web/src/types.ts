// Mirrors the API's shapes (src/jobagent/web/app.py) and the SearchSpec fields
// (src/jobagent/domain/spec.py). Lists default to empty; empty means "no constraint".

export type Workplace = "remote" | "hybrid" | "onsite";

export interface ExcludedRequirement {
  term: string;
  when: "required" | "preferred_or_required" | "mentioned";
  unverifiable: "flag" | "strict";
}

export interface RankingWeights {
  title_match: number;
  related_title_match: number;
  keyword_match: number;
  required_skill_match: number;
  responsibility_match: number;
  industry_match: number;
  target_company_match: number;
  preferred_workplace: number;
  preferred_location: number;
  salary_disclosed: number;
  salary_above_floor: number;
  freshness: number;
  authority_bonus: number;
  uncertainty_penalty: number;
}

export interface SearchSpec {
  name: string;
  description: string;
  titles: string[];
  related_titles: string[];
  excluded_titles: string[];
  title_match: "hard" | "soft";
  keywords: string[];
  required_keywords: string[];
  excluded_keywords: string[];
  required_skills: string[];
  excluded_skills: string[];
  responsibilities_include: string[];
  responsibilities_exclude: string[];
  workplace: Workplace[];
  countries: string[];
  locations: string[];
  employment_types: string[];
  seniority_exclude: string[];
  salary_min: number | null;
  salary_period: "year" | "month" | "week" | "day" | "hour";
  industries: string[];
  companies: string[];
  excluded_companies: string[];
  excluded_requirements: ExcludedRequirement[];
  required_credentials: string[];
  shift_exclude: string[];
  needs_visa_sponsorship: boolean;
  exclude_security_clearance: boolean;
  deal_breakers: string[];
  max_age_days: number | null;
  unverifiable_policy: "flag" | "strict";
  ranking: RankingWeights;
  source_budget: number;
}

export const DEFAULT_RANKING: RankingWeights = {
  title_match: 10,
  related_title_match: 6,
  keyword_match: 2,
  required_skill_match: 4,
  responsibility_match: 3,
  industry_match: 2,
  target_company_match: 8,
  preferred_workplace: 5,
  preferred_location: 4,
  salary_disclosed: 1.5,
  salary_above_floor: 3,
  freshness: 3,
  authority_bonus: 2,
  uncertainty_penalty: -4,
};

export function emptySpec(name = "Untitled search"): SearchSpec {
  return {
    name,
    description: "",
    titles: [],
    related_titles: [],
    excluded_titles: [],
    title_match: "hard",
    keywords: [],
    required_keywords: [],
    excluded_keywords: [],
    required_skills: [],
    excluded_skills: [],
    responsibilities_include: [],
    responsibilities_exclude: [],
    workplace: [],
    countries: ["US"],
    locations: [],
    employment_types: [],
    seniority_exclude: [],
    salary_min: null,
    salary_period: "year",
    industries: [],
    companies: [],
    excluded_companies: [],
    excluded_requirements: [],
    required_credentials: [],
    shift_exclude: [],
    needs_visa_sponsorship: false,
    exclude_security_clearance: false,
    deal_breakers: [],
    max_age_days: null,
    unverifiable_policy: "flag",
    ranking: { ...DEFAULT_RANKING },
    source_budget: 1500,
  };
}

export interface SearchSummary {
  id?: number;
  uid: string;
  name: string;
  revision: number;
  updated_at: string;
  runs?: number;
  last_run?: string | null;
}

export interface SearchDetail extends SearchSummary {
  spec: SearchSpec;
}

export interface RunSummary {
  id: number;
  search_id: number;
  search_uid: string | null;
  started_at: string;
  finished_at: string | null;
  status: string;
  found: number;
  matched: number;
  rejected: number;
  new_count: number;
  spec_revision: number | null;
  request_id: string | null;
  stale: boolean;
  coverage?: Coverage[];
}

export interface Gate {
  gate: string;
  outcome: "pass" | "fail" | "unverifiable";
  rule: string;
  evidence: string;
  detail: string;
}

export interface Signal {
  name: string;
  points: number;
  evidence: string;
}

export interface Explanation {
  gates?: Gate[];
  signals?: Signal[];
}

export interface ResultItem {
  id: number;
  identity: string;
  title: string;
  company: string;
  location_raw: string;
  workplace: string;
  employment_type: string | null;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_period: string | null;
  posted_at: string | null;
  url: string;
  decision: "match" | "rejected";
  score: number;
  uncertain: boolean;
  is_new: boolean;
  run_id: number;
  search_id: number;
  match_id: number;
  user_status: string;
  changed_since: boolean;
  preview: string;
  explanation: Explanation;
  first_seen: string | null;
  last_seen: string | null;
  verification: string;
}

export interface ResultsPage {
  run: RunSummary | null;
  total: number;
  offset: number;
  items: ResultItem[];
}

export interface RunRequest {
  id: string;
  priority_uid: string | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  origin: string;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  rows_written: number;
  note: string;
  lease_expires_at?: string;
  attached?: boolean;
}

export interface Coverage {
  source: string;
  status: string;
  found: number;
  requests: number;
  duration_ms: number;
  note: string;
}

export interface StatusResponse {
  search: SearchSummary;
  latest_run: RunSummary | null;
  coverage: Coverage[];
  available_runs: RunSummary[];
  active_request: RunRequest | null;
  queued_request: RunRequest | null;
  latest_request: RunRequest | null;
  stale: boolean;
}

export interface Section {
  heading: string | null;
  lines: string[];
}

export interface SourceRef {
  source: string;
  url: string;
  authority: number;
}

export interface RuleSuggestion {
  kind: "employer" | "title" | "duty" | "skill" | "anywhere" | "seniority" | "hide";
  value: string;
  label: string;
}

export interface JobDetail {
  job: Record<string, unknown> & {
    id: number;
    title: string;
    company: string;
    url: string;
    location_raw: string;
    workplace: string;
    employment_type: string | null;
    salary_min: number | null;
    salary_max: number | null;
    salary_currency: string | null;
    salary_period: string | null;
    posted_at: string | null;
    first_seen: string | null;
    last_seen: string | null;
    verification: string;
  };
  sections: Section[];
  sources: SourceRef[];
  suggested_rules: RuleSuggestion[];
  explanation: Explanation | null;
  verdict: (Record<string, unknown> & {
    decision: "match" | "rejected";
    score: number;
    uncertain: number;
    is_new: number;
    title: string;
    company: string;
    location_raw: string;
    workplace: string;
    employment_type: string | null;
    salary_min: number | null;
    salary_max: number | null;
    salary_currency: string | null;
    salary_period: string | null;
    posted_at: string | null;
    url: string;
  }) | null;
  run: RunSummary | null;
  changed_since: boolean;
  status: string;
  note: string;
  search?: SearchSummary;
}

export interface DismissedItem {
  id: number;
  job_id: number;
  search_id: number;
  reason: string;
  created_at: string;
  title: string;
  company: string;
  rules: RuleSuggestion[];
}

export interface RegistryResponse {
  counts: Record<string, number>;
  boards: Array<Record<string, unknown> & {
    id: number;
    company: string;
    ats: string;
    token: string;
    us_signal: number;
    consecutive_failures: number;
    last_success: string | null;
    last_failure_kind: string;
  }>;
}

export interface SourcesResponse {
  sources: Array<{ name: string; kind: string; priority: string; note: string }>;
  excluded: Array<{ name: string; reason: string }>;
}
