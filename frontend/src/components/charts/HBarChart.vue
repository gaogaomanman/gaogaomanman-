<script setup lang="ts">
/**
 * 横向条形图（用于「区域完成率对比」）。
 *
 * 为什么单独一个组件（而不是给 BarChart 加参数）：区域有十几到二十几个，
 * 纵向柱状图标签会挤在一起；横向条形图能完整显示区域名，且天然按完成率排序。
 *
 * 配色规则（`mode='rate'`）：≥100% 绿、80~99% 青、60~79% 黄、<60% 红——
 * 让"哪个区县掉队"一眼可见（完成率是这张图唯一要传达的信息）。
 */
import { onMounted, ref, watch } from 'vue'
import { echarts, useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'

interface Props {
  labels: string[]
  values: (number | null)[]
  /** 次要数值（如任务量），仅用于 tooltip */
  secondary?: number[]
  secondaryName?: string
  mode?: 'rate' | 'plain'
  height?: number
}

const props = withDefaults(defineProps<Props>(), {
  secondary: () => [],
  secondaryName: '',
  mode: 'rate',
  height: 320,
})

const elRef = ref<HTMLElement | null>(null)
const { setOption } = useECharts(elRef, { show: false })

function colorOf(v: number | null): string {
  if (props.mode !== 'rate') return '#4facfe'
  const n = v ?? 0
  if (n >= 100) return '#34D399'
  if (n >= 80) return '#00F2FE'
  if (n >= 60) return '#FDE68A'
  return '#FCA5A5'
}

function buildOption(): EChartsOption {
  const labels = [...props.labels].reverse()
  const values = [...props.values].reverse()
  const secondary = [...props.secondary].reverse()
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (items: unknown) => {
        const arr = items as { dataIndex: number; value: number }[]
        if (!arr.length) return ''
        const idx = arr[0].dataIndex
        const extra = props.secondary.length ? `<br/>${props.secondaryName || '任务量'}：${secondary[idx] ?? 0}` : ''
        const unit = props.mode === 'rate' ? '%' : ''
        return `${labels[idx]}<br/>完成率：${values[idx] ?? 0}${unit}${extra}`
      },
    },
    grid: { left: 72, right: 52, top: 12, bottom: 24 },
    xAxis: {
      type: 'value',
      max: props.mode === 'rate' ? Math.max(100, ...values.map((v) => v ?? 0)) : undefined,
      axisLabel: { color: '#9fb8c8', formatter: props.mode === 'rate' ? '{value}%' : '{value}' },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
    },
    yAxis: {
      type: 'category',
      data: labels,
      axisLine: { lineStyle: { color: '#9fb8c8' } },
      axisLabel: { color: '#c7d2fe', fontSize: 12 },
    },
    series: [
      {
        name: props.mode === 'rate' ? '完成率' : '数量',
        type: 'bar',
        data: values.map((v) => ({
          value: v ?? 0,
          itemStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
              { offset: 0, color: 'rgba(255,255,255,0.18)' },
              { offset: 1, color: colorOf(v) },
            ]),
            borderRadius: [0, 6, 6, 0],
          },
        })),
        barMaxWidth: 18,
        label: {
          show: true,
          position: 'right',
          color: '#fff',
          fontSize: 11,
          fontWeight: 'bold',
          formatter: props.mode === 'rate' ? '{c}%' : '{c}',
        },
      },
    ],
  }
}

watch(() => [props.labels, props.values], () => setOption(buildOption()), { deep: true })
onMounted(() => setOption(buildOption()))
</script>

<template>
  <div ref="elRef" class="w-full" :style="{ height: `${props.height}px` }" />
</template>
