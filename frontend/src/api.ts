import { exclusiveEnd, resolvePeriod, type Period, type PeriodId } from "./periods";

export type Basis = "additional" | "allocated";
/** Views are named for the questions they answer, not for their endpoints. */
export type View =
  | "overview" | "runs" | "tool-runs" | "tools" | "daily" | "users" | "server" | "status";

export interface ReportState {
  view: View;
  period: PeriodId;
  basis: Basis;
  mode: "accrued" | "completed";
  fromTime: string;
  toTime: string;
  timezone: string;
  search: string;
  owner: string;
  toolId: string;
  toolVersion: string;
  invocationId: string;
  workflowId: string;
  state: string;
  runner: string;
  destination: string;
  capacity: string;
  quality: string;
  minCost: string;
  maxCost: string;
  sort: string;
  direction: "asc" | "desc";
  offset: number;
  revision?: string;
}

export interface Meta {
  basis: Basis;
  currency: "USD";
  calculation_version: string | null;
  revision_id: string | null;
  as_of: string | null;
  priced_subtotal: string | null;
  observation_window: { from: string | null; to: string | null; timezone: string; semantics: string; mode: string };
  coverage: { jobs: number; priced: number; incomplete: number; known_zero: number; temporally_unattributed: number };
  /** Jobs a dated report leaves out because their cost has no usable timing. */
  undated: { job_count: number; amount: string | null; incomplete: number } | null;
}

export interface Summary extends Meta {
  amount: string | null;
  job_count: number;
  priced_job_count: number;
  unpriced_job_count: number;
  known_zero_job_count: number;
  failed_spend: string;
  failed_job_count: number;
  failed_incomplete_job_count: number;
  /** The whole cost of jobs that had a repeat attempt. */
  repeated_job_spend: string;
  repeated_job_count: number;
  /** Only what the repeat attempts themselves used. */
  repeat_attempt_spend: string;
  repeat_attempt_shared_spend: string;
  repeat_attempt_spend_complete: boolean;
  baseline_infrastructure_amount: string | null;
  /** When the server was actually observed, not the window that was asked for. */
  baseline_infrastructure_observed: { from: string; to: string } | null;
  can_view_infrastructure: boolean;
  demo: boolean;
  demo_period: { from: string; to: string } | null;
  imported_snapshot: {
    captured_at: string | null;
    source_cutoffs: Record<string, string> | null;
    snapshot_digest: string | null;
    label: string | null;
  } | null;
}

export interface Job {
  id: string; source_id: string; tool_id: string; tool_name: string;
  tool_version: string | null;
  owner: string; owner_id: string; state: string; runner: string | null;
  destination: string | null; created_at: string; amount: string | null; currency: string;
  quality: string; reason: string; cost_lines: number; attempt_count: number;
  repeat_attempt_count: number; attempt_evidence: "provider" | "galaxy_record" | "none";
  capacities: string[]; temporally_unattributed: boolean;
}

export interface JobList {
  items: Job[]; total: number; limit: number; offset: number;
  /** Outside the period's totals; shown beside them, never counted in them. */
  undated_items: Job[];
  meta: Meta;
}

export interface Infrastructure {
  items: Array<Record<string, string>>; amount: string | null; scope: string;
  allocation_reason: string; observation_window: Record<string, string>;
  observed_coverage: { from: string; to: string } | null;
}

export interface Invocation {
  id: string; source_id: string; workflow_id: string; workflow_name: string;
  workflow_version: string | null; parent_id: string | null; state: string;
  run_status: string; started_at: string;
  job_count: number; run_job_count: number;
  /** Cost accrued inside the selected period. */
  amount: string | null;
  /** The whole run, whatever period is selected. */
  run_total: string | null;
  run_total_complete: boolean;
  currency: string;
  unpriced_job_count: number; run_unpriced_job_count: number; reused_job_count: number;
  timing_unavailable: boolean;
}

