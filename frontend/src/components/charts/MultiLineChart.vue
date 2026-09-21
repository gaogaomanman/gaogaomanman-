<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'

export interface MultiLineSeries {
  name: string
  color: string
  data: (number | null)[]
}

interface Props {
  labels: string[]
  series: MultiLineSeries[]
}

const props = defineProps<Props>()

const elRef = ref<HTMLElement | null>(null)
const { setOption } = useECharts(elRef, { show: false })

function buildOption(): EChartsOption {
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' as const },
    legend: {
      top: 0,
      textStyle: { color: '#9fb8c8', fontSize: 13 },
    },
    grid: { left: 44, right: 16, top: 34, bottom: 28 },
    xAxis: {
      type: 'category',
      data: props.labels,
      axisLine: { lineStyle: { color: '#9fb8c8' } },
      axisLabel: { color: '#9fb8c8', interval: 0, rotate: props.labels.length > 8 ? 45 : 0 },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#9fb8c8', formatter: '{value}%' },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
    },
    series: props.series.map((s) => ({
      name: s.name,
      type: 'line' as const,
      data: s.data,
      smooth: 0.35,
      showSymbol: true,
      symbol: 'circle',
      symbolSize: 5,
      connectNulls: false,
      lineStyle: { color: s.color, width: 2.5 },
      itemStyle: { color: s.color },
      label: {
        show: true,
        position: 'top',
        color: '#fff',
        fontSize: 10,
        formatter: (p: { value: number | null }) => (p.value == null ? '' : `${p.value}%`),
      },
    })),
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
