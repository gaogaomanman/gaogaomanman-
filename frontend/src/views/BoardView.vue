<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RefreshCw } from 'lucide-vue-next'
import { dashboardApi, navApi } from '../api/client'
import type {
  CompletionData,
  DetectRateData,
  ItemCountData,
  MonthlyData,
  ProductData,
  RegionData,
  SummaryData,
  TopData,
} from '../api/types'
import type { MultiLineSeries } from '../components/charts/MultiLineChart.vue'
import StatCard from '../components/StatCard.vue'
import TopList from '../components/TopList.vue'
import LineChart from '../components/charts/LineChart.vue'
import BarChart from '../components/charts/BarChart.vue'
import StackedBarChart from '../components/charts/StackedBarChart.vue'
import MultiLineChart from '../components/charts/MultiLineChart.vue'
import RadarChart from '../components/charts/RadarChart.vue'

const status = ref('未连接')
const statusType = ref('')

/* 数据来源提示：镜像模式下副标题必须写明「数据截止时间」。
   原副标题恒为"实时统计"，在镜像模式下会让人误以为看到的是实时数据。 */
const sourceText = ref('')
const sourceTitle = ref('')

async function loadSource(): Promise<void> {
  try {
    const s = await navApi.status()
    const m = s.mirror
    if (!m) return
    if (m.mode === 'mirror') {
      const t = (m.data_deadline || '').replace('T', ' ')
      sourceText.value = t ? `镜像数据 · 截止 ${t}` : '镜像数据（尚未同步）'
      sourceTitle.value = '统计基于本地 SQLite 镜像快照，非实时；要查最新数据请切换为直连模式并重新同步'
    } else {
      sourceText.value = '实时统计（直连源库）'
      sourceTitle.value = m.fallback || '统计直接取源库，为实时数据'
    }
  } catch {
    /* 状态获取失败时保持原文案 */
  }
}

void loadSource()

/* ===================== 数据源标签页（达梦 / LIMS） =====================
 * 两个数据源**同时都有数据的年份只有 2025**：
 *   - 达梦平台（DETECTION）：2025 年启用 → DT_SAMPLE 仅 2025/2026；
 *   - LIMS（SQL Server）  ：2025 年因系统切换**仅存部分**，2024 及之前才是完整来源。
 * 故按**数据源**分标签页，年份列表各自独立；2025 在两个标签下都能看，且口径互不混淆。
 * 口径（LIMS 侧）：样品数按送检单（Submission_ID）去重 · 项次按检测日期（Date_Test）· 判定取 Qualified。
 *            明细卡片原为「高频检测项目 Top10」，现改为「高风险项目（超标）项次 Top10」：
 *            只统计 Qualified = '不符合' 的项次再按项目汇总（口径见后端 board/service.py）。
 *
 * ⚠️ LIMS 侧取数策略：**只查当前选中的年份**，不一次把 2020–2025 全扫一遍。
 *    单年查询约 0.3~1 秒（年份区间 + 覆盖索引）；跨年全量扫描 11 秒以上。
 *    「历年对比」表改为**按需加载**（点按钮才查，查过就缓存）。
 */
const LIMS_YEARS = [2025, 2024, 2023, 2022, 2021, 2020]

type LimsDetail = {
  year: number
  samples: number
  items: number
  ok: number
  bad: number
  risk_items: { name: string; cnt: number }[]
  by_kind: { name: string; cnt: number }[]
}

const limsDetail = ref<LimsDetail | null>(null)
const limsOverview = ref<{
  years: number[]
  samples: number[]
  items: number[]
  ok: number[]
  bad: number[]
} | null>(null)
const limsOverviewLoading = ref(false)
const limsOverviewError = ref('')

/* 数据源标签页：'dm' = 达梦平台，'lims' = LIMS（SQL Server） */
const TABS = [
  { key: 'dm' as const, label: '达梦平台', hint: '2025–2026' },
  { key: 'lims' as const, label: 'LIMS（SQL Server）', hint: '2020–2025' },
]
const dataTab = ref<'dm' | 'lims'>('dm')

