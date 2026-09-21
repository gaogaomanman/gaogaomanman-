<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useECharts } from './useECharts'
import type { EChartsOption } from 'echarts'

interface Props {
  labels: string[]
  values: number[]
  totals?: number[]
  fails?: number[]
  color?: string
  fillColor?: string
  maxValue?: number
  unit?: string
}

const props = withDefaults(defineProps<Props>(), {
  color: '#38bdf8',
  fillColor: 'rgba(56,189,248,0.22)',
  maxValue: 3,
  unit: '%',
})

const elRef = ref<HTMLElement | null>(null)
const { setOption } = useECharts(elRef, { show: false })

/**
 * 依据鼠标在容器内的位置，解析最近的雷达轴（指示器）下标。
 * 雷达图 series.data 只有一项、ECharts tooltip 的 params 不提供鼠标坐标、且 convertToPixel
 * 对雷达坐标系返回 undefined，故按 ECharts 雷达布局约定（默认 startAngle=90°、顺时针、center
 * 50%/50%、radius 65%）用纯几何反算各轴方向，按角度取最近轴。
 */
function pickIndicator(mx: number, my: number): number {
  const el = elRef.value
  const n = props.labels.length
  if (!el || n === 0) return -1
  const w = el.clientWidth
  const h = el.clientHeight
  const cx = w / 2
  const cy = h / 2
  const R = (Math.min(w, h) / 2) * 0.65
  const step = (2 * Math.PI) / n
  const angleAt = (x: number, y: number) => Math.atan2(y - cy, x - cx)
  const mouseAngle = angleAt(mx, my)
  let best = -1
  let bestDiff = Math.PI
  for (let i = 0; i < n; i += 1) {
    const angle = Math.PI / 2 - i * step
    const px = cx + R * Math.cos(angle)
    const py = cy - R * Math.sin(angle)
    let diff = Math.abs(angleAt(px, py) - mouseAngle)
    if (diff > Math.PI) diff = Math.PI * 2 - diff
    if (diff < bestDiff) {
      bestDiff = diff
      best = i
    }
  }
  return best
}

// ECharts radar 原生 tooltip 无法按轴定位，这里用容器级鼠标监听 + 自定义浮层显示最近轴信息
const tip = ref<{ x: number; y: number; text: string } | null>(null)

function onMove(e: MouseEvent) {
  const i = pickIndicator(e.offsetX, e.offsetY)
  if (i < 0 || i >= props.labels.length) {
    tip.value = null
    return
  }
  tip.value = {
    x: e.offsetX,
    y: e.offsetY,
    text: `${props.labels[i]}: ${props.values[i]}${props.unit}（不合格 ${props.fails?.[i] ?? 0} / 检测 ${props.totals?.[i] ?? 0}）`,
  }
}
function onLeave() {
  tip.value = null
}

function buildOption(): EChartsOption {
  const series = [
    {
      name: props.unit,
      type: 'radar' as const,
      data: [
        {
          value: props.values,
          name: props.unit,
          areaStyle: { color: props.fillColor },
          lineStyle: { color: props.color, width: 2.5 },
          itemStyle: { color: props.color },
          symbol: 'circle',
          symbolSize: 6,
        },
      ],
    },
  ]
  return {
    backgroundColor: 'transparent',
    tooltip: { show: false },
    radar: {
      indicator: props.labels.map((label) => ({ name: label, max: props.maxValue })),
      radius: '65%',
      splitNumber: 4,
      axisName: { color: '#e8f0f8', fontSize: 13, fontWeight: 'bold' as const },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.1)' } },
      splitArea: { show: false },
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.1)' } },
    },
    series,
  }
}

watch(
  () => [props.labels, props.values, props.totals, props.fails],
  () => setOption(buildOption()),
  { deep: true },
)

// 父级 v-if 保证组件挂载时数据已就绪，watch 不会首次触发，需挂载后主动渲染一次
onMounted(() => setOption(buildOption()))
</script>

<template>
  <div
    class="relative h-full min-h-[300px] w-full"
    @mousemove="onMove"
    @mouseleave="onLeave"
  >
    <!-- ECharts 独占此节点，浮层放在其外层，避免与 ECharts 争抢同一父节点的子元素 -->
    <div ref="elRef" class="h-full w-full" />
    <div
      v-if="tip"
      class="pointer-events-none absolute z-10 whitespace-nowrap rounded bg-black/80 px-2 py-1 text-xs text-white shadow-lg"
      :style="{ position: 'absolute', left: tip.x + 12 + 'px', top: tip.y + 12 + 'px' }"
    >
      {{ tip.text }}
    </div>
  </div>
</template>
