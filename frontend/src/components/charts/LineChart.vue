<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'

interface Props {
  labels: string[]
  values: number[]
  color?: string
  fillColor?: string
  showValue?: boolean
  unit?: string
}

const props = withDefaults(defineProps<Props>(), {
  color: '#00f2fe',
  fillColor: 'rgba(0,242,254,0.15)',
  showValue: true,
  unit: '',
})

const elRef = ref<HTMLElement | null>(null)
const { setOption, valueLabelSeriesOption } = useECharts(elRef, { show: props.showValue })

function buildOption(): EChartsOption {
  const series = [
    {
      name: '数量',
      type: 'line' as const,
      data: props.values,
      smooth: 0.35,
      showSymbol: true,
      symbol: 'circle',
      symbolSize: 5,
      lineStyle: { color: props.color, width: 2 },
      itemStyle: { color: props.color },
      areaStyle: { color: props.fillColor },
    },
  ]
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' as const },
    grid: { left: 40, right: 16, top: 28, bottom: 28 },
    xAxis: {
      type: 'category',
      data: props.labels,
      axisLine: { lineStyle: { color: '#9fb8c8' } },
      axisLabel: { color: '#9fb8c8', interval: 0, rotate: props.labels.length > 8 ? 45 : 0 },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#9fb8c8' },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
    },
    series: valueLabelSeriesOption(series),
  }
}

watch(
  () => [props.labels, props.values],
  () => setOption(buildOption()),
  { deep: true },
)

// 父级 v-if 保证组件挂载时数据已就绪，watch 不会首次触发，需挂载后主动渲染一次
onMounted(() => setOption(buildOption()))
</script>

<template>
  <div ref="elRef" class="h-full min-h-[260px] w-full" />
</template>
