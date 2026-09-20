export type Basis = "additional" | "allocated";

export interface Summary {
  basis: Basis;
  currency: string;
  amount: string;
  job_count: number;
  priced_job_count: number;
  unpriced_job_count: number;
  known_zero_job_count: number;
  baseline_infrastructure_amount: string;
}

export interface Job {
  id: string;
  source_id: string;
  tool_id: string;
  tool_version: string;
  owner: string;
  state: string;
  runner: string;
  amount: string | null;
  currency: string;
  quality: string;
  reason: string;
}

export interface Invocation {
  id: string;
  source_id: string;
  workflow_name: string;
  parent_id: string | null;
  job_count: number;
  amount: string;
  currency: string;
  unpriced_job_count: number;
}

const identityHeaders = { "X-Rainstone-Tenant": "anvil-demo", "X-Rainstone-User": "admin", "X-Rainstone-Admin": "true" };

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: identityHeaders });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json() as Promise<T>;
}

export async function loadDashboard(basis: Basis, search = "") {
  const query = new URLSearchParams({ basis });
  if (search) query.set("search", search);
  return Promise.all([
    get<Summary>(`/api/summary?basis=${basis}`),
    get<{ items: Job[]; total: number }>(`/api/jobs?${query}`),
    get<Invocation[]>(`/api/invocations?basis=${basis}`),
    get<Array<{ source: string; status: string; last_success_at: string }>>("/api/freshness"),
  ]);
}
