<script setup lang="ts">
import type { TopItem } from '../api/types'

interface Props {
  items: TopItem[]
  unit?: string
  emptyText?: string
  rankColors?: string[]
}

const props = withDefaults(defineProps<Props>(), {
  unit: '批次',
  emptyText: '暂无数据',
  rankColors: () => [
    'linear-gradient(135deg, #fbbf24, #f97316)',
    'linear-gradient(135deg, #c084fc, #ec4899)',
    'linear-gradient(135deg, #34d399, #06b6d4)',
    'linear-gradient(135deg, #60a5fa, #a78bfa)',
    'linear-gradient(135deg, #64748b, #94a3b8)',
  ],
})
</script>

<template>
  <div class="flex flex-col gap-2">
    <div
      v-if="!items || items.length === 0"
      class="rounded-xl bg-white/5 px-2.5 py-2 text-sm text-ink-muted"
    >
      {{ props.emptyText }}
    </div>
    <div
      v-for="(item, i) in items"
      :key="item.name"
      class="flex items-center gap-3 rounded-xl bg-white/5 px-2.5 py-1.5 text-[15px] text-ink transition-all hover:translate-x-1 hover:bg-white/10"
    >
      <span
        class="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[10px] text-[15px] font-extrabold text-white shadow-[0_3px_10px_rgba(96,165,250,0.35)]"
        :style="{ background: props.rankColors[i % props.rankColors.length] }"
      >
        {{ i + 1 }}
      </span>
      <span class="min-w-0 flex-1 truncate font-medium" :title="item.name">{{ item.name }}</span>
      <span
        class="shrink-0 rounded-xl bg-ok/15 px-2.5 py-0.5 text-sm font-bold text-ok"
      >
        {{ item.cnt }} {{ props.unit }}
      </span>
    </div>
  </div>
</template>
