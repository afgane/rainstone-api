<script setup lang="ts">
import { Download } from "@lucide/vue";
import type { Job, Meta } from "../api";
import {
  capacityLabel, formatCost, formatDateTime, jobStateLabel, qualityLabel, undatedSentence,
} from "../vocabulary";

defineProps<{
  jobs: Job[];
  undatedJobs: Job[];
  undated: Meta["undated"];
  total: number;
  periodLabel: string;
  timezone: string;
}>();
const emit = defineEmits<{
  detail: [id: string]; sort: [field: string]; export: []; "more-undated": [];
}>();
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
            <th><button class="sort-button" @click="emit('sort', 'created_at')">Submitted</button></th>
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
            <td>{{ formatDateTime(job.created_at, timezone) }}</td>
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
    <details v-if="undated?.job_count" class="inline-details">
      <summary>{{ undatedSentence(undated.job_count) }}</summary>
      <p>
        Their cost evidence has no time it can be placed at, so no period can claim them. They are
        listed here for inspection only and are not part of the totals or the export.
      </p>
      <ul class="step-list">
        <li v-for="job in undatedJobs" :key="job.id">
          <button class="link-button" @click="emit('detail', job.id)">{{ job.tool_name }}</button>
          <span>{{ jobStateLabel(job.state) }}</span>
          <small>Submitted {{ formatDateTime(job.created_at, timezone) }} · {{ qualityLabel(job.quality) }}</small>
        </li>
      </ul>
      <p v-if="undated.job_count > undatedJobs.length">
        Showing {{ undatedJobs.length }} of {{ undated.job_count }}.
        <button class="link-button" @click="emit('more-undated')">Show more</button>
      </p>
    </details>
  </section>
</template>
