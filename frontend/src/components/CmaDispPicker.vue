<script setup lang="ts">
/**
 * 处理意见 / 处置说明 的「历史记忆」选择器。
 *
 * 交互：输入框 + `[n ▾]` 入口 → 点开列出历史填过的文本（带使用次数），面板内可输关键词过滤，
 * 点一条只**填入输入框**（不落库），仍需点「保存」才写入——使用者始终是最终确认人。
 *
 * 为什么做成组件：核对结果表、台账明细「处理意见」、台账明细「处置/复核说明」是同一交互，
 * 三处各写一份必然漂移（本工程前几轮已经因"看起来一样的多份实现"踩过口径漂移的坑）。
 */
import { computed, nextTick, ref } from 'vue'

export interface HistoryOption {
  text: string
  uses: number
}

const props = defineProps<{
  modelValue: string
  options: HistoryOption[]
  placeholder?: string
  /** 无历史时的提示文案 */
  emptyText?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: string): void
}>()

const open = ref(false)
const kw = ref('')
const searchEl = ref<HTMLInputElement | null>(null)

const filtered = computed(() => {
  const k = kw.value.trim().toLowerCase()
  const src = k ? props.options.filter((o) => o.text.toLowerCase().includes(k)) : props.options
  return src.slice(0, 50)
})

async function toggle(): Promise<void> {
  open.value = !open.value
  if (open.value) {
    kw.value = ''
    await nextTick()
    searchEl.value?.focus()
  }
}

function pick(text: string): void {
  emit('update:modelValue', text)
  open.value = false
}
</script>

<template>
  <div class="cma-dp">
    <div class="cma-dp-row">
      <input
        :value="modelValue"
        class="inp cma-dp-inp"
        :placeholder="placeholder"
        @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      />
      <button
        type="button"
        class="cma-dp-toggle"
        :title="`从历史记忆中选择（共 ${options.length} 条）`"
        @click="toggle"
      >{{ options.length }} ▾</button>
    </div>
    <div v-if="open" class="cma-dp-panel">
      <div class="cma-dp-head">
        <input ref="searchEl" v-model="kw" class="cma-dp-search" placeholder="输入关键词过滤历史…" />
        <button type="button" class="cma-dp-close" title="收起" @click="open = false">×</button>
      </div>
      <div v-if="filtered.length === 0" class="cma-dp-empty">
        {{ options.length === 0
          ? (emptyText || '暂无历史记录：先手动填写并保存，下次就能从这里选。')
          : '没有匹配的历史记录，可直接手动输入。' }}
      </div>
      <ul v-else class="cma-dp-list">
        <li v-for="(o, i) in filtered" :key="i" class="cma-dp-item" :title="o.text" @click="pick(o.text)">
          <span class="cma-dp-text">{{ o.text }}</span>
          <span class="cma-dp-uses">×{{ o.uses }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>

<style>
/* 全部规则以 .cma-dp 前缀提升优先级：工程里 `.v-cma * { padding:0 }` 的全局 reset
   与单类选择器同优先级，靠"谁在后面"决定胜负太脆弱，这里直接用双类选择器压过去。 */
.cma-dp { position: relative; flex: 1; min-width: 0; }
.cma-dp .cma-dp-row { display: flex; align-items: center; gap: 4px; }
.cma-dp .cma-dp-inp { flex: 1; min-width: 120px; height: 30px; font-size: 12px; }
.cma-dp .cma-dp-toggle { flex: none; height: 30px; padding: 0 8px; border: 1.5px solid var(--border, #e0e4e8);
  background: #fff; border-radius: 6px; font-size: 12px; color: var(--text2, #7f8c8d); cursor: pointer; white-space: nowrap; }
.cma-dp .cma-dp-toggle:hover { border-color: var(--primary, #4f6ef7); color: var(--primary, #4f6ef7); }
.cma-dp .cma-dp-panel { margin-top: 4px; border: 1.5px solid var(--primary, #4f6ef7); border-radius: 8px; background: #fff;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.12); overflow: hidden; }
.cma-dp .cma-dp-head { display: flex; align-items: center; gap: 6px; padding: 6px;
  border-bottom: 1px solid var(--border, #e0e4e8); background: #fafbfc; }
.cma-dp .cma-dp-search { flex: 1; height: 26px; border: 1px solid var(--border, #e0e4e8); border-radius: 6px;
  padding: 0 8px; font-size: 12px; outline: none; min-width: 0; }
.cma-dp .cma-dp-search:focus { border-color: var(--primary, #4f6ef7); }
.cma-dp .cma-dp-close { flex: none; width: 24px; height: 24px; border: none; background: transparent; cursor: pointer;
  font-size: 16px; color: var(--text2, #7f8c8d); line-height: 1; }
.cma-dp .cma-dp-close:hover { color: var(--danger, #e74c3c); }
.cma-dp .cma-dp-empty { padding: 12px 10px; font-size: 12px; color: var(--text2, #7f8c8d); line-height: 1.6; }
.cma-dp .cma-dp-list { list-style: none; margin: 0; padding: 0; max-height: 200px; overflow-y: auto; }
.cma-dp .cma-dp-item { display: flex; align-items: flex-start; gap: 8px; padding: 7px 10px; cursor: pointer;
  font-size: 12px; line-height: 1.5; border-bottom: 1px solid #f0f2f5; }
.cma-dp .cma-dp-item:last-child { border-bottom: none; }
.cma-dp .cma-dp-item:hover { background: #eef3ff; }
.cma-dp .cma-dp-text { flex: 1; word-break: break-all; color: var(--text1, #2c3e50); }
.cma-dp .cma-dp-uses { flex: none; color: var(--text2, #7f8c8d); font-weight: 600; }
</style>
