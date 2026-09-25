<script setup lang="ts">
import { Activity, BarChart3, Building2, Search, Server, Users, Wrench, Workflow, X } from "@lucide/vue";
import { computed } from "vue";
import { activeFilters, FILTER_LABELS, periodOf, type AdvancedFilter, type ReportState, type View } from "../api";
import { describePeriod, PERIOD_LABELS, PERIOD_ORDER, type PeriodId } from "../periods";

const props = defineProps<{
  state: ReportState;
  canViewServer: boolean;
  canViewUsers: boolean;
  advancedOpen: boolean;
}>();
const emit = defineEmits<{
  view: [view: View];
  period: [period: PeriodId];
  // The sidebar never mutates report state directly; it reports the change.
  set: [key: string, value: string];
  "toggle-advanced": [];
  "clear-filter": [key: AdvancedFilter];
  clear: [];
}>();

function update(key: string, event: Event): void {
  emit("set", key, (event.target as HTMLInputElement | HTMLSelectElement).value);
}

const PRIMARY: Array<{ id: View; label: string; icon: unknown }> = [
  { id: "overview", label: "Overview", icon: BarChart3 },
  { id: "runs", label: "Workflow runs", icon: Workflow },
  { id: "tool-runs", label: "Tool runs", icon: Wrench },
  { id: "tools", label: "Tools", icon: Building2 },
];

const secondary = computed(() => [
  ...(props.canViewUsers ? [{ id: "users" as View, label: "Galaxy accounts", icon: Users }] : []),
  ...(props.canViewServer ? [{ id: "server" as View, label: "Galaxy server", icon: Server }] : []),
  { id: "status" as View, label: "Status", icon: Activity },
]);

const chips = computed(() => activeFilters(props.state));
const period = computed(() => periodOf(props.state));
const searchLabel = computed(() =>
  props.state.view === "runs" ? "Find a workflow run"
    : props.state.view === "tools" ? "Search tools"
      : props.state.view === "tool-runs" ? "Find a tool run"
        : "Search your work");
// Status is operational: report controls do not belong there.
const showControls = computed(() => props.state.view !== "status");
</script>

<template>
  <nav class="sidebar" aria-label="Reporting">
    <div class="nav-group">
      <button
        v-for="item in PRIMARY" :key="item.id" class="nav-item"
        :aria-current="state.view === item.id ? 'page' : undefined"
        @click="emit('view', item.id)"
      >
        <component :is="item.icon" :size="18" aria-hidden="true" />{{ item.label }}
      </button>
    </div>

    <template v-if="showControls">
      <div class="control-group" role="group" aria-labelledby="period-label">
        <h2 id="period-label">Period</h2>
        <div class="period-buttons">
          <button
            v-for="id in PERIOD_ORDER" :key="id" class="chip-button"
            :aria-pressed="state.period === id" @click="emit('period', id)"
          >{{ PERIOD_LABELS[id] }}</button>
        </div>
        <p class="resolved">{{ describePeriod(period, state.timezone) }}</p>
        <div v-if="state.period === 'custom'" class="custom-dates">
          <label><span>From</span>
            <input :value="state.fromTime" type="date" @change="update('fromTime', $event)"></label>
          <label><span>To (included)</span>
            <input :value="state.toTime" type="date" @change="update('toTime', $event)"></label>
        </div>
      </div>

      <div class="control-group">
        <label class="search"><Search :size="16" aria-hidden="true" />
          <span class="sr-only">{{ searchLabel }}</span>
          <input :value="state.search" :placeholder="searchLabel" @input="update('search', $event)">
        </label>
      </div>

      <div class="control-group">
        <button
          class="disclosure" :aria-expanded="advancedOpen" aria-controls="advanced-filters"
          @click="emit('toggle-advanced')"
        >
          More filters<span v-if="chips.length" class="count">{{ chips.length }}</span>
        </button>
        <!-- Active advanced filters stay visible and removable while collapsed,
             so a hidden selection can never change an easy answer silently. -->
        <ul v-if="chips.length" class="chips">
          <li v-for="chip in chips" :key="chip.key">
            <!-- A filter value can be a full Tool Shed identifier, so the label
                 truncates inside the sidebar and keeps the whole value in its
                 tooltip and accessible name. -->
            <button
              :title="`${FILTER_LABELS[chip.key]}: ${chip.value}`"
              @click="emit('clear-filter', chip.key)"
            >
              <span class="chip-label">{{ FILTER_LABELS[chip.key] }}: {{ chip.value }}</span>
              <X :size="14" aria-hidden="true" />
              <span class="sr-only">Remove the {{ FILTER_LABELS[chip.key] }} filter,
                currently {{ chip.value }}</span>
            </button>
          </li>
        </ul>
        <div v-show="advancedOpen" id="advanced-filters" class="advanced">
          <label v-if="state.view !== 'tools'"><span>Status</span>
            <select 
              :value="state.state" @change="update('state', $event)">
              <option value="">Any status</option>
              <option value="ok">Completed</option>
              <option value="error">Failed</option>
              <option value="running">Running</option>
              <option value="paused">Paused</option>
            </select></label>
          <label><span>Cost coverage</span>
            <select 
              :value="state.quality" @change="update('quality', $event)">
              <option value="">Any</option>
              <option value="known_zero">Used your Galaxy server</option>
              <option value="approximate">Estimated</option>
              <option value="partial">Cost incomplete</option>
              <option value="unpriced">Price unavailable</option>
              <option value="in_progress">Still running</option>
              <option value="unavailable">Cost data unavailable</option>
              <option value="not_started">Not run yet</option>
            </select></label>
          <label><span>Where it ran</span>
            <select 
              :value="state.capacity" @change="update('capacity', $event)">
              <option value="">Anywhere</option>
              <option value="existing">Your Galaxy server</option>
              <option value="dedicated">Dedicated cloud compute</option>
              <option value="unknown">Not established</option>
            </select></label>
          <label v-if="state.view !== 'runs'"><span>Tool ID</span>
            <input 
              :value="state.toolId" @change="update('toolId', $event)"></label>
          <label v-if="state.view !== 'runs'"><span>Tool version</span>
            <input 
              :value="state.toolVersion" @change="update('toolVersion', $event)"></label>
          <label v-if="state.view === 'runs'"><span>Workflow ID</span>
            <input 
              :value="state.workflowId" @change="update('workflowId', $event)"></label>
          <label><span>Minimum cost</span>
            <input :value="state.minCost" type="number" min="0" step="any" @change="update('minCost', $event)"></label>
          <label><span>Maximum cost</span>
            <input :value="state.maxCost" type="number" min="0" step="any" @change="update('maxCost', $event)"></label>
          <label v-if="canViewUsers"><span>Galaxy account</span>
            <input 
              :value="state.owner" @change="update('owner', $event)"></label>
          <label><span>Cost measure</span>
            <select 
              :value="state.basis" @change="update('basis', $event)">
              <option value="additional">Estimated run compute cost</option>
              <option value="allocated">Resource allocation estimate</option>
            </select></label>
        </div>
        <button v-if="chips.length" class="link-button" @click="emit('clear')">Clear filters</button>
      </div>
    </template>

    <div class="nav-group secondary-group">
      <button
        v-for="item in secondary" :key="item.id" class="nav-item"
        :aria-current="state.view === item.id ? 'page' : undefined"
        @click="emit('view', item.id)"
      >
        <component :is="item.icon" :size="18" aria-hidden="true" />{{ item.label }}
      </button>
    </div>
  </nav>
</template>
