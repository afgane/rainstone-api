<script setup lang="ts">
import { CircleDollarSign, Filter, RefreshCw } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import {
  ADVANCED_FILTERS, activeFilters, downloadExport, get, loadReport, periodOf, queryString,
  type AdvancedFilter, type DailyItem, type Freshness, type GroupItem, type Invocation, type Job,
  type Me, type ReportState, type Status, type Summary, type View,
} from "./api";
import DetailDialog from "./components/DetailDialog.vue";
import OverviewPanel from "./components/OverviewPanel.vue";
import ReportSidebar from "./components/ReportSidebar.vue";
import RunsPanel from "./components/RunsPanel.vue";
import ServerPanel from "./components/ServerPanel.vue";
import StatusPanel from "./components/StatusPanel.vue";
import ToolRunsPanel from "./components/ToolRunsPanel.vue";
import ToolsPanel from "./components/ToolsPanel.vue";
import { describePeriod, PERIOD_LABELS, type PeriodId } from "./periods";
import { formatCost, measureName } from "./vocabulary";

const VIEWS: View[] = ["overview", "runs", "tool-runs", "tools", "daily", "users", "server", "status"];
const TITLES: Record<View, string> = {
  overview: "Overview",
  runs: "Workflow runs",
  "tool-runs": "Tool runs",
  tools: "Tools",
  daily: "Daily cost",
  users: "Galaxy accounts",
  server: "Galaxy server",
  status: "Status",
};

function stateFromUrl(): ReportState {
  const params = new URLSearchParams(location.search);
  const view = params.get("view") as View;
  const period = (params.get("period") || "this-month") as PeriodId;
  return {
    view: VIEWS.includes(view) ? view : "overview",
    period,
    basis: params.get("basis") === "allocated" ? "allocated" : "additional",
    mode: "accrued",
    fromTime: params.get("from") || "",
    toTime: params.get("to") || "",
    timezone: params.get("timezone") || "UTC",
    search: params.get("search") || "", owner: params.get("owner") || "",
    toolId: params.get("tool_id") || "", toolVersion: params.get("tool_version") || "",
    invocationId: params.get("invocation_id") || "", workflowId: params.get("workflow_id") || "",
    state: params.get("state") || "", runner: params.get("runner") || "",
    destination: params.get("destination") || "", capacity: params.get("capacity") || "",
    quality: params.get("quality") || "", minCost: params.get("min_cost") || "",
    maxCost: params.get("max_cost") || "", sort: params.get("sort") || "created_at",
    direction: params.get("direction") === "asc" ? "asc" : "desc",
    offset: Number(params.get("offset")) || 0,
  };
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
const detailKind = ref<"runs" | "tool-runs" | null>(null);
const detailLoading = ref(false);
const advancedOpen = ref(false);
const drawerOpen = ref(false);
let detailOpener: HTMLElement | null = null;
let controller: AbortController | null = null;
let timer = 0;
let firstLoad = true;

const period = computed(() => periodOf(state));
const periodLabel = computed(() => (state.period === "custom"
  ? "the selected dates"
  : PERIOD_LABELS[state.period].toLowerCase()));
const filters = computed(() => activeFilters(state));
const overviewParts = computed(() => (Array.isArray(viewData.value)
  ? viewData.value as Array<{ items?: unknown[] }>
  : []));
const days = computed(() => (state.view === "overview"
  ? (overviewParts.value[0]?.items || []) as DailyItem[]
  : ((viewData.value as { items?: DailyItem[] } | null)?.items || [])));
const tools = computed(() => (state.view === "overview"
  ? (overviewParts.value[1]?.items || []) as GroupItem[]
  : ((viewData.value as { items?: GroupItem[] } | null)?.items || [])));
const runs = computed(() => (state.view === "overview"
  ? (overviewParts.value[2]?.items || []) as Invocation[]
  : ((viewData.value as { items?: Invocation[] } | null)?.items || [])));
const server = computed(() => (state.view === "server"
  ? viewData.value as Record<string, never> | null
  : null));
const status = computed(() => (state.view === "status" ? viewData.value as Status | null : null));
const serverWindow = computed(() => {
  const observed = summary.value?.observation_window;
  return observed?.from && observed?.to
    ? `${new Date(observed.from).toLocaleDateString()} – ${new Date(observed.to).toLocaleDateString()}`
    : null;
});
const page = computed(() => Math.floor(state.offset / 50) + 1);

function updateUrl(push = false) {
  const query = new URLSearchParams(queryString(state));
  query.set("view", state.view);
  query.set("period", state.period);
  // Period presets resolve their own dates; only custom dates travel in the URL.
  query.delete("from");
  query.delete("to");
  if (state.period === "custom") {
    query.set("from", state.fromTime);
    query.set("to", state.toTime);
  }
  const current = new URLSearchParams(location.search);
  if (current.get("detail_kind") && current.get("detail_id")) {
    query.set("detail_kind", current.get("detail_kind")!);
    query.set("detail_id", current.get("detail_id")!);
  }
  history[push ? "pushState" : "replaceState"]({}, "", `${location.pathname}?${query}`);
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
    firstLoad = false;
  } catch (reason) {
    if ((reason as Error).name !== "AbortError") {
      error.value = reason instanceof Error ? reason.message : "Unable to load reporting data";
    }
  } finally {
    loading.value = false;
  }
}

