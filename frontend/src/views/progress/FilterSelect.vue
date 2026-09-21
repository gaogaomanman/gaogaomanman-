<script setup lang="ts">
/**
 * 多选下拉（合同 / 业务类别筛选用）。
 *
 * 为什么不直接用 `<select multiple>`：原生多选要按住 Ctrl 才能加选，页面使用者（业务同事）
 * 十有八九会误操作；这里用复选框下拉，并带「全选 / 清空」与已选数量，语义直白。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Check, ChevronDown } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    label: string
    options: { value: string; label: string; hint?: string }[]
    /** 空数组 = 全部（页面据此显示「全部」） */
    modelValue: string[]
    placeholder?: string
    width?: string
  }>(),
  { placeholder: '全部', width: 'w-[240px]' },
)

const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()

const open = ref(false)
const rootRef = ref<HTMLElement | null>(null)

const summary = computed(() => {
  if (!props.modelValue.length) return props.placeholder
  if (props.modelValue.length === 1) {
    const hit = props.options.find((o) => o.value === props.modelValue[0])
    return hit?.label || props.modelValue[0]
  }
  return `已选 ${props.modelValue.length} 项`
})

function toggle(value: string): void {
  const next = props.modelValue.includes(value)
    ? props.modelValue.filter((v) => v !== value)
    : [...props.modelValue, value]
  emit('update:modelValue', next)
}

function selectAll(): void {
  emit('update:modelValue', props.options.map((o) => o.value))
}

function clearAll(): void {
  emit('update:modelValue', [])
}

function onDocClick(e: MouseEvent): void {
  if (rootRef.value && !rootRef.value.contains(e.target as Node)) open.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div ref="rootRef" class="relative">
    <button
      class="year-select flex items-center justify-between gap-2"
      :class="width"
      :title="`${label}：${modelValue.length ? modelValue.join('、') : '全部'}`"
      @click="open = !open"
    >
      <span class="truncate">
        <span class="text-ink-muted">{{ label }}：</span>{{ summary }}
      </span>
      <ChevronDown :size="14" class="shrink-0 opacity-70" />
    </button>

    <div
      v-if="open"
      class="absolute right-0 z-[60] mt-2 max-h-[320px] w-[300px] overflow-auto rounded-xl border border-white/15 bg-[#241f52] p-2 shadow-glow-lg"
    >
      <div class="mb-1.5 flex items-center justify-between px-1">
        <span class="text-[12px] text-ink-muted">共 {{ options.length }} 项</span>
        <span class="flex gap-2">
          <button class="text-[12px] text-cyan hover:underline" @click="selectAll">全选</button>
          <button class="text-[12px] text-ink-muted hover:text-ink hover:underline" @click="clearAll">清空</button>
        </span>
      </div>
      <button
        v-for="o in options"
        :key="o.value"
        class="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[13px] transition-colors hover:bg-white/10"
        @click="toggle(o.value)"
      >
        <span
          class="flex h-4 w-4 shrink-0 items-center justify-center rounded border"
          :class="modelValue.includes(o.value) ? 'border-primary bg-primary' : 'border-white/30'"
        >
          <Check v-if="modelValue.includes(o.value)" :size="12" class="text-white" />
        </span>
        <span class="min-w-0 flex-1 truncate text-ink">{{ o.label }}</span>
        <span v-if="o.hint" class="shrink-0 text-[11px] text-ink-muted">{{ o.hint }}</span>
      </button>
      <p v-if="!options.length" class="px-2 py-3 text-center text-[12px] text-ink-muted">暂无可选项</p>
    </div>
  </div>
</template>
