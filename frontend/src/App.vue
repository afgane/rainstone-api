<script setup lang="ts">
import {
  BarChart3, BriefcaseBusiness, Building2, CircleDollarSign, Database,
  Download, RefreshCw, Search, Users, Wrench, Workflow, X,
} from "@lucide/vue";
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import {
  downloadExport, get, loadReport, queryString, type DailyItem, type Freshness,
  type GroupItem, type Invocation, type Job, type Me, type ReportState, type Summary, type View,
} from "./api";
import CostAmount from "./components/CostAmount.vue";
import JobsTable from "./components/JobsTable.vue";

const validViews: View[] = ["overview", "jobs", "tools", "invocations", "daily", "users", "infrastructure"];
function stateFromUrl(): ReportState {
  const params = new URLSearchParams(location.search);
  const view = params.get("view") as View;
  return {
    view: validViews.includes(view) ? view : "overview",
    basis: params.get("basis") === "allocated" ? "allocated" : "additional",
    mode: params.get("mode") === "completed" ? "completed" : "accrued",
    fromTime: params.get("from") || defaultBoundary(-30),
    toTime: params.get("to") || defaultBoundary(1),
    timezone: params.get("timezone") || "UTC",
    search: params.get("search") || "", owner: params.get("owner") || "",
    toolId: params.get("tool_id") || "", toolVersion: params.get("tool_version") || "",
    invocationId: params.get("invocation_id") || "", workflowId: params.get("workflow_id") || "",
    state: params.get("state") || "", runner: params.get("runner") || "",
    destination: params.get("destination") || "", capacity: params.get("capacity") || "",
    quality: params.get("quality") || "", minCost: params.get("min_cost") || "",
    maxCost: params.get("max_cost") || "", sort: params.get("sort") || "created_at",
    direction: params.get("direction") === "asc" ? "asc" : "desc",
    offset: Number(params.get("offset")) || 0, revision: params.get("revision") || undefined,
  };
}

function defaultBoundary(dayOffset: number): string {
  const value = new Date();
  value.setUTCDate(value.getUTCDate() + dayOffset);
  return value.toISOString().slice(0, 10);
}

const state = reactive<ReportState>(stateFromUrl());
const summary = ref<Summary | null>(null);
const jobs = ref<{ items: Job[]; total: number; limit: number }>({ items: [], total: 0, limit: 50 });
const freshness = ref<Freshness | null>(null);
const me = ref<Me | null>(null);
const viewData = ref<unknown>(null);
const loading = ref(true);
const error = ref("");
const detail = ref<Record<string, unknown> | null>(null);
const detailLoading = ref(false);
const closeButton = ref<HTMLButtonElement | null>(null);
let detailOpener: HTMLElement | null = null;
let controller: AbortController | null = null;
let timer = 0;
let firstLoad = true;

const nav = computed(() => [
  { id: "overview" as View, label: "Overview", icon: BarChart3 },
  { id: "jobs" as View, label: "Jobs", icon: BriefcaseBusiness },
  { id: "tools" as View, label: "Tools", icon: Wrench },
  { id: "invocations" as View, label: "Workflows", icon: Workflow },
  { id: "daily" as View, label: "Daily", icon: BarChart3 },
  ...(me.value?.capabilities.users ? [{ id: "users" as View, label: "Users", icon: Users }] : []),
  ...(me.value?.capabilities.infrastructure ? [{ id: "infrastructure" as View, label: "Infrastructure", icon: Building2 }] : []),
]);
const basisTitle = computed(() => state.basis === "additional" ? "Additional compute spend" : "Allocated resource cost");
const overviewParts = computed(() => Array.isArray(viewData.value) ? viewData.value as Array<{ items?: unknown[] }> : []);
const groups = computed(() => state.view === "overview"
  ? (overviewParts.value[1]?.items || []) as GroupItem[]
  : ((viewData.value as { items?: GroupItem[] } | null)?.items || []));
const invocations = computed(() => state.view === "overview"
  ? (overviewParts.value[2]?.items || []) as Invocation[]
  : ((viewData.value as { items?: Invocation[] } | null)?.items || []));
