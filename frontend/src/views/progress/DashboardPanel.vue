<script setup lang="ts">
/**
 * 数据看板：总览卡片 + 大类完成率 + 各市区完成率 + 区域×品类进度大表。
 *
 * 版面顺序按使用方要求：**主表之后不再放任何图表**（只留口径说明）。
 *
 * 为什么要把未归类/未映射区域摆在明面上：完成量是**镜像按规则归类**出来的，
 * 一旦有样品没接住，表格就会静默偏小；业务同事看到的数字会与手工台账对不上，
 * 而原因必须能一眼看到（点提示可直接跳到处理页）。
 */
import { computed, ref } from 'vue'
import ExcelJS from 'exceljs'
import { AlertTriangle, Download, MapPinOff, RefreshCw } from 'lucide-vue-next'
import StatCard from '../../components/StatCard.vue'
import type { ProgressOverview } from '../../api/client'
import MatrixTable from './MatrixTable.vue'
import KindSummary from './KindSummary.vue'
import RegionRateChart from './RegionRateChart.vue'

const props = defineProps<{ data: ProgressOverview }>()
const emit = defineEmits<{ goto: [tab: string]; refresh: [] }>()

const exporting = ref(false)
const exportError = ref('')

const s = computed(() => props.data.summary)
const g = computed(() => props.data.grand_total)

const hasIssues = computed(
  () =>
    s.value.done_unmatched > 0 ||
    s.value.done_unmapped_region > 0 ||
    s.value.done_ignored > 0 ||
    s.value.done_no_task > 0 ||
    s.value.contract_filtered ||
    !!props.data.sync.last_error,
)

/* ==================== 导出 Excel（与看板同口径） ==================== */

/** 正在导出的模式：false = 进度对照表；true = 未完成量（未完成量 = 任务量 - 完成量） */
const exportUnfinished = ref(false)

/**
 * 表头行数：明细 3 行、汇总 2 行；导出结构与页面完全一致，便于直接拿去汇报。
 * `unfinished=true`：把每列的「完成量」换成「未完成量 = 任务量 - 完成量」（超额完成为负数），
 * 其余（表头结构、任务量、完成率、合计）不变——两张表放在一起能直接看出"还差多少、差在哪"。
 */
