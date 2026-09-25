<script setup lang="ts">
import type { Infrastructure } from "../api";
import { formatCost, formatDateTime, SERVER_EXPLANATION } from "../vocabulary";

const props = defineProps<{ server: Infrastructure | null; timezone: string }>();

function span(from: string, to: string): string {
  return `${formatDateTime(from, props.timezone)} – ${formatDateTime(to, props.timezone)}`;
}
</script>

<template>
  <section class="panel">
    <div class="panel-heading">
      <div><h2>Galaxy server cost</h2><p>{{ SERVER_EXPLANATION }}</p></div>
    </div>
    <p class="quiet-amount">
      {{ formatCost(server?.amount) }}
      <small>{{ server?.observed_coverage
        ? `Observed ${span(server.observed_coverage.from, server.observed_coverage.to)}`
        : "No server observations available" }}</small>
      <small>This is the whole server. It is not filtered by the tools or runs you selected,
        and it is never added to run costs.</small>
    </p>
    <div class="callout">{{ server?.allocation_reason }}</div>
    <div v-if="server?.items?.length" class="table-wrap">
      <table>
        <thead><tr><th>Resource</th><th>Machine</th><th>Observed</th><th>Cost</th></tr></thead>
        <tbody>
          <tr v-for="item in server?.items || []" :key="item.id">
            <td>{{ item.resource_uid }}</td>
            <td>{{ item.machine_type }} · {{ item.region }}</td>
            <td>{{ span(item.observed_start, item.observed_end) }}</td>
            <td>{{ formatCost(item.amount) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
