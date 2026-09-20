<script setup lang="ts">
const props = defineProps<{ amount: string | null; currency?: string; quality?: string }>();

function display(): string {
  if (props.amount === null) return "Unknown";
  const value = Number(props.amount);
  if (value > 0 && value < 0.01) return "Less than $0.01";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: props.currency || "USD" }).format(value);
}
</script>

<template>
  <span class="cost-amount">{{ display() }}</span>
</template>
