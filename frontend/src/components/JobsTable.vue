<script setup lang="ts">
import type { Job } from "../api";
import CostAmount from "./CostAmount.vue";

// eslint-disable-next-line no-unused-vars
defineProps<{ items: Job[]; sortLabel: (field: string) => string }>();
defineEmits<{ sort: [field: string]; detail: [id: string] }>();
</script>

<template>
  <section class="panel">
    <div class="panel-heading"><div><h2>Jobs</h2><p>Open a row for attempts, provisioned shape, timing, policy, and price provenance.</p></div></div>
    <div v-if="items.length" class="table-wrap">
      <table class="jobs-table">
        <thead><tr>
          <th><button class="sort-button" :aria-label="`Sort by Job${sortLabel('source_id')}`" @click="$emit('sort', 'source_id')">Job{{ sortLabel("source_id") }}</button></th>
          <th><button class="sort-button" :aria-label="`Sort by Tool${sortLabel('tool_id')}`" @click="$emit('sort', 'tool_id')">Tool and version{{ sortLabel("tool_id") }}</button></th>
          <th><button class="sort-button" @click="$emit('sort', 'owner')">Owner{{ sortLabel("owner") }}</button></th>
          <th><button class="sort-button" @click="$emit('sort', 'runner')">Runner{{ sortLabel("runner") }}</button></th>
          <th><button class="sort-button" @click="$emit('sort', 'state')">State{{ sortLabel("state") }}</button></th>
          <th><button class="sort-button" @click="$emit('sort', 'amount')">Cost{{ sortLabel("amount") }}</button></th><th>Quality</th>
        </tr></thead>
        <tbody><tr v-for="job in items" :key="job.id">
          <td><button class="link-button" @click="$emit('detail', job.id)">#{{ job.source_id }}</button></td>
          <td><strong>{{ job.tool_id }}</strong><small>{{ job.tool_version || "Unversioned" }}</small></td>
          <td>{{ job.owner }}</td><td>{{ job.runner || "Unknown" }}</td><td>{{ job.state }}</td>
          <td><CostAmount :amount="job.amount" :currency="job.currency" /></td>
          <td><span class="quality" :data-quality="job.quality">{{ job.quality.replace("_", " ") }}</span><small>{{ job.reason }}</small></td>
        </tr></tbody>
      </table>
    </div>
    <div v-else class="empty">No jobs match these filters.</div>
  </section>
</template>
