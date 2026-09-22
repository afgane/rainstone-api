<script setup lang="ts">
import { Download } from "@lucide/vue";
import type { Invocation } from "../api";
import { formatCost, needsCostData, pluralize, runStatusLabel } from "../vocabulary";

defineProps<{ runs: Invocation[]; periodLabel: string }>();
const emit = defineEmits<{ run: [id: string]; export: [] }>();

function started(run: Invocation): string {
  return new Date(run.started_at).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
</script>

<template>
  <section class="panel">
    <div class="panel-heading">
      <div>
        <h2>Runs in this period</h2>
        <p>Runs that used compute in {{ periodLabel }}. Each total covers the whole run.</p>
      </div>
      <button class="secondary" @click="emit('export')"><Download :size="16" aria-hidden="true" /> Export CSV</button>
    </div>
    <div v-if="!runs.length" class="empty">No workflow runs match this period.</div>
    <ul v-else class="run-list">
      <li v-for="run in runs" :key="run.id">
        <button class="run-card" @click="emit('run', run.id)">
          <span class="run-title">
            <strong>{{ run.workflow_name }}</strong>
            <small>{{ started(run) }}</small>
          </span>
          <span class="run-meta">
            <span class="status" :data-status="run.run_status">{{ runStatusLabel(run.run_status) }}</span>
            <small v-if="run.workflow_version">Version {{ run.workflow_version }}</small>
            <small>{{ pluralize(run.run_job_count, "tool run") }}</small>
          </span>
          <span class="run-cost">
            <strong>{{ formatCost(run.run_total) }}</strong>
            <small>Run total</small>
            <!-- The period share is only worth saying when it differs. -->
            <small v-if="run.amount !== run.run_total">
              {{ formatCost(run.amount) }} in the selected period
            </small>
            <small v-if="run.run_unpriced_job_count">{{ needsCostData(run.run_unpriced_job_count) }}</small>
            <small v-if="run.timing_unavailable">Timing unavailable for this run</small>
          </span>
        </button>
      </li>
    </ul>
  </section>
</template>
