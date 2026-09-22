<script setup lang="ts">
import { Download } from "@lucide/vue";
import type { Job } from "../api";
import { capacityLabel, formatCost, jobStateLabel, qualityLabel } from "../vocabulary";

defineProps<{ jobs: Job[]; total: number; periodLabel: string }>();
const emit = defineEmits<{ detail: [id: string]; sort: [field: string]; export: [] }>();

function ran(job: Job): string {
  return new Date(job.created_at).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
</script>

<template>
  <section class="panel">
    <div class="panel-heading">
      <div>
        <h2>Tool runs in this period</h2>
        <p>{{ total }} tool runs with compute in {{ periodLabel }}. Open one for its cost explanation.</p>
      </div>
      <button class="secondary" @click="emit('export')"><Download :size="16" aria-hidden="true" /> Export CSV</button>
    </div>
    <div v-if="!jobs.length" class="empty">No tool runs match this period.</div>
    <div v-else class="table-wrap">
      <table class="jobs-table">
        <thead>
          <tr>
            <th><button class="sort-button" @click="emit('sort', 'tool_id')">Tool</button></th>
            <th><button class="sort-button" @click="emit('sort', 'created_at')">Started</button></th>
            <th><button class="sort-button" @click="emit('sort', 'state')">Status</button></th>
            <th><button class="sort-button" @click="emit('sort', 'amount')">Cost</button></th>
            <th>Where it ran</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="job in jobs" :key="job.id">
            <td>
              <button class="link-button" @click="emit('detail', job.id)">{{ job.tool_name }}</button>
              <small>{{ job.tool_version || "Unversioned" }}</small>
            </td>
            <td>{{ ran(job) }}</td>
            <td>{{ jobStateLabel(job.state) }}</td>
            <td>
              <strong>{{ formatCost(job.amount) }}</strong>
              <small>{{ qualityLabel(job.quality) }}</small>
            </td>
            <td>{{ capacityLabel(job.capacities) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