const days = computed(() => state.view === "overview"
  ? (overviewParts.value[0]?.items || []) as DailyItem[]
  : ((viewData.value as { items?: DailyItem[] } | null)?.items || []));
const maxDay = computed(() => Math.max(0, ...days.value.map(day => Number(day.amount))));
const page = computed(() => Math.floor(state.offset / 50) + 1);

function updateUrl(push = false) {
  const query = new URLSearchParams(queryString(state));
  query.set("view", state.view);
  const current = new URLSearchParams(location.search);
  if (current.get("detail_kind") && current.get("detail_id")) {
    query.set("detail_kind", current.get("detail_kind")!);
    query.set("detail_id", current.get("detail_id")!);
  }
  const url = `${location.pathname}?${query}`;
  history[push ? "pushState" : "replaceState"]({}, "", url);
}

async function refresh(push = false) {
  controller?.abort();
  controller = new AbortController();
  loading.value = true; error.value = "";
  updateUrl(push && !firstLoad);
  try {
    const result = await loadReport(state, controller.signal);
    summary.value = result.summary; jobs.value = result.jobs;
    freshness.value = result.freshness; me.value = result.me; viewData.value = result.view;
    if (!state.revision && result.summary.revision_id) {
      state.revision = result.summary.revision_id;
      updateUrl(false);
    }
    firstLoad = false;
  } catch (reason) {
    if ((reason as Error).name !== "AbortError") error.value = reason instanceof Error ? reason.message : "Unable to load reporting data";
  } finally {
    loading.value = false;
  }
}

