export type Basis = "additional" | "allocated";
export type View = "overview" | "jobs" | "tools" | "invocations" | "daily" | "users" | "infrastructure";

export interface ReportState {
  view: View;
  basis: Basis;
  mode: "accrued" | "completed";
  fromDate: string;
  toDate: string;
  timezone: string;
  search: string;
  owner: string;
  state: string;
  runner: string;
  quality: string;
  sort: string;
  direction: "asc" | "desc";
  offset: number;
  revision?: string;
}

export interface Meta {
  basis: Basis;
  currency: "USD";
  revision_id: string | null;
  as_of: string | null;
  priced_subtotal: string | null;
  observation_window: { from: string | null; to: string | null; timezone: string; semantics: string; mode: string };
  coverage: { jobs: number; priced: number; incomplete: number; known_zero: number; temporally_unattributed: number };
}

export interface Summary extends Meta {
  amount: string | null;
  job_count: number;
  priced_job_count: number;
  unpriced_job_count: number;
  known_zero_job_count: number;
  failed_spend: string;
  retried_spend: string;
  baseline_infrastructure_amount: string | null;
  can_view_infrastructure: boolean;
  demo: boolean;
}

export interface Job {
  id: string; source_id: string; tool_id: string; tool_version: string | null;
  owner: string; owner_id: string; state: string; runner: string | null;
  destination: string | null; created_at: string; amount: string | null; currency: string;
  quality: string; reason: string; attempt_cost_lines: number;
}

export interface Invocation {
  id: string; source_id: string; workflow_id: string; workflow_name: string;
  workflow_version: string | null; parent_id: string | null; state: string;
  job_count: number; amount: string | null; currency: string;
  unpriced_job_count: number; reused_job_count: number;
}

export interface GroupItem {
  tool_id?: string; tool_version?: string; owner_id?: string; label?: string;
  job_count: number; amount: string | null; priced_count: number; incomplete_count: number;
  statistics?: { sample_count: number; excluded_count: number; mean: string | null; median: string | null; p95: string | null };
}

export interface DailyItem {
  date: string; amount: string; currency: string; job_count: number; provisional: boolean;
  by_runner: Record<string, string>; by_owner: Record<string, string>; by_tool: Record<string, string>;
}

export interface Freshness {
  overall_status: string;
  sources: Array<{ source: string; status: string; last_success_at: string; error: string | null }>;
}

export interface Me {
  source_id: string; label: string; is_admin: boolean;
  capabilities: { infrastructure: boolean; users: boolean };
}

const identityHeaders = {
  "X-Rainstone-Tenant": "anvil-demo", "X-Rainstone-User": "admin", "X-Rainstone-Admin": "true",
};

function apiPath(path: string): string {
  const base = document.querySelector<HTMLMetaElement>('meta[name="rainstone-base"]')?.content || "/";
  return `${base.replace(/\/$/, "")}/api${path}`;
}

export function queryString(state: ReportState): string {
  const query = new URLSearchParams({ basis: state.basis, mode: state.mode, timezone: state.timezone });
  if (state.fromDate) query.set("from", `${state.fromDate}T00:00:00Z`);
  if (state.toDate) query.set("to", `${state.toDate}T00:00:00Z`);
  for (const key of ["search", "owner", "state", "runner", "quality"] as const) {
    if (state[key]) query.set(key, state[key]);
  }
  query.set("sort", state.sort);
  query.set("direction", state.direction);
  query.set("offset", String(state.offset));
  if (state.revision) query.set("revision", state.revision);
  return query.toString();
}

export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(apiPath(path), { headers: identityHeaders, signal });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export async function loadReport(state: ReportState, signal?: AbortSignal) {
  const initialQuery = queryString(state);
  const summary = await get<Summary>(`/summary?${initialQuery}`, signal);
  const snapshotState = { ...state, revision: summary.revision_id || state.revision };
  const query = queryString(snapshotState);
  const common = [
    get<{ items: Job[]; total: number; limit: number; offset: number; meta: Meta }>(`/jobs?${query}`, signal),
    get<Freshness>("/freshness", signal),
    get<Me>("/me", signal),
  ] as const;
  const viewRequest = state.view === "overview"
    ? Promise.all([
        get<{ items: DailyItem[]; meta: Meta }>(`/daily?${query}`, signal),
        get<{ items: GroupItem[]; meta: Meta }>(`/tools?${query}`, signal),
        get<{ items: Invocation[]; meta: Meta }>(`/invocations?${query}`, signal),
      ])
    : state.view === "tools" ? get<{ items: GroupItem[]; meta: Meta }>(`/tools?${query}`, signal)
    : state.view === "invocations" ? get<{ items: Invocation[]; meta: Meta }>(`/invocations?${query}`, signal)
    : state.view === "daily" ? get<{ items: DailyItem[]; meta: Meta }>(`/daily?${query}`, signal)
    : state.view === "users" ? get<{ items: GroupItem[]; meta: Meta }>(`/users?${query}`, signal)
    : state.view === "infrastructure" ? get<{ items: Array<Record<string, string>>; amount: string | null; scope: string; allocation_reason: string }>(`/infrastructure?${query}`, signal)
    : Promise.resolve(null);
  const [jobs, freshness, me, view] = await Promise.all([...common, viewRequest]);
  return { summary, jobs, freshness, me, view };
}

export async function downloadExport(state: ReportState): Promise<void> {
  const response = await fetch(apiPath(`/export/jobs.csv?${queryString({ ...state, offset: 0 })}`), {
    headers: identityHeaders,
  });
  if (!response.ok) throw new Error(`Export failed: ${response.status}`);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url; link.download = "rainstone-jobs.csv"; link.click();
  URL.revokeObjectURL(url);
}