async function exportExcel(unfinished = false): Promise<void> {
  exportUnfinished.value = unfinished
  exporting.value = true
  exportError.value = ''
  try {
    const { categories, rows, category_totals: categoryTotals, grand_total: grandTotal } = props.data
    const wb = new ExcelJS.Workbook()
    const ws = wb.addWorksheet(
      `${props.data.year}年${props.data.task?.name || ''}${unfinished ? '未完成' : '进度'}`.slice(0, 31),
    )

    const ctot = new Map(categoryTotals.map((t) => [t.category_id, t]))
    /** 未完成量 = 任务量 - 完成量（超额完成为负数，照实显示） */
    const unfin = (quota: number | undefined, done: number | undefined) => (quota ?? 0) - (done ?? 0)

    // 表头：品类 → 任务量/完成量（未完成模式=未完成量）（+ 合计、完成率），与页面完全一致
    ws.getCell(1, 1).value = '县区市'
    ws.mergeCells(1, 1, 2, 1)
    let col = 2
    categories.forEach((cat) => {
      ws.getCell(1, col).value = cat.name
      ws.mergeCells(1, col, 1, col + 1)
      ws.getCell(2, col).value = '任务量'
      ws.getCell(2, col + 1).value = unfinished ? '未完成量' : '完成量'
      col += 2
    })
    const sumStart = col
    ws.getCell(1, col).value = '合计'
    ws.mergeCells(1, col, 1, col + 1)
    ws.getCell(2, col).value = '任务量'
    ws.getCell(2, col + 1).value = unfinished ? '未完成量' : '完成量'
    const rateCol = col + 2
    ws.getCell(1, rateCol).value = '完成率'
    ws.mergeCells(1, rateCol, 2, rateCol)

    // 数据行（用**本区域**的品类格子，含品类级下达量）
    rows.forEach((row, ri) => {
      const r = 3 + ri
      ws.getCell(r, 1).value = row.region
      const catMap = new Map((row.category_cells ?? []).map((c) => [c.category_id, c]))
      let c2 = 2
      categories.forEach((cat) => {
        const cc = catMap.get(cat.id)
        ws.getCell(r, c2).value = cc?.quota ?? 0
        ws.getCell(r, c2 + 1).value = unfinished ? unfin(cc?.quota, cc?.done) : (cc?.done ?? 0)
        c2 += 2
      })
      ws.getCell(r, sumStart).value = row.quota
      ws.getCell(r, sumStart + 1).value = unfinished ? row.quota - row.done : row.done
      ws.getCell(r, rateCol).value = row.rate === null ? '—' : `${row.rate}%`
    })

    // 合计行（列合计）
    const totalRow = 3 + rows.length
    ws.getCell(totalRow, 1).value = '合计'
    let c3 = 2
    categories.forEach((cat) => {
      const ct = ctot.get(cat.id)
      ws.getCell(totalRow, c3).value = ct?.quota ?? 0
      ws.getCell(totalRow, c3 + 1).value = unfinished ? unfin(ct?.quota, ct?.done) : (ct?.done ?? 0)
      c3 += 2
    })
    ws.getCell(totalRow, sumStart).value = grandTotal.quota
    ws.getCell(totalRow, sumStart + 1).value = unfinished ? grandTotal.quota - grandTotal.done : grandTotal.done
    ws.getCell(totalRow, rateCol).value = grandTotal.rate === null ? '—' : `${grandTotal.rate}%`

    // 口径说明（用数字对不上时能自己排查）
    const noteRow = totalRow + 2
    ws.getCell(noteRow, 1).value =
      `考核表：${props.data.task?.name || ''}（${props.data.year} 年，本表 ${categories.length} 列）；` +
      `合同=${props.data.filters.contracts.join('、') || '全部'}` +
      `；业务类别=${props.data.filters.bizs.join('、') || '全部'}` +
      `；截止=${props.data.filters.cutoff || '全年'}`
    ws.getCell(noteRow + 1, 1).value =
      `未归类样品 ${s.value.done_unmatched} 条；区域未映射 ${s.value.done_unmapped_region} 条；` +
      `未纳入本考核表 ${s.value.done_no_task} 条；忽略名单 ${s.value.done_ignored} 条；` +
      `镜像数据截止 ${props.data.sync.data_deadline || '—'}`
    if (unfinished) {
      ws.getCell(noteRow + 2, 1).value =
        '口径：未完成量 = 任务量 - 完成量；超额完成显示为负数；完成率为该行/列的完成进度（参考）'
    }
    ;[1, 2].forEach((i) => {
      const row = ws.getRow(i)
      row.font = { bold: true }
      row.alignment = { horizontal: 'center', vertical: 'middle' }
    })
    ws.getColumn(1).width = 12
    for (let i = 2; i <= rateCol; i++) ws.getColumn(i).width = 9
    ws.getColumn(3).width = 22

    const buf = await wb.xlsx.writeBuffer()
    const blob = new Blob([buf], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${props.data.year}年${props.data.task?.name || ''}${unfinished ? '未完成样品' : '抽采样进度统计'}.xlsx`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    exportError.value = (e as Error).message
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <div class="space-y-5">
    <!-- 总览卡片 -->
    <div class="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
      <StatCard label="总任务量" :value="g.quota" gradient="linear-gradient(90deg,#60a5fa,#38bdf8)" />
      <StatCard label="完成量（样品数）" :value="g.done" gradient="linear-gradient(90deg,#34d399,#22d3ee)" />
      <StatCard label="完成率" :value="g.rate === null ? '—' : g.rate" unit="%" gradient="linear-gradient(90deg,#a78bfa,#f472b6)" />
      <StatCard label="品类数" :value="s.category_count" gradient="linear-gradient(90deg,#fbbf24,#f97316)" />
      <StatCard label="区域数" :value="s.region_count" gradient="linear-gradient(90deg,#f87171,#fb7185)" />
      <StatCard label="涉及合同" :value="s.contract_count" gradient="linear-gradient(90deg,#38bdf8,#818cf8)" />
    </div>

    <!-- 提示条：没算进来的数据必须一眼可见 -->
    <div v-if="hasIssues" class="glass-card border-warn/40 bg-warn/10 px-5 py-3.5 text-[13px]">
      <div class="flex flex-wrap items-center gap-x-5 gap-y-2">
        <span class="inline-flex items-center gap-1.5 font-bold text-warn">
          <AlertTriangle :size="15" /> 有数据没进表
        </span>
        <button
          v-if="s.done_unmatched > 0"
          class="cursor-pointer text-warn hover:underline"
          @click="emit('goto', 'unmatched')"
        >
          {{ s.done_unmatched }} 条样品未归类到任何列（去处理 →）
        </button>
        <button
          v-if="s.done_unmapped_region > 0"
          class="inline-flex cursor-pointer items-center gap-1 text-warn hover:underline"
          @click="emit('goto', 'region')"
        >
          <MapPinOff :size="14" /> {{ s.done_unmapped_region }} 条样品区域未映射到区县市（去挂载 →）
        </button>
        <button
          v-if="s.done_no_task > 0"
          class="cursor-pointer text-warn hover:underline"
          @click="emit('goto', 'contract')"
        >
          {{ s.done_no_task }} 条样品不属于任何考核表（合同没挂任务，去合同管理 →）
        </button>
        <span v-if="s.contract_filtered" class="text-ink-muted">
          已按合同筛选完成量；任务量按任务下达、不随筛选变化，故此时完成率仅供参考
        </span>
        <span v-if="s.done_ignored > 0" class="text-ink-muted">{{ s.done_ignored }} 条在忽略名单中（不参与统计）</span>
        <span v-if="data.sync.last_error" class="text-err">最近一次同步失败：{{ data.sync.last_error }}</span>
      </div>
    </div>

    <!-- 核心信息 1+2：按产品类别的总完成率 + 各市区完成率条形图 -->
    <div class="grid grid-cols-1 gap-5 xl:grid-cols-2">
      <KindSummary :data="data" />
      <RegionRateChart :data="data" />
    </div>

    <!-- 核心信息 3：区域 × 品类进度主表（其后的图表已按要求全部去掉） -->
    <div class="space-y-4">
      <div class="flex flex-wrap items-center justify-end gap-3">
        <span class="text-[12px] text-ink-muted">数据取自本地快照 · 重算于 {{ (data.sync.synced_at || '尚未重算').replace('T', ' ') }}</span>
        <span v-if="exportError" class="text-[12px] text-err">导出失败：{{ exportError }}</span>
        <button class="tech-btn" @click="emit('refresh')">
          <RefreshCw :size="15" />刷新
        </button>
        <button class="tech-btn" :disabled="exporting" @click="exportExcel(false)">
          <Download :size="15" />
          {{ exporting && !exportUnfinished ? '导出中…' : '导出这张表' }}
        </button>
        <button
          class="tech-btn"
          :disabled="exporting"
          title="同样的表格，但「完成量」列换成「未完成量 = 任务量 - 完成量」"
          @click="exportExcel(true)"
        >
          <Download :size="15" />
          {{ exporting && exportUnfinished ? '导出中…' : '导出未完成样品' }}
        </button>
      </div>
      <MatrixTable
        :categories="data.categories"
        :rows="data.rows"
        :category-totals="data.category_totals"
        :grand-total="data.grand_total"
      />
    </div>

    <!-- 数据来源与口径：按使用方要求整块移除（2026-09-21），只保留表格右上角的"重算于"时间 -->
  </div>
</template>