function changeView(view: View) {
  state.view = view; state.offset = 0; drawerOpen.value = false; void refresh(true);
}
function changePeriod(id: PeriodId) {
  state.period = id; state.offset = 0;
  if (id === "custom" && !state.fromTime) {
    state.fromTime = period.value.fromDate; state.toTime = period.value.toDate;
  }
  void refresh(true);
}
function setField(key: string, value: string) {
  (state as unknown as Record<string, unknown>)[key] = value;
  changeFilters();
}
function changeFilters() {
  state.offset = 0;
  updateUrl(false);
  window.clearTimeout(timer);
  timer = window.setTimeout(() => void refresh(true), 300);
}
function clearFilter(key: AdvancedFilter) {
  (state as unknown as Record<string, unknown>)[key] = "";
  void refresh(true);
}
function clearFilters() {
  for (const key of ADVANCED_FILTERS) (state as unknown as Record<string, unknown>)[key] = "";
  state.search = "";
  void refresh(true);
}
function sort(field: string) {
  if (state.sort === field) state.direction = state.direction === "asc" ? "desc" : "asc";
  else { state.sort = field; state.direction = "asc"; }
  state.offset = 0; void refresh(true);
}
function selectDay(date: string) {
  state.period = "custom"; state.fromTime = date; state.toTime = date;
  changeView("tool-runs");
}
function showDemoPeriod() {
  const window = summary.value?.demo_period;
  if (!window) return;
  state.period = "custom";
  state.fromTime = window.from.slice(0, 10);
  state.toTime = window.to.slice(0, 10);
  void refresh(true);
}
function selectTool(toolId: string) {
  state.toolId = toolId; changeView("tool-runs");
}

async function showDetail(kind: "runs" | "tool-runs", id: string, updateHistory = true) {
  detailOpener = document.activeElement as HTMLElement | null;
  detailKind.value = kind;
  if (updateHistory) {
    const params = new URLSearchParams(location.search);
    params.set("detail_kind", kind); params.set("detail_id", id);
    history.pushState({}, "", `${location.pathname}?${params}`);
  }
  detailLoading.value = true;
  const path = kind === "runs" ? "invocations" : "jobs";
  try {
    detail.value = await get<Record<string, unknown>>(`/${path}/${id}?${queryString(state)}`);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "Unable to load detail";
  } finally {
    detailLoading.value = false;
  }
}
function closeDetail(updateHistory = true) {
  detail.value = null; detailKind.value = null;
  if (updateHistory) {
    const params = new URLSearchParams(location.search);
    params.delete("detail_kind"); params.delete("detail_id");
    history.pushState({}, "", `${location.pathname}?${params}`);
  }
  detailOpener?.focus();
}
function refreshLatest() { void refresh(true); }
async function download() {
  try { await downloadExport(state); }
  catch (reason) { error.value = reason instanceof Error ? reason.message : "Unable to export report"; }
}
function onPopState() {
  const params = new URLSearchParams(location.search);
  const kind = params.get("detail_kind"); const id = params.get("detail_id");
  Object.assign(state, stateFromUrl()); void refresh(false);
  if ((kind === "runs" || kind === "tool-runs") && id) void showDetail(kind, id, false);
  else closeDetail(false);
}
function onKey(event: KeyboardEvent) {
  if (event.key !== "Escape") return;
  if (detail.value) closeDetail();
  else drawerOpen.value = false;
}

watch(() => state.basis, () => { state.offset = 0; void refresh(true); });
onMounted(() => {
  window.addEventListener("popstate", onPopState);
  window.addEventListener("keydown", onKey);
  void refresh();
  const params = new URLSearchParams(location.search);
  const kind = params.get("detail_kind"); const id = params.get("detail_id");
  if ((kind === "runs" || kind === "tool-runs") && id) void showDetail(kind, id, false);
});
onBeforeUnmount(() => {
  controller?.abort();
  window.removeEventListener("popstate", onPopState);
  window.removeEventListener("keydown", onKey);
});
</script>

