<script setup lang="ts">
import { computed } from "vue";
import type { DailyItem, GroupItem, Invocation, Summary } from "../api";
import { periodOf, type ReportState } from "../api";
import { describePeriod } from "../periods";
import {
  coverageSentence, formatCost, formatDate, measureExplanation, measureName, needsCostData,
  pluralize, runStatusLabel, SERVER_EXPLANATION, undatedSentence,
} from "../vocabulary";

const props = defineProps<{
  state: ReportState;
  summary: Summary;
  days: DailyItem[];
  tools: GroupItem[];
  runs: Invocation[];
  serverWindow: string | null;
}>();
const emit = defineEmits<{
  view: [view: "runs" | "tool-runs" | "tools" | "daily" | "server"];
  run: [id: string];
  day: [date: string];
  "demo-period": [];
}>();

const period = computed(() => periodOf(props.state));
const incomplete = computed(() => props.summary.unpriced_job_count);
const recordedSoFar = computed(() => incomplete.value > 0 && props.summary.amount !== null);
const maxDay = computed(() => Math.max(0, ...props.days.map(day => Number(day.amount))));
const topRuns = computed(() =>
  [...props.runs]
    .sort((a, b) => Number(b.amount || 0) - Number(a.amount || 0))
    .slice(0, 4));
const topTools = computed(() =>
  [...props.tools]
    .sort((a, b) => Number(b.amount || 0) - Number(a.amount || 0))
    .slice(0, 4));
const failedOrRepeated = computed(() =>
  props.summary.failed_job_count > 0 || props.summary.repeated_job_count > 0);
const failedSentence = computed(() => {
  const { failed_job_count: count, failed_incomplete_job_count: missing } = props.summary;
  const amount = `${pluralize(count, "failed tool run")} account for ${formatCost(props.summary.failed_spend)}`;
  return missing ? `${amount} recorded so far; ${needsCostData(missing, "run")}.` : `${amount}.`;
});
// The whole cost of jobs that repeated is a different figure from what the
// repeats used; a resource shared by both attempts belongs to neither alone.
const repeatedSentence = computed(() => {
  const summary = props.summary;
  const jobs = `${pluralize(summary.repeated_job_count, "tool run")} needed more than one attempt `
    + `and cost ${formatCost(summary.repeated_job_spend)} in total.`;
  const repeats = `The repeat attempts themselves used ${formatCost(summary.repeat_attempt_spend)}`;
  if (summary.repeat_attempt_spend_complete) return `${jobs} ${repeats}.`;
  const shared = Number(summary.repeat_attempt_shared_spend) > 0
    ? `; another ${formatCost(summary.repeat_attempt_shared_spend)} went to resources a first `
      + "attempt and its repeat shared, which cannot be divided between them"
    : "";
  return `${jobs} ${repeats} recorded so far${shared}.`;
});
</script>