function changeView(view: View) {
  state.view = view; state.offset = 0; void refresh(true);
}
function changeFilters() {
  state.offset = 0;
  updateUrl(false);
  window.clearTimeout(timer);
  timer = window.setTimeout(() => void refresh(true), 300);
}
function resetFilters() {
  Object.assign(state, {
    search: "", owner: "", toolId: "", toolVersion: "", invocationId: "", workflowId: "",
    state: "", runner: "", destination: "", capacity: "", quality: "", minCost: "",
    maxCost: "", fromTime: "", toTime: "", offset: 0,
  });
  void refresh(true);
}
function sort(field: string) {
  if (state.sort === field) state.direction = state.direction === "asc" ? "desc" : "asc";
  else { state.sort = field; state.direction = "asc"; }
  state.offset = 0; void refresh(true);
}
function sortLabel(field: string) {
  return state.sort === field ? ` (${state.direction === "asc" ? "ascending" : "descending"})` : "";
}
function detailAmount(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
function detailList(key: string): Array<Record<string, unknown>> {
  const value = detail.value?.[key];
  return Array.isArray(value) ? value as Array<Record<string, unknown>> : [];
}
async function showDetail(kind: "jobs" | "invocations", id: string, updateHistory = true) {
  detailOpener = document.activeElement as HTMLElement | null;
  if (updateHistory) {
    const params = new URLSearchParams(location.search);
    params.set("detail_kind", kind); params.set("detail_id", id);
    history.pushState({}, "", `${location.pathname}?${params}`);
  }
  detailLoading.value = true;
  try {
    detail.value = await get<Record<string, unknown>>(`/${kind}/${id}?${queryString(state)}`);
    await nextTick(); closeButton.value?.focus();
  }
  catch (reason) { error.value = reason instanceof Error ? reason.message : "Unable to load detail"; }
  finally { detailLoading.value = false; }
}
function closeDetail(updateHistory = true) {
  detail.value = null;
  if (updateHistory) {
    const params = new URLSearchParams(location.search);
    params.delete("detail_kind"); params.delete("detail_id");
    history.pushState({}, "", `${location.pathname}?${params}`);
  }
  detailOpener?.focus();
}
function refreshLatest() { state.revision = undefined; void refresh(true); }
async function download() {
  try { await downloadExport(state); }
  catch (reason) { error.value = reason instanceof Error ? reason.message : "Unable to export report"; }
}
function onPopState() {
  const params = new URLSearchParams(location.search);
  const kind = params.get("detail_kind"); const id = params.get("detail_id");
  Object.assign(state, stateFromUrl()); void refresh(false);
  if ((kind === "jobs" || kind === "invocations") && id) void showDetail(kind, id, false);
  else closeDetail(false);
}
function onKey(event: KeyboardEvent) { if (event.key === "Escape") closeDetail(); }
function selectDay(day: string) {
  const next = new Date(`${day}T12:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  state.fromTime = day;
  state.toTime = next.toISOString().slice(0, 10);
  changeView("daily");
}

watch(() => state.basis, () => { state.offset = 0; void refresh(true); });
onMounted(() => {
  window.addEventListener("popstate", onPopState); window.addEventListener("keydown", onKey); void refresh();
  const params = new URLSearchParams(location.search);
  const kind = params.get("detail_kind"); const id = params.get("detail_id");
  if ((kind === "jobs" || kind === "invocations") && id) void showDetail(kind, id, false);
});
onBeforeUnmount(() => { controller?.abort(); window.removeEventListener("popstate", onPopState); window.removeEventListener("keydown", onKey); });
</script>

<template>
  <header class="masthead">
    <div class="masthead-inner">
      <div class="brand"><CircleDollarSign :size="28" aria-hidden="true" /><span>Rainstone</span></div>
      <span v-if="summary?.demo" class="demo-badge">Demo data · synthetic scenarios included</span>
    </div>
  </header>
  <div class="shell">
    <nav class="side-nav" aria-label="Reporting views">
      <button v-for="item in nav" :key="item.id" :aria-current="state.view === item.id ? 'page' : undefined" @click="changeView(item.id)">
        <component :is="item.icon" :size="18" aria-hidden="true" />{{ item.label }}
      </button>
    </nav>
    <main id="main" class="page">
      <section class="page-heading">
        <div><p class="eyebrow">Galaxy compute reporting</p><h1>{{ nav.find(item => item.id === state.view)?.label }}</h1>
          <p>Accrued compute cost from observed resource intervals. Amounts are estimates in USD.</p></div>
        <label class="basis-control"><span>Cost basis</span><select v-model="state.basis">
          <option value="additional">Additional compute spend</option><option value="allocated">Allocated resource cost</option>
        </select></label>
      </section>

      <section class="filters" aria-label="Report filters">
        <label class="search"><Search :size="18" aria-hidden="true" /><span class="sr-only">Search report</span>
          <input v-model="state.search" placeholder="Search job, tool, owner, or workflow" @input="changeFilters"></label>
        <label><span>Accounting view</span><select v-model="state.mode" @change="changeFilters"><option value="accrued">Cost accrued in range</option><option value="completed">Jobs completed in range</option></select></label>
        <label><span>From ({{ state.timezone }})</span><input v-model="state.fromTime" placeholder="YYYY-MM-DD or ISO timestamp" @change="changeFilters"></label>
        <label><span>To, exclusive ({{ state.timezone }})</span><input v-model="state.toTime" placeholder="YYYY-MM-DD or ISO timestamp" @change="changeFilters"></label>
        <label><span>Tool ID</span><input v-model="state.toolId" @change="changeFilters"></label>
        <label><span>Tool version</span><input v-model="state.toolVersion" @change="changeFilters"></label>
        <label><span>Workflow ID</span><input v-model="state.workflowId" @change="changeFilters"></label>
        <label><span>Invocation ID</span><input v-model="state.invocationId" @change="changeFilters"></label>
        <label><span>State</span><select v-model="state.state" @change="changeFilters"><option value="">All states</option><option>ok</option><option>error</option><option>running</option></select></label>
        <label><span>Runner</span><select v-model="state.runner" @change="changeFilters"><option value="">All runners</option><option>kubernetes</option><option>gcp_batch</option><option>unknown</option></select></label>
        <label><span>Destination</span><input v-model="state.destination" @change="changeFilters"></label>
        <label><span>Capacity</span><select v-model="state.capacity" @change="changeFilters"><option value="">All capacity</option><option>existing</option><option>dedicated</option><option>elastic_shared</option><option>unknown</option></select></label>
        <label v-if="me?.is_admin"><span>Owner ID</span><input v-model="state.owner" @change="changeFilters"></label>
        <label><span>Minimum cost</span><input v-model="state.minCost" type="number" min="0" step="any" @change="changeFilters"></label>
        <label><span>Maximum cost</span><input v-model="state.maxCost" type="number" min="0" step="any" @change="changeFilters"></label>
        <label><span>Quality</span><select v-model="state.quality" @change="changeFilters"><option value="">All quality</option><option value="known_zero">Known zero</option><option>approximate</option><option>partial</option><option>unpriced</option><option value="in_progress">In progress</option></select></label>
        <button class="secondary" @click="resetFilters">Reset</button>
        <button class="secondary" @click="download"><Download :size="16" aria-hidden="true" /> Export CSV</button>
      </section>

      <div v-if="error" class="error" role="alert"><strong>Reporting data unavailable.</strong> {{ error }}</div>
      <div v-if="loading" class="loading" aria-live="polite"><RefreshCw class="spin" :size="18" /> Loading the selected report…</div>

      <template v-if="summary && !loading">
        <section class="stats" aria-label="Cost overview">
          <article class="stat-card featured"><span>{{ basisTitle }}</span><CostAmount :amount="summary.amount" currency="USD" />
            <small v-if="summary.job_count">{{ summary.priced_job_count }} with known amounts · {{ summary.unpriced_job_count }} incomplete</small><small v-else>No matching work</small></article>
          <article class="stat-card"><span>Jobs in result</span><strong>{{ summary.job_count }}</strong><small>{{ summary.known_zero_job_count }} known zero · counts can overlap incomplete coverage</small></article>
          <article class="stat-card"><span>Failed / retried spend</span><strong><CostAmount :amount="summary.failed_spend" /> / <CostAmount :amount="summary.retried_spend" /></strong><small>Retries remain part of incurred spend</small></article>
          <article v-if="summary.can_view_infrastructure" class="stat-card"><span>Baseline infrastructure, separate scope</span><CostAmount :amount="summary.baseline_infrastructure_amount" /><small>Whole-VM observed window; never added to job allocations</small></article>
        </section>
        <p class="snapshot">Snapshot {{ summary.revision_id?.slice(0, 8) }} · as of {{ summary.as_of ? new Date(summary.as_of).toLocaleString() : "unavailable" }} · {{ summary.observation_window.semantics }} · {{ summary.observation_window.timezone }} <button class="secondary" @click="refreshLatest">Refresh snapshot</button></p>

        <template v-if="state.view === 'overview'">
          <section class="panel"><div class="panel-heading"><div><h2>{{ state.mode === 'completed' ? 'Cost of jobs completed per day' : 'Daily accrued cost' }}</h2><p>Each bar selects that day in {{ state.timezone }}.</p></div></div>
            <div v-if="days.length" class="trend"><button v-for="day in days" :key="day.date" :aria-label="`Select ${day.date}: $${day.amount}`" :title="`${day.date}: $${day.amount}`" :style="{ height: `${Math.max(8, Number(day.amount) / (maxDay || 1) * 120)}px` }" @click="selectDay(day.date)"></button></div>
            <div class="table-wrap"><table><caption class="sr-only">Daily cost chart data</caption><thead><tr><th>Date</th><th>Cost</th><th>Jobs</th><th>Status</th></tr></thead><tbody>
              <tr v-for="day in days" :key="day.date"><td>{{ day.date }}</td><td><CostAmount :amount="day.amount" /></td><td>{{ day.job_count }}</td><td>{{ day.provisional ? "Provisional" : "Observed" }}</td></tr>
            </tbody></table></div></section>
          <JobsTable :items="jobs.items.slice(0, 8)" :sort-label="sortLabel" @sort="sort" @detail="id => showDetail('jobs', id)" />
          <div class="overview-columns">
            <section class="panel">
              <div class="panel-heading"><div><h2>Leading tool versions</h2><p>Full IDs keep versions distinct.</p></div></div>
              <button v-for="item in groups.slice(0, 4)" :key="item.tool_id" class="rank-row" @click="state.search = item.tool_id || ''; changeView('tools')">
                <span><strong>{{ item.tool_id }}</strong><small>{{ item.job_count }} jobs</small></span><CostAmount :amount="item.amount" />
              </button>
            </section>
            <section class="panel">
              <div class="panel-heading"><div><h2>Leading workflows</h2><p>Root totals deduplicate executions.</p></div></div>
              <button v-for="item in invocations.slice(0, 4)" :key="item.id" class="rank-row" @click="showDetail('invocations', item.id)">
                <span><strong>{{ item.workflow_name }}</strong><small>{{ item.job_count }} jobs</small></span><CostAmount :amount="item.amount" />
              </button>
            </section>
          </div>
        </template>

        <JobsTable v-else-if="state.view === 'jobs'" :items="jobs.items" :sort-label="sortLabel" @sort="sort" @detail="id => showDetail('jobs', id)" />

        <section v-else-if="state.view === 'tools' || state.view === 'users'" class="panel">
          <div class="panel-heading"><div><h2>{{ state.view === "tools" ? "Tool versions" : "Galaxy accounts" }}</h2><p>Totals cover the full filtered result. Historical statistics use complete successful jobs.</p></div></div>
          <div v-if="!groups.length" class="empty">No groups match these filters.</div>
          <div v-else class="table-wrap"><table><thead><tr><th>{{ state.view === "tools" ? "Full tool ID and version" : "Account" }}</th><th>Jobs</th><th>Coverage</th><th>Cost</th><th v-if="state.view === 'tools'">Completed cohort</th></tr></thead><tbody>
            <tr v-for="item in groups" :key="item.tool_id || item.owner_id"><td><strong>{{ item.label || item.tool_id }}</strong><small v-if="item.tool_version">{{ item.tool_version }}</small></td><td>{{ item.job_count }}</td><td>{{ item.priced_count }} priced · {{ item.incomplete_count }} incomplete</td><td><CostAmount :amount="item.amount" /></td><td v-if="state.view === 'tools'"><span v-if="item.statistics?.sample_count">median <CostAmount :amount="item.statistics.median" /> · p95 <CostAmount :amount="item.statistics.p95" /> · n={{ item.statistics.sample_count }}</span><span v-else>Unavailable</span></td></tr>
          </tbody></table></div>
        </section>

        <section v-else-if="state.view === 'invocations'" class="panel">
          <div class="panel-heading"><div><h2><Workflow :size="20" /> Root workflow invocations</h2><p>Root subtotals deduplicate executions; child totals are not added again.</p></div></div>
          <div v-if="!invocations.length" class="empty">No workflows match these filters.</div>
          <div v-else class="invocation-grid"><button v-for="item in invocations" :key="item.id" class="invocation-card" @click="showDetail('invocations', item.id)"><strong>{{ item.workflow_name }}</strong><span>{{ item.workflow_version || "Unversioned" }} · {{ item.state }}</span><span>{{ item.job_count }} distinct executions · {{ item.reused_job_count }} reused outputs</span><CostAmount :amount="item.amount" /><small>{{ item.unpriced_job_count }} incomplete</small></button></div>
        </section>

        <section v-else-if="state.view === 'daily'" class="panel">
          <div class="panel-heading"><div><h2>{{ state.mode === 'completed' ? 'Cost of jobs completed per day' : 'Cost accrued per day' }}</h2><p>Day boundaries use {{ state.timezone }} and preserve the filtered subtotal.</p></div>
            <label><span>Timezone</span><select v-model="state.timezone" @change="refresh(true)"><option>UTC</option><option>America/New_York</option><option>Europe/London</option></select></label></div>
          <div class="table-wrap"><table><thead><tr><th>Date</th><th>Cost</th><th>Jobs</th><th>Runner breakdown</th><th>Coverage</th></tr></thead><tbody><tr v-for="day in days" :key="day.date"><td>{{ day.date }}</td><td><CostAmount :amount="day.amount" /></td><td>{{ day.job_count }}</td><td>{{ Object.entries(day.by_runner).map(([key, value]) => `${key}: $${value}`).join(" · ") }}</td><td>{{ day.incomplete_count }} incomplete · {{ day.provisional ? "Provisional" : "Observed" }}</td></tr></tbody></table></div>
        </section>

        <section v-else-if="state.view === 'infrastructure'" class="panel">
          <div class="panel-heading"><div><h2>Baseline infrastructure</h2><p>{{ (viewData as { scope?: string })?.scope }}</p></div></div>
          <div class="callout">{{ (viewData as { allocation_reason?: string })?.allocation_reason }}</div>
          <div class="table-wrap"><table><thead><tr><th>Resource</th><th>Shape / region</th><th>Observed window</th><th>Cost</th><th>Quality</th></tr></thead><tbody><tr v-for="item in ((viewData as {items?: Array<Record<string,string>>})?.items || [])" :key="item.id"><td>{{ item.resource_uid }}</td><td>{{ item.machine_type }} · {{ item.region }}</td><td>{{ new Date(item.observed_start).toLocaleString() }} – {{ new Date(item.observed_end).toLocaleString() }}</td><td><CostAmount :amount="item.amount" /></td><td>{{ item.quality }}</td></tr></tbody></table></div>
        </section>

        <nav v-if="state.view === 'jobs' && jobs.total > jobs.limit" class="pagination" aria-label="Jobs pagination">
          <button :disabled="state.offset === 0" @click="state.offset = Math.max(0, state.offset - jobs.limit); refresh(true)">Previous</button><span>Page {{ page }} · {{ jobs.total }} jobs</span><button :disabled="state.offset + jobs.limit >= jobs.total" @click="state.offset += jobs.limit; refresh(true)">Next</button>
        </nav>
        <footer><Database :size="16" /> Collector {{ freshness?.overall_status }} · <span v-for="source in freshness?.sources" :key="source.source">{{ source.source }}: {{ source.status }} </span></footer>
      </template>
    </main>
  </div>

  <div v-if="detail || detailLoading" class="dialog-backdrop" @click.self="closeDetail()">
    <section class="dialog" role="dialog" aria-modal="true" aria-labelledby="detail-title">
      <button ref="closeButton" class="icon-button" aria-label="Close details" @click="closeDetail()"><X /></button>
      <div v-if="detailLoading" class="loading"><RefreshCw class="spin" /> Loading details…</div>
      <template v-else-if="detail"><p class="eyebrow">Traceable report detail</p><h2 id="detail-title">{{ detail.workflow_name || `Job #${detail.source_id}` }}</h2>
        <p v-if="detail.full_job_amount !== undefined"><strong>Selected interval:</strong> <CostAmount :amount="detailAmount(detail.interval_amount)" /> · <strong>Full job:</strong> <CostAmount :amount="detailAmount(detail.full_job_amount)" /></p>
        <p>{{ detail.reason }}</p>
        <div v-if="detailList('attempts').length" class="detail-list"><article v-for="attempt in detailList('attempts')" :key="String(attempt.id)"><strong>Attempt {{ attempt.source_attempt_id }}</strong><span>{{ attempt.runner }} · {{ attempt.outcome }} · {{ attempt.quality }}</span><span>{{ attempt.machine_type || 'Unknown machine' }} · {{ attempt.capacity_relationship }}</span><CostAmount :amount="detailAmount(attempt.amount)" /><small>{{ attempt.reason }}</small></article></div>
        <div v-if="detailList('steps').length" class="detail-list"><article v-for="step in detailList('steps')" :key="String(step.step_key)"><strong>{{ step.step_key }}</strong><span>{{ step.relationship }}</span><button v-if="step.job" class="secondary" @click="showDetail('jobs', String((step.job as Record<string, unknown>).id))">Open job #{{ (step.job as Record<string, unknown>).source_id }}</button><small v-else>No authorized matching job</small></article></div>
        <div v-if="detailList('children').length" class="detail-list"><article v-for="child in detailList('children')" :key="String(child.id)"><strong>{{ child.workflow_name }}</strong><span>{{ child.job_count }} jobs</span><button class="secondary" @click="showDetail('invocations', String(child.id))">Open child workflow</button></article></div>
      </template>
    </section>
  </div>
</template>
