<script setup lang="ts">
interface Props {
  label: string
  value: string | number
  unit?: string
  gradient: string
}

const props = withDefaults(defineProps<Props>(), {
  unit: '',
  gradient: 'linear-gradient(90deg, #60a5fa, #38bdf8)',
})
</script>

<template>
  <div
    class="glass-card relative overflow-hidden px-4 py-5 text-center transition-all hover:-translate-y-1 hover:shadow-glow-lg"
  >
    <div class="absolute inset-x-0 top-0 h-[3px]" :style="{ background: props.gradient }" />
    <div
      class="bg-gradient-to-b from-white to-[#e0e7ff] bg-clip-text text-4xl font-extrabold text-transparent"
    >
      {{ props.value }}
      <small v-if="props.unit" class="ml-0.5 text-sm font-medium text-ink-muted">{{ props.unit }}</small>
    </div>
    <div class="mt-2 text-[13px] font-medium tracking-wide text-ink-muted">{{ props.label }}</div>
  </div>
</template>