/* 是否走 LIMS 分支：**由标签页决定**（原先按年份判断 → 2025 无法区分两个数据源） */
const isLimsYear = computed(() => dataTab.value === 'lims')

/* 年份下拉：随标签页变化（达梦 = 接口返回值；LIMS = 2020–2025） */
const allYears = computed(() => {
  const list = isLimsYear.value ? LIMS_YEARS : [...years.value, new Date().getFullYear()]
  return [...new Set(list)].sort((a, b) => b - a)
})

/** 切换数据源标签页：取该标签的最新一年并重新取数。 */
function switchTab(key: 'dm' | 'lims'): void {
  if (dataTab.value === key) return
  dataTab.value = key
  const list = key === 'lims' ? LIMS_YEARS : [...years.value, new Date().getFullYear()]
  year.value = [...new Set(list)].sort((a, b) => b - a)[0]
  void loadAll()
}

function rate(ok: number, bad: number): string {
  const total = ok + bad
  return total > 0 ? `${((ok / total) * 100).toFixed(2)}%` : '—'
}

/** 只查当前选中的年份（快）：年度 4 个汇总数 + 该年明细。 */
async function loadLimsDetail(): Promise<void> {
  try {
    const resp = await dashboardApi.history(year.value)
    limsDetail.value = resp.data.detail ?? null
  } catch {
    limsDetail.value = null
  }
}

/** 历年对比表：**按需加载**（用户点按钮才查），已加载过直接复用缓存。 */
async function loadLimsOverview(): Promise<void> {
  if (limsOverview.value || limsOverviewLoading.value) return
  limsOverviewLoading.value = true
  limsOverviewError.value = ''
  try {
    const resp = await dashboardApi.historyOverview()
    limsOverview.value = resp.data
  } catch (e) {
    limsOverviewError.value = e instanceof Error ? e.message : String(e)
  } finally {
    limsOverviewLoading.value = false
  }
}
const year = ref(new Date().getFullYear())
const years = ref<number[]>([])
const refreshing = ref(false)

const summary = ref<SummaryData | null>(null)
const monthly = ref<MonthlyData | null>(null)
const product = ref<ProductData | null>(null)
const completion = ref<CompletionData | null>(null)
const itemCount = ref<ItemCountData | null>(null)
const detectRate = ref<DetectRateData | null>(null)
const top = ref<TopData | null>(null)
const risk = ref<TopData | null>(null)
const region = ref<RegionData | null>(null)
const productRisk = ref<RegionData | null>(null)

const errors = ref<Record<string, string>>({})

function fmt(n: number | undefined): string {
  if (n === undefined || n === null || isNaN(n)) return '-'
  return n.toLocaleString('zh-CN')
}

async function safeLoad<T>(key: string, fn: () => Promise<T>): Promise<T | null> {
  try {
    const resp = await fn()
    if (!resp.success) throw new Error((resp as { error?: string }).error || '接口失败')
    return (resp as { data: T }).data
  } catch (e) {
    errors.value[key] = e instanceof Error ? e.message : String(e)
    return null
  }
}

