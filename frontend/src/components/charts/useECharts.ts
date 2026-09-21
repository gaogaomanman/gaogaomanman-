// ECharts 组合式封装：init / resize / dispose / 数值标注
import { onBeforeUnmount, onMounted, shallowRef, watch, type Ref } from 'vue'
import * as echarts from 'echarts'

export interface ValueLabel {
  /** 是否在图表上直接标注数值 */
  show: boolean
  /** 标注颜色 */
  color?: string
}

export function useECharts(elRef: Ref<HTMLElement | null>, valueLabel: ValueLabel = { show: true }) {
  const chart = shallowRef<echarts.ECharts | null>(null)
  let observer: ResizeObserver | null = null

  function init() {
    if (!elRef.value) return
    chart.value = echarts.init(elRef.value)
  }

  function setOption(option: echarts.EChartsOption) {
    if (!chart.value) return
    chart.value.setOption(option, true)
  }

  function resize() {
    chart.value?.resize()
  }

  onMounted(() => {
    init()
    if (elRef.value) {
      observer = new ResizeObserver(() => resize())
      observer.observe(elRef.value)
    }
  })

  onBeforeUnmount(() => {
    observer?.disconnect()
    chart.value?.dispose()
    chart.value = null
  })

  // 数值标注插件（追加到 option 的 series 上，由各图表组件使用）
  function valueLabelSeriesOption(series: echarts.SeriesOption[]): echarts.SeriesOption[] {
    if (!valueLabel.show) return series
    return series.map((s) => ({
      ...s,
      label: {
        show: true,
        position: 'top',
        color: valueLabel.color || '#fff',
        fontSize: 11,
        fontWeight: 'bold' as const,
        textBorderColor: '#0f2027',
        textBorderWidth: 3,
        ...(s.label || {}),
      },
    }))
  }

  return { chart, init, setOption, resize, valueLabelSeriesOption }
}

export { echarts }
