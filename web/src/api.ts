// The API client. One password, kept for the session; every call carries it.
// 401 sends the app back to the login screen; 409 and 410 are surfaced as typed errors
// so the screens can say "changed elsewhere" and "results expired" precisely.

import type {
  DismissedItem,
  JobDetail,
  RegistryResponse,
  ResultItem,
  ResultsPage,
  RunRequest,
  RunSummary,
  SearchDetail,
  SearchSpec,
  SearchSummary,
  SourcesResponse,
  StatusResponse,
} from "./types";

const PASSWORD_KEY = "jobagent.password";
export const UNAUTHORIZED_EVENT = "jobagent:unauthorized";

export function getPassword(): string {
  try {
    return sessionStorage.getItem(PASSWORD_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setPassword(value: string): void {
  try {
    if (value) sessionStorage.setItem(PASSWORD_KEY, value);
    else sessionStorage.removeItem(PASSWORD_KEY);
  } catch {
    // Storage may be unavailable; the app then asks again after a reload.
  }
}

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }

  get expiredLatestRun(): RunSummary | null {
    const d = this.detail as { expired?: boolean; latest_run?: RunSummary | null } | null;
    return this.status === 410 && d?.expired ? (d.latest_run ?? null) : null;
  }

  get conflictRevision(): number | null {
    const d = this.detail as { revision?: number } | null;
    return this.status === 409 && typeof d?.revision === "number" ? d.revision : null;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

interface Options {
  method?: string;
  body?: unknown;
  query?: Query;
}

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  }
  const headers: Record<string, string> = { Authorization: `Bearer ${getPassword()}` };
  let body: string | undefined;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const response = await fetch(url.toString(), { method: options.method ?? "GET", headers, body });
  if (response.status === 401) {
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
  }
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = (await response.json()).detail;
    } catch {
      detail = await response.text().catch(() => "");
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  const type = response.headers.get("content-type") ?? "";
  return (type.includes("application/json") ? response.json() : response.text()) as Promise<T>;
}

// ---------------------------------------------------------------- endpoints

export const searches = {
  list: () => api<SearchSummary[]>("/api/searches"),
  get: (uid: string) => api<SearchDetail>(`/api/searches/${uid}`),
  create: (spec: SearchSpec) => api<SearchSummary>("/api/searches", { method: "POST", body: spec }),
  update: (uid: string, spec: SearchSpec, revision: number) =>
    api<{ uid: string; name: string; revision: number }>(`/api/searches/${uid}`, {
      method: "PUT",
      body: { spec, revision },
    }),
  remove: (uid: string) => api<void>(`/api/searches/${uid}`, { method: "DELETE" }),
  importYaml: (yaml: string) => api<SearchSummary>("/api/searches/import", { method: "POST", body: { yaml } }),
  yaml: (uid: string) => api<string>(`/api/searches/${uid}/yaml`),
  results: (uid: string, query: Query) => api<ResultsPage>(`/api/searches/${uid}/results`, { query }),
  status: (uid: string) => api<StatusResponse>(`/api/searches/${uid}/status`),
  runs: (uid: string) => api<RunSummary[]>(`/api/searches/${uid}/runs`),
  dismissed: (uid: string) => api<DismissedItem[]>(`/api/searches/${uid}/dismissed`),
  exportUrl: (uid: string, run: number | null, format: string, decision: string) => {
    const url = new URL(`/api/searches/${uid}/export`, window.location.origin);
    url.searchParams.set("format", format);
    url.searchParams.set("decision", decision);
    if (run) url.searchParams.set("run", String(run));
    return url.toString();
  },
};

export const jobs = {
  get: (id: number, search?: string, run?: number | null) =>
    api<JobDetail>(`/api/jobs/${id}`, { query: { search, run } }),
  setStatus: (id: number, status: string, note = "") =>
    api<{ job_id: number; status: string }>(`/api/jobs/${id}/status`, { method: "POST", body: { status, note } }),
  dismiss: (id: number, body: { search_uid: string; revision: number; reason: string; rules: Array<{ kind: string; value: string }> }) =>
    api<{ job_id: number; reason: string; rules: string[]; revision: number; spec_changed: boolean }>(
      `/api/jobs/${id}/dismiss`,
      { method: "POST", body },
    ),
  saved: () => api<ResultItem[]>("/api/saved"),
};

export const runs = {
  request: (priority_uid: string | null) => api<RunRequest>("/api/runs", { method: "POST", body: { priority_uid } }),
  list: () => api<RunRequest[]>("/api/runs"),
  get: (id: string) => api<RunRequest>(`/api/runs/${id}`),
  cancel: (id: string) => api<RunRequest>(`/api/runs/${id}/cancel`, { method: "POST" }),
};

export const meta = {
  sources: () => api<SourcesResponse>("/api/sources"),
  registry: (platform?: string) => api<RegistryResponse>("/api/registry", { query: { platform, limit: 100 } }),
  addBoard: (url: string, name?: string) =>
    api<{ boards: Array<{ platform: string; token: string; company: string }> }>("/api/registry", {
      method: "POST",
      body: { url, name: name || null },
    }),
  health: () => api<{ ok: boolean; backend: string }>("/api/health"),
};

// Downloads go through fetch so the password header travels with them.
export async function download(url: string, filename: string): Promise<void> {
  const response = await fetch(url, { headers: { Authorization: `Bearer ${getPassword()}` } });
  if (!response.ok) throw new ApiError(response.status, await response.text());
  const blob = await response.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.click();
  URL.revokeObjectURL(link.href);
}