export interface GroupItem {
  tool_id?: string; tool_name?: string; tool_version?: string; owner_id?: string; label?: string;
  job_count: number; amount: string | null; priced_count: number; incomplete_count: number;
  statistics?: { sample_count: number; excluded_count: number; mean: string | null; median: string | null; p95: string | null };
}

export interface DailyItem {
  date: string; amount: string; currency: string; job_count: number; provisional: boolean;
  incomplete_count: number;
  by_runner: Record<string, string>; by_owner: Record<string, string>; by_tool: Record<string, string>;
}

export interface Freshness {
  overall_status: string;
  sources: Array<{ source: string; status: string; last_success_at: string | null; error: string | null }>;
  observation_gaps: Array<{ source: string; kind: string; detected_at: string; recoverable: boolean; detail: string }>;
}

export interface Me {
  source_id: string; label: string; is_admin: boolean;
  auth_mode: string; attribution: string;
  capabilities: { infrastructure: boolean; users: boolean };
}

export interface StatusCheck {
  name: string; status: string; detail: string; facts: Record<string, unknown>;
}

export interface Status {
  generated_at: string; overall_status: string; auth_mode: string; tenant: string;
  checks: StatusCheck[]; failed_capabilities: string[];
  recorded_reports?: Array<{
    context: string; generated_at: string; age_seconds: number; stale: boolean;
    overall_status: string;
  }>;
}

function meta(name: string): string {
  return document.querySelector<HTMLMetaElement>(`meta[name="${name}"]`)?.content || "";
}

// Deployed modes resolve identity on the server; only the development fixture
// adapter accepts these headers, so production requests never carry them.
const identityHeaders: Record<string, string> = meta("rainstone-auth-mode") === "development"
  ? { "X-Rainstone-Tenant": "anvil-demo", "X-Rainstone-User": "admin", "X-Rainstone-Admin": "true" }
  : {};

export function authMode(): string {
  return meta("rainstone-auth-mode") || "development";
}

function apiPath(path: string): string {
  const base = meta("rainstone-base") || "/";
  return `${base.replace(/\/$/, "")}/api${path}`;
}

/** Filters a scientist never has to touch to get an answer. */
export const ADVANCED_FILTERS = [
  "state", "runner", "destination", "capacity", "quality", "toolId", "toolVersion",
  "workflowId", "invocationId", "minCost", "maxCost", "owner",
] as const;

export type AdvancedFilter = (typeof ADVANCED_FILTERS)[number];

export const FILTER_LABELS: Record<AdvancedFilter, string> = {
  state: "Status",
  runner: "Where it ran",
  destination: "Destination",
  capacity: "Capacity",
  quality: "Cost coverage",
  toolId: "Tool ID",
  toolVersion: "Tool version",
  workflowId: "Workflow ID",
  invocationId: "Run ID",
  minCost: "Minimum cost",
  maxCost: "Maximum cost",
  owner: "Galaxy account",
};

export function activeFilters(state: ReportState): Array<{ key: AdvancedFilter; value: string }> {
  return ADVANCED_FILTERS
    .map(key => ({ key, value: String(state[key] ?? "") }))
    .filter(entry => entry.value !== "");
}

export function periodOf(state: ReportState, now = new Date()): Period {
  return resolvePeriod(state.period, state.timezone, {
    fromDate: state.fromTime, toDate: state.toTime,
  }, now);
}

export function queryString(state: ReportState, now = new Date()): string {
  const query = new URLSearchParams({ basis: state.basis, mode: state.mode, timezone: state.timezone });
  const period = periodOf(state, now);
  query.set("from", offsetBoundary(period.fromDate, state.timezone));
  // Users choose an inclusive last day; the API boundary is exclusive.
  query.set("to", offsetBoundary(exclusiveEnd(period), state.timezone));
  const filters = {
    search: state.search, owner: state.owner, tool_id: state.toolId,
    tool_version: state.toolVersion, invocation_id: state.invocationId,
    workflow_id: state.workflowId, state: state.state, runner: state.runner,
    destination: state.destination, capacity: state.capacity, quality: state.quality,
    min_cost: state.minCost, max_cost: state.maxCost,
  };
  for (const [key, value] of Object.entries(filters)) {
    if (value) query.set(key, value);
  }
  query.set("sort", state.sort);
  query.set("direction", state.direction);
  query.set("offset", String(state.offset));
  if (state.revision) query.set("revision", state.revision);
  return query.toString();
}

