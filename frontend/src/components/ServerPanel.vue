<script setup lang="ts">
import { formatCost, SERVER_EXPLANATION } from "../vocabulary";

defineProps<{
  server: {
    items?: Array<Record<string, string>>;
    amount?: string | null;
    allocation_reason?: string;
    observation_window?: Record<string, string>;
  } | null;
}>();

function window(server: Record<string, string> | undefined): string {
  if (!server?.from || !server?.to) return "the observed window";
  return `${new Date(server.from).toLocaleString()} – ${new Date(server.to).toLocaleString()}`;
}
</script>

<template>
  <section class="panel">
    <div class="panel-heading">
      <div><h2>Galaxy server cost</h2><p>{{ SERVER_EXPLANATION }}</p></div>
    </div>
    <p class="quiet-amount">
      {{ formatCost(server?.amount) }}
      <small>Observed {{ window(server?.observation_window) }}</small>
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
            <td>{{ new Date(item.observed_start).toLocaleString() }} –
              {{ new Date(item.observed_end).toLocaleString() }}</td>
            <td>{{ formatCost(item.amount) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
