<script setup lang="ts">
/**
 * 环形图（与 BarChart / LineChart 同风格：透明背景、深色轴文字、既有配色数组）。
 *
 * 与 BarChart 的差异：
 * - 默认**环形**（`innerRadius`）并显示图例——品类分布只有几项，环形比饼图更易读；
 * - 支持 `secondary`（任务量）作为 tooltip 的第二行，便于同时看"任务/完成"。
 */
import { onMounted, ref, watch } from 'vue'
import { useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'

interface Props {
  labels: string[]
  values: number[]
  /** 同名序列的第二个值（如任务量），仅用于 tooltip 展示 */
  secondary?: number[]
  secondaryName?: string
  colors?: string[]
  innerRadius?: string
  centerLabel?: string
  centerValue?: string
}

const props = withDefaults(defineProps<Props>(), {
  colors: () => ['#4facfe', '#00f2fe', '#5ce1a2', '#ffd93d', '#ff6b6b', '#c084fc', '#f471b5', '#22d3ee'],
  secondary: () => [],
  secondaryName: '',
  innerRadius: '52%',
  centerLabel: '',
  centerValue: '',
})

const elRef = ref<HTMLElement | null>(null)
const { setOption } = useECharts(elRef, { show: false })

function buildOption(): EChartsOption {
  const data = props.labels.map((name, i) => ({
    name,
    value: props.values[i] ?? 0,
    secondary: props.secondary[i] ?? 0,
    itemStyle: { color: props.colors[i % props.colors.length] },
  }))

  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      formatter: (p: { name: string; value: number; percent?: number; data?: { secondary?: number } }) => {
        const extra = props.secondary.length ? `<br/>${props.secondaryName || '任务量'}：${p.data?.secondary ?? 0}` : ''
        return `${p.name}<br/>完成量：${p.value}（${p.percent ?? 0}%）${extra}`
      },
    },
    legend: {
      bottom: 0,
      icon: 'circle',
      itemWidth: 8,
      itemHeight: 8,
      textStyle: { color: '#c7d2fe', fontSize: 12 },
    },
    series: [
      {
        name: props.centerLabel || '分布',
        type: 'pie',
        radius: [props.innerRadius, '72%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: 'rgba(15,23,42,0.6)', borderWidth: 2 },
        label: { show: false },
        emphasis: { scale: true, scaleSize: 6 },
        data,
      },
    ],
  }
}

watch(
  () => [props.labels, props.values, props.secondary],
  () => setOption(buildOption()),
  { deep: true },
)

// 父级 v-if 可能让组件在数据就绪后才挂载，watch 不会首次触发 → 挂载后主动渲染一次
onMounted(() => setOption(buildOption()))
</script>

<template>
  <div class="relative h-full min-h-[280px] w-full">
    <div ref="elRef" class="h-full min-h-[280px] w-full" />
    <div
      v-if="props.centerValue"
      class="pointer-events-none absolute left-1/2 top-[38%] -translate-x-1/2 -translate-y-1/2 text-center"
    >
      <div class="bg-gradient-to-b from-white to-[#dbeafe] bg-clip-text text-2xl font-extrabold text-transparent">
        {{ props.centerValue }}
      </div>
      <div class="mt-0.5 text-[12px] text-ink-muted">{{ props.centerLabel }}</div>
    </div>
  </div>
</template>
