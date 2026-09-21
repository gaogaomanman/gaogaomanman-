<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'

interface Props {
  labels: string[]
  values: number[]
  colors?: string[]
  showValue?: boolean
  unit?: string
}

const props = withDefaults(defineProps<Props>(), {
  colors: () => ['#4facfe', '#00f2fe', '#5ce1a2', '#ffd93d', '#ff6b6b', '#c084fc', '#f471b5', '#22d3ee'],
  showValue: true,
  unit: '',
})

const elRef = ref<HTMLElement | null>(null)
const { setOption, valueLabelSeriesOption } = useECharts(elRef, { show: props.showValue })

function buildOption(): EChartsOption {
  const series = [
    {
      name: '数量',
      type: 'bar' as const,
      data: props.values,
      barMaxWidth: 42,
      itemStyle: {
        borderRadius: [6, 6, 0, 0],
        color: (params: { dataIndex: number }) =>
          props.colors[params.dataIndex % props.colors.length],
      },
    },
  ]
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis' as const,
      axisPointer: { type: 'shadow' as const },
    },
    grid: { left: 40, right: 16, top: 28, bottom: 28 },
    xAxis: {
      type: 'category',
      data: props.labels,
      axisLine: { lineStyle: { color: '#9fb8c8' } },
      axisLabel: { color: '#9fb8c8', interval: 0, rotate: props.labels.length > 5 ? 45 : 0 },
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
