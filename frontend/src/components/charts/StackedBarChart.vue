<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'

interface Props {
  labels: string[]
  series: Record<string, number[]>
  colors?: Record<string, string>
  showValue?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  colors: () => ({ 农产品: '#34d399', 畜产品: '#a78bfa', 水产品: '#06b6d4', 其他: '#fbbf24' }),
  showValue: true,
})

const elRef = ref<HTMLElement | null>(null)
const { setOption } = useECharts(elRef, { show: false })

function buildOption(): EChartsOption {
  const names = Object.keys(props.series || {})
  const series = names.map((name) => ({
    name,
    type: 'bar' as const,
    stack: 'total',
    data: props.series[name] || [],
    barMaxWidth: 26,
    itemStyle: { color: props.colors[name] || '#34d399', borderRadius: 0 },
  }))
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis' as const,
      axisPointer: { type: 'shadow' as const },
    },
    legend: {
      top: 0,
      textStyle: { color: '#e0e7ff', fontSize: 13 },
    },
    grid: { left: 40, right: 16, top: 32, bottom: 28 },
    xAxis: {
      type: 'category',
      data: props.labels,
      axisLine: { lineStyle: { color: '#c7d2fe' } },
      axisLabel: { color: '#c7d2fe' },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#c7d2fe' },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
    },
    series,
  }
}

watch(
  () => [props.labels, props.series],
  () => setOption(buildOption()),
  { deep: true },
)

// 父级 v-if 保证组件挂载时数据已就绪，watch 不会首次触发，需挂载后主动渲染一次
onMounted(() => setOption(buildOption()))
</script>

<template>
  <div ref="elRef" class="h-full min-h-[260px] w-full" />
</template>