<template>
  <section class="headline" aria-labelledby="headline-measure">
    <p id="headline-measure" class="eyebrow">{{ measureName(state.basis) }}</p>
    <p class="headline-period">{{ describePeriod(period, state.timezone) }}</p>
    <p class="headline-amount">
      {{ formatCost(summary.amount) }}
      <span v-if="recordedSoFar" class="headline-qualifier">recorded so far</span>
    </p>
    <p class="headline-coverage">
      {{ coverageSentence(summary.job_count, incomplete) }}
      <span class="measure-note">Compute only · USD</span>
    </p>
    <p v-if="!summary.job_count && summary.demo_period" class="headline-explanation">
      {{ summary.imported_snapshot ? "This imported snapshot's data was recorded" : "This demonstration's data was recorded" }}
      {{ formatDate(summary.demo_period.from, state.timezone) }} –
      {{ formatDate(summary.demo_period.to, state.timezone) }}.
      <button class="link-button" @click="emit('demo-period')">Show that period</button>
    </p>
    <p v-if="summary.undated?.job_count" class="headline-explanation">
      {{ undatedSentence(summary.undated.job_count) }}
      <button class="link-button" @click="emit('view', 'tool-runs')">See them</button>
    </p>
    <p class="headline-explanation">{{ measureExplanation(state.basis) }}</p>

    <details v-if="failedOrRepeated" class="inline-details">
      <summary>Failed and repeated work</summary>
      <p v-if="summary.failed_job_count">{{ failedSentence }}</p>
      <p v-if="summary.repeated_job_count">{{ repeatedSentence }}</p>
      <p>All of this is already part of the amount above.</p>
    </details>
  </section>

  <section class="panel">
    <div class="panel-heading">
      <div><h2>Daily cost</h2><p>Select a day to see what ran.</p></div>
      <button class="link-button" @click="emit('view', 'daily')">View daily details</button>
    </div>
    <div v-if="days.length" class="trend">
      <button
        v-for="day in days" :key="day.date"
        :aria-label="`${day.date}: ${formatCost(day.amount)}`"
        :title="`${day.date}: ${formatCost(day.amount)}`"
        :style="{ height: `${Math.max(8, (Number(day.amount) / (maxDay || 1)) * 120)}px` }"
        @click="emit('day', day.date)"
      ></button>
    </div>
    <div v-else class="empty">No cost was recorded in this period.</div>
    <div v-if="days.length" class="table-wrap">
      <table>
        <caption class="sr-only">Daily cost for the selected period</caption>
        <thead><tr><th>Date</th><th>Cost</th><th>Runs</th></tr></thead>
        <tbody>
          <tr v-for="day in days" :key="day.date">
            <td>{{ day.date }}</td><td>{{ formatCost(day.amount) }}</td><td>{{ day.job_count }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <div class="overview-columns">
    <section class="panel">
      <div class="panel-heading">
        <div><h2>Workflow runs</h2><p>Your most expensive runs in this period.</p></div>
        <button class="link-button" @click="emit('view', 'runs')">See all runs</button>
      </div>
      <div v-if="!topRuns.length" class="empty">No workflow runs in this period.</div>
      <button v-for="run in topRuns" :key="run.id" class="rank-row" @click="emit('run', run.id)">
        <span>
          <strong>{{ run.workflow_name }}</strong>
          <small>{{ formatDate(run.started_at, state.timezone) }} · {{ runStatusLabel(run.run_status) }}</small>
        </span>
        <span class="rank-amount">{{ formatCost(run.amount) }}</span>
      </button>
    </section>

    <section class="panel">
      <div class="panel-heading">
        <div><h2>Tools</h2><p>Where most of that cost came from.</p></div>
        <button class="link-button" @click="emit('view', 'tools')">See all tools</button>
      </div>
      <div v-if="!topTools.length" class="empty">No tool runs in this period.</div>
      <button
        v-for="tool in topTools" :key="`${tool.tool_id}@${tool.tool_version}`" class="rank-row"
        @click="emit('view', 'tools')"
      >
        <span>
          <strong>{{ tool.tool_name || tool.tool_id }}</strong>
          <small>{{ tool.tool_version || "Unversioned" }} · {{ tool.job_count }} runs</small>
        </span>
        <span class="rank-amount">{{ formatCost(tool.amount) }}</span>
      </button>
    </section>
  </div>

  <section v-if="summary.can_view_infrastructure" class="panel quiet">
    <div class="panel-heading">
      <div><h2>Galaxy server cost</h2><p>{{ SERVER_EXPLANATION }}</p></div>
      <button class="link-button" @click="emit('view', 'server')">Server details</button>
    </div>
    <p class="quiet-amount">
      {{ formatCost(summary.baseline_infrastructure_amount) }}
      <small>{{ serverWindow ? `Observed ${serverWindow}` : "No server observations available" }}</small>
      <small>Shown separately; never added to run costs.</small>
    </p>
  </section>
</template>