<template>
  <header class="masthead">
    <div class="masthead-inner">
      <div class="brand"><CircleDollarSign :size="28" aria-hidden="true" /><span>Rainstone</span></div>
      <span v-if="summary?.demo" class="demo-badge">Demo data · synthetic scenarios included</span>
      <span v-else-if="me?.attribution" class="demo-badge">{{ me.attribution }}</span>
    </div>
  </header>

  <div class="shell">
    <button class="drawer-toggle" :aria-expanded="drawerOpen" @click="drawerOpen = !drawerOpen">
      <Filter :size="18" aria-hidden="true" /> Filters
      <span v-if="filters.length" class="count">{{ filters.length }}</span>
    </button>
    <div class="sidebar-wrap" :data-open="drawerOpen">
      <ReportSidebar
        :state="state"
        :can-view-server="Boolean(me?.capabilities.infrastructure)"
        :can-view-users="Boolean(me?.capabilities.users)"
        :advanced-open="advancedOpen"
        @view="changeView"
        @period="changePeriod"
        @set="setField"
        @toggle-advanced="advancedOpen = !advancedOpen"
        @clear-filter="clearFilter"
        @clear="clearFilters"
      />
    </div>

    <main id="main" class="page">
      <h1 class="page-title">{{ TITLES[state.view] }}</h1>
      <div v-if="error" class="error" role="alert">
        <strong>Reporting data unavailable.</strong> {{ error }}
      </div>
      <div v-if="loading" class="loading" aria-live="polite">
        <RefreshCw class="spin" :size="18" /> Loading…
      </div>

      <template v-if="summary && !loading">
        <OverviewPanel
          v-if="state.view === 'overview'"
          :state="state" :summary="summary" :days="days" :tools="tools" :runs="runs"
          :server-window="serverWindow"
          @view="changeView" @run="id => showDetail('runs', id)" @day="selectDay"
          @demo-period="showDemoPeriod"
        />

        <RunsPanel
          v-else-if="state.view === 'runs'"
          :runs="runs" :period-label="periodLabel"
          @run="id => showDetail('runs', id)" @export="download"
        />

        <ToolRunsPanel
          v-else-if="state.view === 'tool-runs'"
          :jobs="jobs.items" :total="jobs.total" :period-label="periodLabel"
          @detail="id => showDetail('tool-runs', id)" @sort="sort" @export="download"
        />

        <ToolsPanel
          v-else-if="state.view === 'tools'"
          :tools="tools" :period-label="periodLabel" @select="selectTool"
        />

        <ServerPanel v-else-if="state.view === 'server'" :server="server" />

        <StatusPanel v-else-if="state.view === 'status'" :status="status" :freshness="freshness" />

        <section v-else-if="state.view === 'daily'" class="panel">
          <div class="panel-heading">
            <div><h2>Daily cost</h2>
              <p>Cost accrued each day in {{ periodLabel }}, in {{ state.timezone }}.</p></div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Date</th><th>Cost</th><th>Runs</th><th>Coverage</th></tr></thead>
              <tbody>
                <tr v-for="day in days" :key="day.date">
                  <td>{{ day.date }}</td>
                  <td>{{ formatCost(day.amount) }}</td>
                  <td>{{ day.job_count }}</td>
                  <td>
                    {{ day.incomplete_count ? `${day.incomplete_count} still need cost data` : "Complete" }}
                    <span v-if="day.provisional">· still running</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section v-else-if="state.view === 'users'" class="panel">
          <div class="panel-heading">
            <div><h2>Galaxy accounts</h2><p>Cost by Galaxy account in {{ periodLabel }}.</p></div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Account</th><th>Runs</th><th>Cost</th></tr></thead>
              <tbody>
                <tr v-for="group in tools" :key="group.owner_id">
                  <td>{{ group.label }}</td><td>{{ group.job_count }}</td>
                  <td>{{ formatCost(group.amount) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <nav
          v-if="state.view === 'tool-runs' && jobs.total > jobs.limit" class="pagination"
          aria-label="Pages"
        >
          <button
            :disabled="state.offset === 0"
            @click="state.offset = Math.max(0, state.offset - jobs.limit); refresh(true)"
          >Previous</button>
          <span>Page {{ page }} · {{ jobs.total }} tool runs</span>
          <button
            :disabled="state.offset + jobs.limit >= jobs.total"
            @click="state.offset += jobs.limit; refresh(true)"
          >Next</button>
        </nav>

        <div v-if="state.view !== 'status'" class="snapshot">
          {{ measureName(state.basis) }} · {{ describePeriod(period, state.timezone) }} ·
          Updated {{ summary.as_of ? new Date(summary.as_of).toLocaleString() : "unavailable" }}
          <button class="link-button" @click="refreshLatest">Refresh</button>
          <details class="inline-details">
            <summary>Technical details</summary>
            <p class="mono">Snapshot {{ summary.revision_id }} · {{ summary.calculation_version }}</p>
            <p>
              {{ summary.observation_window.semantics }} interval in
              {{ summary.observation_window.timezone }}. Amounts are estimates in USD covering
              compute only; disks, network, discounts, credits and taxes are excluded.
            </p>
          </details>
        </div>
      </template>
    </main>
  </div>

  <DetailDialog
    :kind="detailKind" :detail="detail" :loading="detailLoading" :period-label="periodLabel"
    @close="closeDetail" @open="(kind, id) => showDetail(kind, id)"
  />
</template>
