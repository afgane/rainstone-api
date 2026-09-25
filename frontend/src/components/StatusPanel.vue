<script setup lang="ts">
import type { Freshness, Status } from "../api";
import { diagnosticsUrl } from "../api";
import { formatDateTime } from "../vocabulary";

defineProps<{ status: Status | null; freshness: Freshness | null; timezone: string }>();
</script>

<template>
  <section class="panel">
    <div class="panel-heading">
      <div>
        <h2>Deployment status</h2>
        <p>Read-only self-checks for this instance. Diagnostics exclude credentials,
          connection strings and job parameters.</p>
      </div>
      <a class="secondary" :href="diagnosticsUrl()" download>Download diagnostics</a>
    </div>
    <div class="callout">
      Overall {{ status?.overall_status }} · identity mode {{ status?.auth_mode }} ·
      instance {{ status?.tenant }}
    </div>
    <p v-for="recorded in status?.recorded_reports || []" :key="recorded.context" class="recorded">
      Checks recorded by the {{ recorded.context }} {{ recorded.age_seconds }}s ago
      ({{ recorded.overall_status }}){{ recorded.stale ? " — stale, treat as unverified" : "" }}.
    </p>
    <p v-if="!(status?.recorded_reports || []).length" class="recorded">
      No collector or initialization checks have been recorded yet, so source and cloud access is
      unverified here.
    </p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Check</th><th>Status</th><th>Finding</th></tr></thead>
        <tbody>
          <tr v-for="check in status?.checks || []" :key="check.name">
            <td><strong>{{ check.name }}</strong></td>
            <td>{{ check.status }}</td>
            <td>{{ check.detail }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel-heading"><div><h2>Collection</h2>
      <p>Collector {{ freshness?.overall_status }}.</p></div></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Source</th><th>State</th><th>Last success</th></tr></thead>
        <tbody>
          <tr v-for="source in freshness?.sources || []" :key="source.source">
            <td>{{ source.source }}</td><td>{{ source.status }}</td>
            <td>{{ source.last_success_at ? formatDateTime(source.last_success_at, timezone) : "never" }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="freshness?.observation_gaps?.length" class="recorded">
      {{ freshness.observation_gaps.length }} observation gaps recorded.
    </p>
  </section>
</template>