function offsetBoundary(value: string, timezone: string): string {
  if (/Z$|[+-]\d\d:\d\d$/.test(value)) return value;
  const local = value.length === 10 ? `${value}T00:00:00` : value;
  const [date, clock] = local.split("T");
  const [year, month, day] = date.split("-").map(Number);
  const [hour = 0, minute = 0, second = 0] = (clock || "").split(":").map(Number);
  const desired = Date.UTC(year, month - 1, day, hour, minute, second);
  let instant = desired;
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
  for (let pass = 0; pass < 2; pass += 1) {
    const parts = Object.fromEntries(formatter.formatToParts(new Date(instant)).map(part => [part.type, part.value]));
    const rendered = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
    instant += desired - rendered;
  }
  return new Date(instant).toISOString();
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(apiPath(path), { headers: identityHeaders, signal });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(body.detail || `${response.status} ${response.statusText}`, response.status);
  }
  return response.json() as Promise<T>;
}

export async function loadReport(state: ReportState, signal?: AbortSignal) {
  try {
    return await loadPinnedReport(state, signal);
  } catch (reason) {
    // The snapshot this load pinned was superseded while the load was still
    // running, which an unattended collector does routinely. Every part of one
    // view must come from one revision, so the load is repeated against the
    // new one rather than shown as an error or mixed with the old one.
    if (reason instanceof ApiError && reason.status === 409) {
      return await loadPinnedReport(state, signal);
    }
    throw reason;
  }
}

/** One view, assembled from a single calculation revision. */
async function loadPinnedReport(state: ReportState, signal?: AbortSignal) {
  const initialQuery = queryString(state);
  const summary = await get<Summary>(`/summary?${initialQuery}`, signal);
  const snapshotState = { ...state, revision: summary.revision_id || state.revision };
  const query = queryString(snapshotState);
  const common = [
    get<JobList>(`/jobs?${query}`, signal),
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
    : state.view === "runs" ? get<{ items: Invocation[]; meta: Meta }>(`/invocations?${query}`, signal)
    : state.view === "daily" ? get<{ items: DailyItem[]; meta: Meta }>(`/daily?${query}`, signal)
    : state.view === "users" ? get<{ items: GroupItem[]; meta: Meta }>(`/users?${query}`, signal)
    : state.view === "server" ? get<Infrastructure>(`/infrastructure?${query}`, signal)
    : state.view === "status" ? get<Status>("/status", signal)
    : Promise.resolve(null);
  const [jobs, freshness, me, view] = await Promise.all([...common, viewRequest]);
  return { summary, jobs, freshness, me, view };
}

/** Report sources, as opposed to the price catalog, which is versioned separately. */
const COLLECTION_SOURCES = new Set(["galaxy_db", "kubernetes", "gcp_batch"]);

/**
 * The instant every report source had been collected through: the oldest of
 * their last successes. A missing success leaves the cutoff unknown. The
 * server already marks a source stale once its last success is too old.
 */
export function collectionCutoff(freshness: Freshness | null) {
  const sources = (freshness?.sources || []).filter(source => COLLECTION_SOURCES.has(source.source));
  if (!sources.length || sources.some(source => !source.last_success_at)) {
    return { cutoff: null, stale: true };
  }
  const cutoff = sources
    .map(source => source.last_success_at as string)
    .reduce((oldest, value) => (new Date(value) < new Date(oldest) ? value : oldest));
  return { cutoff, stale: sources.some(source => source.status !== "healthy") };
}

export function diagnosticsUrl(): string {
  return apiPath("/status/download");
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