async function loadAll(force = false) {
  refreshing.value = true
  errors.value = {}
  const y = year.value

  // 2020–2024：达梦无数据，改走 LIMS 统计
  if (isLimsYear.value) {
    // 点「刷新数据」时**失效历年对比缓存**：该表查询代价高（跨年全扫）故做了缓存，
    // 但若镜像同步过，缓存会一直显示旧数据（口径对、时效旧）——刷新时必须让它重查。
    if (force) limsOverview.value = null
    // ⚠️ 达梦侧数据必须**清空**：否则残留上一个达梦年份（如 2026）的统计，
    //    而图表标题写的是当前年份 → 数据与标注不符。
    //    （图表区现在也只在达梦年份渲染，这里同步清空，双保险。）
    summary.value = null
    monthly.value = null
    product.value = null
    completion.value = null
    itemCount.value = null
    detectRate.value = null
    top.value = null
    risk.value = null
    region.value = null
    productRisk.value = null
    limsDetail.value = null
    // 只查当前年份；历年对比表按需加载（不在这里查，避免每次切年都跨年全扫）
    await loadLimsDetail()
    refreshing.value = false
    return
  }
  // 2025/2026：走达梦板块，清空 LIMS 明细（历年对比缓存保留，切回 LIMS 年份无需重查）
  limsDetail.value = null

  const [sum, mon, pro, com, itm, det, tp, rk, reg, prisk] = await Promise.all([
    safeLoad<SummaryData>('summary', () => dashboardApi.summary(y)),
    safeLoad<MonthlyData>('monthly', () => dashboardApi.monthly(y)),
    safeLoad<ProductData>('product', () => dashboardApi.product(y)),
    safeLoad<CompletionData>('completion', () => dashboardApi.completion(y)),
    safeLoad<ItemCountData>('itemCount', () => dashboardApi.itemCount(y)),
    safeLoad<DetectRateData>('detectRate', () => dashboardApi.detectRate(y)),
    safeLoad<TopData>('top', () => dashboardApi.top(y)),
    safeLoad<TopData>('risk', () => dashboardApi.risk(y)),
    safeLoad<RegionData>('region', () => dashboardApi.region(y)),
    safeLoad<RegionData>('productRisk', () => dashboardApi.productRisk(y)),
  ])

  summary.value = sum
  monthly.value = mon
  product.value = pro
  completion.value = com
  itemCount.value = itm
  detectRate.value = det
  top.value = tp
  risk.value = rk
  region.value = reg
  productRisk.value = prisk
  refreshing.value = false
}

async function loadYears() {
  try {
    const resp = await dashboardApi.years()
    if (resp.success) {
      years.value = resp.years
      if (!years.value.includes(year.value)) {
        year.value = years.value[0] || year.value
      }
    }
  } catch {
    years.value = [year.value]
  }
}

async function connect() {
  status.value = '连接中...'
  statusType.value = ''
  try {
    const h = await dashboardApi.health()
    status.value = '✓ 已连接'
    statusType.value = 'connected'
    void h
  } catch (e) {
    status.value = '连接失败: ' + (e instanceof Error ? e.message : '')
    statusType.value = 'error'
  }
}

function detectRateSeries(): MultiLineSeries[] {
  if (!detectRate.value) return []
  const colors: Record<string, string> = { 农产品: '#5ce1a2', 畜产品: '#ffd93d', 水产品: '#4facfe' }
  return Object.keys(detectRate.value.series).map((cat) => ({
    name: cat,
    color: colors[cat] || '#4facfe',
    data: detectRate.value!.series[cat].map((p) =>
      p.total > 0 ? Number(((p.pos / p.total) * 100).toFixed(2)) : null,
    ),
  }))
}

onMounted(async () => {
  await connect()
  await loadYears()
  await loadAll()
})
</script>

