<script setup lang="ts">
/**
 * 各市区完成率条形图。
 *
 * 形态对齐使用方现有图表：**纵轴是完成率本身（0~1，上限 1.2）**、横轴是各区 + 合计，
 * 柱色用金色系（与"完成量"类图表的蓝青色区分开），柱顶标注百分比，便于直接截图汇报。
 *
 * 为什么不用通用的 BarChart：通用图是按数值大小排序、纵轴自适应、单位百分比；
 * 这张图要固定区县顺序（与表格行一致）并把合计放在最后，纵轴固定 0~1.2，否则跨月对比会失真。
 */
import { computed, onMounted, ref, watch } from 'vue'
import type { EChartsOption } from 'echarts'
import { useECharts } from '../../components/charts/useECharts'
import type { ProgressOverview } from '../../api/client'

const props = defineProps<{ data: ProgressOverview }>()

const elRef = ref<HTMLElement | null>(null)
const { setOption } = useECharts(elRef, { show: true })

/** 标题带上统计范围：单合同筛选时用合同号里"-"之后的部分（如 ps2026002-市例行 → 市例行） */
const title = computed(() => {
  const cs = props.data.filters.contracts
  let scope = ''
  if (cs.length === 1) {
    const no = cs[0]
    scope = no.includes('-') ? no.split('-').slice(1).join('-') : no
  } else if (cs.length > 1) {
    scope = `${cs.length} 个合同`
  }
  const biz = props.data.filters.bizs
  const bizScope = biz.length === 1 ? biz[0] : ''
  return `${props.data.year}年${scope}${bizScope}各市区完成率`
})

const labels = computed(() => [...props.data.rows.map((r) => r.region), '合计'])
const values = computed(() => [
  ...props.data.rows.map((r) => (r.rate === null ? 0 : Math.round(r.rate) / 100)),
  props.data.grand_total.rate === null ? 0 : Math.round(props.data.grand_total.rate) / 100,
])

function buildOption(): EChartsOption {
  return {
    backgroundColor: 'transparent',
    title: {
      text: title.value,
      left: 'center',
      top: 4,
      textStyle: { color: '#e6edf6', fontSize: 14, fontWeight: 'bold' },
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      valueFormatter: (v: unknown) => `${(Number(v) * 100).toFixed(1)}%`,
    },
    grid: { left: 46, right: 18, top: 46, bottom: 58 },
    xAxis: {
      type: 'category',
      data: labels.value,
      axisLine: { lineStyle: { color: '#9fb8c8' } },
      axisLabel: { color: '#9fb8c8', interval: 0, rotate: labels.value.length > 6 ? 30 : 0 },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 1.2,
      interval: 0.2,
      axisLabel: { color: '#9fb8c8', formatter: (v: number) => Number(v).toFixed(1) },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } },
    },
    series: [
      {
        name: '完成率',
        type: 'bar',
        data: values.value,
        barMaxWidth: 40,
        itemStyle: {
          borderRadius: [4, 4, 0, 0],
          color: (p: { dataIndex: number; data: unknown }) =>
            p.dataIndex === labels.value.length - 1
              ? '#F59E0B' // 合计用更深的金色区分
              : '#F0B429',
        },
        label: {
          show: true,
          position: 'top',
          color: '#fff',
          fontSize: 11,
          fontWeight: 'bold',
          textBorderColor: '#0f2027',
          textBorderWidth: 3,
          formatter: (p: { value: unknown }) => `${(Number(p.value) * 100).toFixed(1)}%`,
        },
      },
    ],
  }
}

watch(
  () => [labels.value, values.value, title.value],
  () => setOption(buildOption()),
  { deep: true },
)
onMounted(() => setOption(buildOption()))
</script>

<template>
  <div class="glass-card px-5 py-4">
    <div ref="elRef" class="h-full min-h-[300px] w-full" />
    <p class="mt-1 text-center text-[12px] text-ink-muted">
      纵轴为完成率（0 ~ 1.2）；无任务量的区显示为 0，请以主表中该区是否有任务量为准。
    </p>
  </div>
</template>
