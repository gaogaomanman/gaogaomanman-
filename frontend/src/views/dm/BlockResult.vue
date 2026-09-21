<script setup lang="ts">
/**
 * 达梦页各功能块的结果展示区。
 *
 * `type: 'html'` 直接渲染原页面写入 innerHTML 的提示（文案与配色逐字保留）；
 * `type: 'noList' / 'taskList'` 渲染为可点击芯片，替代原来的内联 onclick。
 */
import type { DmOutcome } from './outcome'

defineProps<{ outcome: DmOutcome | null }>()
const emit = defineEmits<{
  (e: 'pickNo', value: string, col: string): void
  (e: 'pickTask', value: string): void
}>()
</script>

<template>
  <div class="dm-result">
    <div v-if="outcome && outcome.type === 'html'" v-html="outcome.html"></div>
    <template v-else-if="outcome && outcome.type === 'noList'">
      <div v-html="outcome.intro"></div>
      <div class="dm-chips dm-chips-scroll">
        <span
          v-for="n in outcome.nos"
          :key="n"
          class="dm-chip dm-chip-no"
          @click="emit('pickNo', n, outcome.col)"
        >{{ n }}</span>
      </div>
    </template>
    <template v-else-if="outcome && outcome.type === 'taskList'">
      <div v-html="outcome.intro"></div>
      <div class="dm-chips">
        <span
          v-for="t in outcome.tasks"
          :key="t"
          class="dm-chip dm-chip-task"
          @click="emit('pickTask', t)"
        >{{ t }}</span>
      </div>
    </template>
  </div>
</template>

<style>
.dm-result { font-size: 13px; }
.dm-result .dm-chips {
  display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px;
}
.dm-result .dm-chips-scroll { max-height: 200px; overflow: auto; }
.dm-chip {
  cursor: pointer; padding: 5px 12px; border-radius: 6px; font-size: 13px;
  transition: .15s;
}
.dm-chip-task {
  background: #eef2ff; border: 1px solid #d0d8ff; color: var(--primary);
}
.dm-chip-task:hover { background: #d0d8ff; }
.dm-chip-no {
  background: #ede7f6; border: 1px solid #b39ddb; color: #4527a0;
}
.dm-chip-no:hover { background: #d1c4e9; }
</style>
