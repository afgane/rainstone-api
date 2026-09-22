<script setup lang="ts">
import { RefreshCw, X } from "@lucide/vue";
import { computed, nextTick, ref, watch } from "vue";
import {
  capacityLabel, costExplanation, formatCost, jobStateLabel, needsCostData, pluralize,
  qualityLabel, runStatusLabel,
} from "../vocabulary";

const props = defineProps<{
  kind: "runs" | "tool-runs" | null;
  detail: Record<string, unknown> | null;
  loading: boolean;
  periodLabel: string;
}>();
const emit = defineEmits<{ close: []; open: [kind: "runs" | "tool-runs", id: string] }>();

const closeButton = ref<HTMLButtonElement | null>(null);
watch(() => props.detail, async detail => {
  if (detail) {
    await nextTick();
    closeButton.value?.focus();
  }
});

function list(key: string): Array<Record<string, unknown>> {
  const value = props.detail?.[key];
  return Array.isArray(value) ? (value as Array<Record<string, unknown>>) : [];
}
function text(key: string): string | null {
  const value = props.detail?.[key];
  return typeof value === "string" ? value : null;
}

const isRun = computed(() => props.kind === "runs");
// A run's headline is the whole run; the period share is secondary.
const runTotal = computed(() => text("run_total"));
const periodShare = computed(() => text("amount"));
const runDiffers = computed(() => runTotal.value !== periodShare.value);
const steps = computed(() => list("steps"));
const children = computed(() => list("children"));
const resources = computed(() => list("resources"));
const attempts = computed(() => list("attempts"));
</script>

<template>
  <div v-if="detail || loading" class="dialog-backdrop" @click.self="emit('close')">
    <section class="dialog" role="dialog" aria-modal="true" aria-labelledby="detail-title">
      <button ref="closeButton" class="icon-button" aria-label="Close details" @click="emit('close')">
        <X />
      </button>
      <div v-if="loading" class="loading"><RefreshCw class="spin" /> Loading details…</div>
      <template v-else-if="detail">
        <template v-if="isRun">
          <p class="eyebrow">Workflow run</p>
          <h2 id="detail-title">{{ detail.workflow_name }}</h2>
          <p class="dialog-amount">
            <strong>{{ formatCost(runTotal) }}</strong>
            <span>Run total{{ detail.run_total_complete ? "" : " recorded so far" }}</span>
          </p>
          <p class="dialog-meta">
            {{ runStatusLabel(String(detail.run_status)) }} ·
            started {{ new Date(String(detail.started_at)).toLocaleString() }}
            <span v-if="detail.workflow_version"> · version {{ detail.workflow_version }}</span>
          </p>
          <p v-if="runDiffers" class="dialog-meta">
            {{ formatCost(periodShare) }} of this run falls inside {{ periodLabel }}.
          </p>
          <p v-if="Number(detail.run_unpriced_job_count)" class="dialog-meta">
            {{ needsCostData(Number(detail.run_unpriced_job_count)) }}, so this total is a subtotal.
          </p>
          <p v-if="Number(detail.reused_job_count)" class="dialog-meta">
            {{ pluralize(Number(detail.reused_job_count), "step") }} reused earlier outputs and
            added no new compute.
          </p>

          <h3>Steps</h3>
          <div v-if="!steps.length" class="empty">No tool runs are recorded for this run yet.</div>
          <ul v-else class="step-list">
            <li v-for="step in steps" :key="String(step.step_key)">
              <template v-if="step.job">
                <button
                  class="link-button"
                  @click="emit('open', 'tool-runs', String((step.job as Record<string, unknown>).id))"
                >{{ (step.job as Record<string, unknown>).tool_name }}</button>
                <span>{{ jobStateLabel(String((step.job as Record<string, unknown>).state)) }}</span>
                <span>{{ formatCost((step.job as Record<string, unknown>).amount as string) }}</span>
                <small>{{ capacityLabel((step.job as Record<string, unknown>).capacities as string[]) }}</small>
              </template>
              <small v-else>Step {{ step.step_key }} has no authorized matching run.</small>
            </li>
          </ul>

          <template v-if="children.length">
            <h3>Child workflows</h3>
            <ul class="step-list">
              <li v-for="child in children" :key="String(child.id)">
                <button class="link-button" @click="emit('open', 'runs', String(child.id))">
                  {{ child.workflow_name }}
                </button>
                <span>{{ pluralize(Number(child.run_job_count || child.job_count), "tool run") }}</span>
                <span>{{ formatCost(child.run_total as string) }}</span>
              </li>
            </ul>
            <p class="dialog-meta">Child runs are already counted once in the run total above.</p>
          </template>
        </template>

        <template v-else>
          <p class="eyebrow">Tool run</p>
          <h2 id="detail-title">{{ detail.tool_name }}</h2>
          <p class="dialog-amount">
            <strong>{{ formatCost(text("full_job_amount")) }}</strong>
            <span>Cost of this run</span>
          </p>
          <p class="dialog-meta">
            {{ jobStateLabel(String(detail.state)) }} ·
            {{ new Date(String(detail.created_at)).toLocaleString() }} ·
            {{ qualityLabel(String(detail.quality)) }}
          </p>
          <p v-if="text('interval_amount') !== text('full_job_amount')" class="dialog-meta">
            {{ formatCost(text("interval_amount")) }} of it falls inside {{ periodLabel }}.
          </p>
          <p class="explanation">
            {{ costExplanation({
              quality: String(detail.quality),
              amount: text("full_job_amount"),
              reason: String(detail.reason || ""),
              capacities: detail.capacities as string[],
            }) }}
          </p>

          <h3>Where it ran</h3>
          <ul class="step-list">
            <li v-for="resource in resources" :key="String(resource.lifetime_id)">
              <span>{{ resource.machine_type || "Your Galaxy server" }}</span>
              <span>{{ capacityLabel([String(resource.capacity_relationship)]) }}</span>
              <span>{{ formatCost(resource.amount as string) }}</span>
              <small v-if="Number(resource.shared_attempt_count) > 1">
                Charged once for {{ resource.shared_attempt_count }} attempts that reused it
              </small>
            </li>
          </ul>

          <h3>Attempts</h3>
          <ul class="step-list">
            <li v-for="attempt in attempts" :key="String(attempt.id)">
              <span>{{ jobStateLabel(String(attempt.outcome)) }}</span>
              <span v-if="attempt.tool_started_at">
                {{ new Date(String(attempt.tool_started_at)).toLocaleString() }}
              </span>
              <span v-if="attempt.amount">{{ formatCost(attempt.amount as string) }}</span>
              <small v-else-if="attempts.length > 1">Shares the resource charge above</small>
            </li>
          </ul>

          <details class="inline-details">
            <summary>Technical details</summary>
            <p class="mono">{{ detail.tool_id }}</p>
            <p class="mono">Job {{ detail.source_id }} · snapshot {{ detail.revision_id }}</p>
            <p>{{ detail.reason }}</p>
          </details>
        </template>
      </template>
    </section>
  </div>
</template>