<template>
  <div class="mx-auto max-w-[1440px] px-6 py-6">
    <!-- 顶部 -->
    <header class="mb-6 flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1
          class="bg-gradient-to-r from-[#60a5fa] via-[#a78bfa] to-[#f472b6] bg-clip-text text-[28px] font-extrabold tracking-wide text-transparent drop-shadow-[0_2px_8px_rgba(96,165,250,0.3)]"
        >
          📊 检测中心数据看板
        </h1>
        <div class="mt-1 text-[13px] font-medium text-ink-muted">
          {{ isLimsYear ? 'LIMS（SQL Server）· 抽样/检测历史' : '达梦数据库 · DETECTION 模式' }} ·
          <span :title="sourceTitle">{{ sourceText || '实时统计' }}</span>
        </div>
      </div>
      <div class="flex flex-wrap items-center gap-2.5">
        <div class="status-pill" :class="statusType">{{ status }}</div>
        <select v-model="year" class="year-select" @change="loadAll">
          <option v-for="y in allYears" :key="y" :value="y">{{ y }} 年</option>
        </select>
        <button class="tech-btn" :disabled="refreshing" @click="loadAll(true)">
          <RefreshCw class="h-4 w-4" :class="{ 'animate-spin': refreshing }" />
          刷新数据
        </button>
      </div>
    </header>

    <!-- 数据源标签页：2025 年两个源都有数据，按数据源分开看，口径互不混淆 -->
    <div
      class="mb-6 inline-flex flex-wrap items-center gap-1 rounded-2xl border border-white/15 bg-white/10 p-1 backdrop-blur-xl"
    >
      <button
        v-for="t in TABS"
        :key="t.key"
        type="button"
        class="cursor-pointer rounded-xl px-4 py-1.5 text-sm font-semibold transition-all"
        :class="
          dataTab === t.key
            ? 'bg-gradient-to-r from-primary-light to-primary text-white shadow-[0_4px_14px_rgba(96,165,250,0.35)]'
            : 'text-ink-muted hover:text-ink'
        "
        @click="switchTab(t.key)"
      >
        {{ t.label }}
        <span class="ml-1 text-xs font-normal opacity-75">{{ t.hint }}</span>
      </button>
    </div>

    <template v-if="!isLimsYear">
    <!-- 顶部统计卡片 -->
    <div class="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard
        label="仪器设备（台）"
        :value="fmt(summary?.equipment)"
        gradient="linear-gradient(90deg, #60a5fa, #38bdf8)"
      />
      <StatCard
        :label="`样品总数（${summary?.year ?? year} 年）`"
        :value="fmt(summary?.sample_total)"
        gradient="linear-gradient(90deg, #a78bfa, #c084fc)"
      />
      <StatCard
        :label="`检测项次（${itemCount?.year ?? year} 年）`"
        :value="fmt(itemCount?.total)"
        gradient="linear-gradient(90deg, #fbbf24, #f59e0b)"
      />
      <StatCard
        :label="`统计月份数（${summary?.year ?? year} 年）`"
        :value="fmt(summary?.month_count)"
        gradient="linear-gradient(90deg, #34d399, #6ee7b7)"
      />
    </div>

    </template>

    <!-- ======= 2020–2024：LIMS 历史统计（该年份独立一页） ======= -->
    <template v-else>
    <!-- 2025 年是达梦/LIMS 并行年：LIMS 侧仅存部分数据，必须显式提示，避免被当成全年数据误读 -->
    <div
      v-if="year === 2025"
      class="mb-5 rounded-xl border border-warn/40 bg-warn/10 px-4 py-2.5 text-sm text-warn"
    >
      ⚠️ 2025 年为达梦平台与 LIMS 并行的切换年：此标签下的 <b>LIMS 数据仅存部分</b>
      （完整历史请看 2020–2024）；同一年按达梦口径的统计请切到「达梦平台」标签页。
    </div>
      <div v-if="limsDetail" class="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="样品数（按送检单）"
          :value="fmt(limsDetail.samples)"
          gradient="linear-gradient(90deg, #60a5fa, #38bdf8)"
        />
        <StatCard
          label="检测项次"
          :value="fmt(limsDetail.items)"
          gradient="linear-gradient(90deg, #a78bfa, #c084fc)"
        />
        <StatCard
          label="符合 / 不符合"
          :value="`${fmt(limsDetail.ok)} / ${fmt(limsDetail.bad)}`"
          gradient="linear-gradient(90deg, #34d399, #10b981)"
        />
        <StatCard
          label="符合率"
          :value="rate(limsDetail.ok, limsDetail.bad)"
          gradient="linear-gradient(90deg, #f472b6, #ec4899)"
        />
      </div>

      <div v-if="limsDetail" class="mb-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div class="glass-card p-5">
          <h3 class="panel-title">
            ⚠️ 高风险项目（超标）项次 Top10
            <span class="ml-auto text-xs font-medium text-ink-muted">{{ limsDetail.year }} 年</span>
          </h3>
          <div v-if="limsDetail.risk_items.length" class="grid grid-cols-2 gap-2 sm:grid-cols-5">
            <div
              v-for="it in limsDetail.risk_items"
              :key="it.name"
              class="rounded-lg border border-white/10 bg-white/10 px-3 py-2 text-xs"
            >
              <div class="font-semibold text-ink">{{ it.name }}</div>
              <div class="text-warn">{{ fmt(it.cnt) }} 项次</div>
            </div>
          </div>
          <p v-else class="text-xs text-ink-muted">该年无超标（不符合）项次</p>
          <p class="mt-3 text-xs text-ink-muted">
            口径：只统计判定为「不符合」（超标）的检测项次（TResult.Qualified），
            按检测项目汇总后取前 10 · 判定为未判定的不计入
          </p>
        </div>
        <div class="glass-card p-5">
          <h3 class="panel-title">
            🧩 样品类型分布
            <span class="ml-auto text-xs font-medium text-ink-muted">按样品名称归类 · 送检单去重</span>
          </h3>
          <div class="flex flex-wrap gap-2">
            <span
              v-for="k in limsDetail.by_kind"
              :key="k.name"
              class="rounded-full border border-white/10 bg-white/10 px-3 py-1 text-xs text-ink"
            >{{ k.name }}：{{ fmt(k.cnt) }}</span>
          </div>
          <p class="mt-3 text-xs text-ink-muted">
            口径：样品品类按样品名称归为 7 类（农产品 / 畜产品 / 水产品 / 土壤 / 水质 / 肥料 / 其他，
            与 SQL Server 页「样品类型」同口径）· 样品数按送检单（Submission_ID）去重 ·
            项次按检测日期（Date_Test）· 判定取 Qualified（未判定不计入）
          </p>
        </div>
      </div>

      <div class="glass-card p-5">
        <h3 class="panel-title">
          📅 历年对比
          <span class="ml-auto text-xs font-medium text-ink-muted">2020–2025 · LIMS</span>
        </h3>
        <!-- 跨年对比要扫 2020–2025 全部数据（约 2 秒），故按需加载：不点按钮不查，避免拖慢页面 -->
        <div v-if="!limsOverview" class="flex flex-col items-start gap-2 py-1">
          <p class="text-xs text-ink-muted">
            跨年对比需要扫描 2020–2025 全部年份数据（约 2 秒），为不影响页面加载速度，改为按需查询。
          </p>
          <button class="tech-btn" :disabled="limsOverviewLoading" @click="loadLimsOverview">
            {{ limsOverviewLoading ? '加载中…' : '加载历年对比' }}
          </button>
          <p v-if="limsOverviewError" class="text-xs text-err">{{ limsOverviewError }}</p>
        </div>
        <div v-else class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="text-left text-ink-muted">
                <th class="px-2 py-1">年度</th>
                <th class="px-2 py-1">样品数</th>
                <th class="px-2 py-1">检测项次</th>
                <th class="px-2 py-1">符合</th>
                <th class="px-2 py-1">不符合</th>
                <th class="px-2 py-1">符合率</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(yy, i) in limsOverview.years"
                :key="yy"
                class="border-t border-white/10"
                :class="yy === year ? 'bg-white/10 font-semibold' : ''"
              >
                <td class="px-2 py-1">{{ yy }} 年</td>
                <td class="px-2 py-1">{{ fmt(limsOverview.samples[i]) }}</td>
                <td class="px-2 py-1">{{ fmt(limsOverview.items[i]) }}</td>
                <td class="px-2 py-1">{{ fmt(limsOverview.ok[i]) }}</td>
                <td class="px-2 py-1">{{ fmt(limsOverview.bad[i]) }}</td>
                <td class="px-2 py-1">{{ rate(limsOverview.ok[i], limsOverview.bad[i]) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="mt-2 text-xs text-ink-muted">
          ⚠️ 2025 年 LIMS 数据因系统切换仅存部分，2025/2026 年请切换到对应年份查看达梦统计
        </p>
      </div>
    </template>

    <!-- 全局错误提示 -->
    <div
      v-if="Object.keys(errors).length"
      class="mb-5 rounded-xl border border-err/40 bg-err/10 px-4 py-2.5 text-sm text-err"
    >
      {{ Object.values(errors).join('；') }}
    </div>

    <!-- 主图表面板（**仅达梦年份 2025/2026**）
         2020–2024 只有 LIMS（SQL Server）数据、没有对应的达梦统计：
         这些图若不隐藏，会拿不到数据、或残留上一个达梦年份的旧数据（标题却写着当前年份）。 -->
    <template v-if="!isLimsYear">
    <div class="grid grid-cols-1 gap-5 lg:grid-cols-2">
      <!-- 每月检测样品数量 -->
      <div class="glass-card p-5">
        <h3 class="panel-title">
          📅 每月检测样品数量
          <span class="ml-auto text-xs font-medium text-ink-muted">{{ year }} 年</span>
        </h3>
        <div v-if="monthly" class="h-[300px]">
          <LineChart :labels="monthly.labels" :values="monthly.values" />
        </div>
        <div v-else-if="errors.monthly" class="empty-tip">{{ errors.monthly }}</div>
      </div>

      <!-- 样品产品类型 -->
      <div class="glass-card p-5">
        <h3 class="panel-title">
          🥩 样品产品类型
          <span class="ml-auto text-xs font-medium text-ink-muted">{{ year }} 年</span>
        </h3>
        <div v-if="product" class="h-[300px]">
          <BarChart :labels="product.labels" :values="product.values" />
        </div>
        <div v-else-if="errors.product" class="empty-tip">{{ errors.product }}</div>
      </div>

      <!-- 月度样品数量（堆叠） -->
      <div class="glass-card p-5 lg:col-span-2">
        <h3 class="panel-title">
          📊 月度样品数量
          <span class="ml-auto text-xs font-medium text-ink-muted">
            {{ year }} 年 · 合计 {{ fmt(completion?.total) }} 批次 · 农/畜/水/其他
          </span>
        </h3>
        <div v-if="completion" class="h-[300px]">
          <StackedBarChart :labels="completion.labels" :series="completion.series" />
        </div>
        <div v-else-if="errors.completion" class="empty-tip">{{ errors.completion }}</div>
      </div>

      <!-- 月度检测项次（堆叠） -->
      <div class="glass-card p-5 lg:col-span-2">
        <h3 class="panel-title">
          🔬 月度检测项次
          <span class="ml-auto text-xs font-medium text-ink-muted">
            {{ year }} 年 · 合计 {{ fmt(itemCount?.total) }} 项次 · 农/畜/水/其他
          </span>
        </h3>
        <div v-if="itemCount" class="h-[300px]">
          <StackedBarChart :labels="itemCount.labels" :series="itemCount.series" />
        </div>
        <div v-else-if="errors.itemCount" class="empty-tip">{{ errors.itemCount }}</div>
      </div>

      <!-- 检测项次·产品类型 -->
      <div class="glass-card p-5 lg:col-span-2">
        <h3 class="panel-title">
          🧪 检测项次·产品类型
          <span class="ml-auto text-xs font-medium text-ink-muted">
            {{ year }} 年 · 同一检测编号内项目名去重
          </span>
        </h3>
        <div v-if="itemCount" class="h-[300px]">
          <BarChart
            :labels="itemCount.categories.labels"
            :values="itemCount.categories.values"
            :colors="['#34d399', '#a78bfa', '#06b6d4', '#fbbf24']"
          />
        </div>
        <div v-else-if="errors.itemCount" class="empty-tip">{{ errors.itemCount }}</div>
      </div>

      <!-- 月度检出率曲线 -->
      <div class="glass-card p-5 lg:col-span-2">
        <h3 class="panel-title">
          📈 月度检出率曲线
          <span class="ml-auto text-xs font-medium text-ink-muted">农产品 / 畜产品 / 水产品</span>
        </h3>
        <div v-if="detectRate" class="h-[300px]">
          <MultiLineChart :labels="detectRate.labels" :series="detectRateSeries()" />
        </div>
        <div v-else-if="errors.detectRate" class="empty-tip">{{ errors.detectRate }}</div>
      </div>
    </div>

    <!-- 高频检出 / 风险统计 -->
    <div class="mt-5 grid grid-cols-1 gap-5 lg:grid-cols-3">
      <div
        v-for="(cat, ci) in ['农产品', '畜产品', '水产品']"
        :key="cat"
        class="glass-card p-5"
        :class="[
          'border-t-[3px]',
          ci === 0 ? 'border-t-[#22c55e]' : '',
          ci === 1 ? 'border-t-[#a855f7]' : '',
          ci === 2 ? 'border-t-[#06b6d4]' : '',
        ]"
      >
        <div class="mb-3 border-b border-white/15 pb-2.5 text-xl font-bold text-white">
          {{ cat }}
        </div>
        <div class="mt-3 mb-2 text-sm font-bold tracking-wide text-warn">高频检出项目</div>
        <TopList :items="top?.categories[cat]?.projects || []" empty-text="暂无检出" />
        <div class="mt-4 mb-2 text-sm font-bold tracking-wide text-warn">高频检出样品</div>
        <TopList :items="top?.categories[cat]?.samples || []" empty-text="暂无检出" />
      </div>
    </div>

    <!-- 风险项目及风险样品 -->
    <div class="mt-5 grid grid-cols-1 gap-5 lg:grid-cols-3">
      <div
        v-for="(cat, ci) in ['农产品', '畜产品', '水产品']"
        :key="cat"
        class="glass-card p-5"
        :class="[
          'border-t-[3px]',
          ci === 0 ? 'border-t-[#ef4444]' : '',
          ci === 1 ? 'border-t-[#f59e0b]' : '',
          ci === 2 ? 'border-t-[#0ea5e9]' : '',
        ]"
      >
        <div class="mb-3 border-b border-white/15 pb-2.5 text-xl font-bold text-white">
          {{ cat }}
        </div>
        <div class="mt-3 mb-2 text-sm font-bold tracking-wide text-warn">风险项目（超标）</div>
        <TopList :items="risk?.categories[cat]?.projects || []" empty-text="暂无超标" />
        <div class="mt-4 mb-2 text-sm font-bold tracking-wide text-warn">风险样品（超标）</div>
        <TopList :items="risk?.categories[cat]?.samples || []" empty-text="暂无超标" />
      </div>
    </div>

    <!-- 雷达图：苏州不合格率分布 + 产品不合格率 -->
    <div class="mt-5 grid grid-cols-1 gap-5 lg:grid-cols-2">
      <div class="glass-card border-t-[3px] border-t-[#f472b6] p-5">
        <h3 class="panel-title">
          🗺️ 苏州市不合格率分布
          <span class="ml-auto text-xs font-medium text-ink-muted">按受检单位 COUNTY 归类</span>
        </h3>
        <div v-if="region" class="h-[380px]">
          <RadarChart
            :labels="region.labels"
            :values="region.values"
            :totals="region.totals"
            :fails="region.fails"
            color="#f472b6"
            fill-color="rgba(244,114,182,0.28)"
            :max-value="3"
          />
        </div>
        <div v-else-if="errors.region" class="empty-tip">{{ errors.region }}</div>
      </div>

      <div class="glass-card border-t-[3px] border-t-[#38bdf8] p-5">
        <h3 class="panel-title">
          🥧 不合格率·产品分布
          <span class="ml-auto text-xs font-medium text-ink-muted">农/畜/水三类</span>
        </h3>
        <div v-if="productRisk" class="h-[380px]">
          <RadarChart
            :labels="productRisk.labels"
            :values="productRisk.values"
            :totals="productRisk.totals"
            :fails="productRisk.fails"
            color="#38bdf8"
            fill-color="rgba(56,189,248,0.22)"
            :max-value="2"
          />
        </div>
        <div v-else-if="errors.productRisk" class="empty-tip">{{ errors.productRisk }}</div>
      </div>
    </div>
    </template>

  </div>
</template>
