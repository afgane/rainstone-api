<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { CircleDollarSign, Database, RefreshCw, Search, Workflow } from "@lucide/vue";
import { type Basis, type Invocation, type Job, loadDashboard, type Summary } from "./api";
import CostAmount from "./components/CostAmount.vue";

const basis = ref<Basis>((new URLSearchParams(location.search).get("basis") as Basis) || "additional");
const search = ref("");
const summary = ref<Summary | null>(null);
const jobs = ref<Job[]>([]);
const invocations = ref<Invocation[]>([]);
const freshness = ref("");
const error = ref("");
const loading = ref(true);

const basisTitle = computed(() => basis.value === "additional" ? "Additional compute spend" : "Allocated resource cost");

async function refresh() {
  loading.value = true;
  error.value = "";
  try {
    const [summaryData, jobsData, invocationData, freshnessData] = await loadDashboard(basis.value, search.value);
    summary.value = summaryData;
    jobs.value = jobsData.items;
    invocations.value = invocationData;
    freshness.value = freshnessData[0]?.last_success_at || "Unavailable";
    const url = new URL(location.href);
    url.searchParams.set("basis", basis.value);
    history.replaceState({}, "", url);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "Unable to load reporting data";
  } finally {
    loading.value = false;
  }
}

let searchTimer: number;
watch(basis, refresh);
watch(search, () => { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(refresh, 250); });
onMounted(refresh);
</script>

<template>
  <header class="masthead">
    <div class="masthead-inner">
      <div class="brand">
        <CircleDollarSign
          :size="28"
          aria-hidden="true"
        /><span>Rainstone</span>
      </div>
      <span class="demo-badge">Demo data</span>
    </div>
  </header>
  <main
    id="main"
    class="page"
  >
    <section class="page-heading">
      <div>
        <p class="eyebrow">
          Galaxy compute reporting
        </p>
        <h1>Understand what your work cost</h1>
        <p>Traceable compute estimates from observed execution resources and a versioned price snapshot.</p>
      </div>
      <label class="basis-control">
        <span>Cost basis</span>
        <select v-model="basis">
          <option value="additional">Additional compute spend</option>
          <option value="allocated">Allocated resource cost</option>
        </select>
      </label>
    </section>

    <div
      v-if="error"
      class="error"
      role="alert"
    >
      {{ error }}
    </div>
    <div
      v-if="loading"
      class="loading"
      aria-live="polite"
    >
      <RefreshCw
        class="spin"
        :size="18"
      /> Loading reporting data…
    </div>

    <template v-if="summary && !loading">
      <section
        class="stats"
        aria-label="Cost overview"
      >
        <article class="stat-card featured">
          <span>{{ basisTitle }}</span>
          <CostAmount
            :amount="summary.amount"
            :currency="summary.currency"
          />
          <small>{{ summary.priced_job_count }} priced · {{ summary.unpriced_job_count }} incomplete</small>
        </article>
        <article class="stat-card">
          <span>Jobs in view</span><strong>{{ summary.job_count }}</strong>
          <small>{{ summary.known_zero_job_count }} known zero under this basis</small>
        </article>
        <article class="stat-card">
          <span>Baseline VM, separate total</span>
          <CostAmount
            :amount="summary.baseline_infrastructure_amount"
            currency="USD"
          />
          <small>Six-minute observed window · never added to job allocations</small>
        </article>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <div><h2>Jobs</h2><p>Each row explains its cost quality in words.</p></div>
          <label class="search"><Search
            :size="18"
            aria-hidden="true"
          /><span class="sr-only">Search jobs</span><input
            v-model="search"
            placeholder="Search tool or job ID"
          ></label>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Job</th><th>Tool and version</th><th>Owner</th><th>Runner</th><th>Cost</th><th>Quality</th></tr></thead>
            <tbody>
              <tr
                v-for="job in jobs"
                :key="job.id"
              >
                <td>#{{ job.source_id }}</td>
                <td><strong>{{ job.tool_id.split('/').slice(-2, -1)[0] || job.tool_id }}</strong><small>{{ job.tool_version }}</small></td>
                <td>{{ job.owner }}</td><td>{{ job.runner }}</td>
                <td>
                  <CostAmount
                    :amount="job.amount"
                    :currency="job.currency"
                  />
                </td>
                <td>
                  <span
                    class="quality"
                    :data-quality="job.quality"
                  >{{ job.quality.replace('_', ' ') }}</span><small>{{ job.reason }}</small>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <div>
            <h2>
              <Workflow
                :size="20"
                aria-hidden="true"
              /> Workflow invocations
            </h2><p>Nested totals deduplicate executions within each invocation.</p>
          </div>
        </div>
        <div class="invocation-grid">
          <article
            v-for="item in invocations"
            :key="item.id"
            class="invocation-card"
          >
            <strong>{{ item.workflow_name }}</strong><span>{{ item.job_count }} unique jobs</span>
            <CostAmount
              :amount="item.amount"
              :currency="item.currency"
            />
            <small v-if="item.unpriced_job_count">{{ item.unpriced_job_count }} incomplete</small>
          </article>
        </div>
      </section>

      <footer>
        <Database
          :size="16"
          aria-hidden="true"
        /> Fixture collector healthy · last sync {{ freshness }}
      </footer>
    </template>
  </main>
</template>
