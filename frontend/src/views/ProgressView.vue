<script setup lang="ts">
/**
 * 抽采样进度统计（/progress）模块外壳。
 *
 * 只做外壳与共享状态：**任务（考核表）** + 年份 + 页签切换与同步动作；
 * 具体渲染交给 `views/progress/*` 各面板。
 *
 * 「任务」是第一层维度：使用方的考核表是按任务下达的（市例行 / 市监督 各一张），
 * 每个任务有自己的一套表头列，且**逐年可调整**——所以所有面板都带 `taskTypeId`，
 * 切换任务时整张表（表头列、任务量、完成量、图表）随之切换。
 *
 * 使用方明确不要「合同 / 业务类别 / 截止日期」筛选（2026-09-21），顶部只留任务切换与年份；
 * 合同仍存在的两个地方：①样品按合同归属到任务（「合同管理」维护）；②同步日志里看合同数。
 *
 * 数据来源必须对使用者可见：完成量取自**本地 SQLite 镜像快照**（每天 19:00 同步源库），
 * 所以顶部同时显示「镜像数据截止时间」与「完成量重算时间」——不显示会被误认为实时数据。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  AlertTriangle, BarChart3, ClipboardList, FileSpreadsheet, Layers, MapPin, RefreshCw,
} from 'lucide-vue-next'
import { progressApi, type ProgressOverview, type ProgressTaskType } from '../api/client'
import { isUnderConstruction } from '../features'
import UnderConstruction from './progress/UnderConstruction.vue'
import DashboardPanel from './progress/DashboardPanel.vue'
import QuotaPanel from './progress/QuotaPanel.vue'
import CategoryPanel from './progress/CategoryPanel.vue'
import RegionPanel from './progress/RegionPanel.vue'
import ContractPanel from './progress/ContractPanel.vue'
import UnmatchedPanel from './progress/UnmatchedPanel.vue'

type TabKey = 'dashboard' | 'quota' | 'category' | 'region' | 'contract' | 'unmatched'

const TABS: { key: TabKey; label: string; icon: typeof BarChart3 }[] = [
  { key: 'dashboard', label: '数据看板', icon: BarChart3 },
  { key: 'quota', label: '任务量录入', icon: ClipboardList },
  { key: 'category', label: '品类配置', icon: Layers },
  { key: 'region', label: '区域配置', icon: MapPin },
  { key: 'contract', label: '合同管理', icon: FileSpreadsheet },
  { key: 'unmatched', label: '未归类处理', icon: AlertTriangle },
]

const route = useRoute()
const router = useRouter()

/** 产品构建里若开关为关 → 只渲染「建设中」占位页（调试环境始终渲染真实模块） */
const uc = isUnderConstruction('progress')

const tab = ref<TabKey>('dashboard')
const year = ref(new Date().getFullYear())
const years = ref<number[]>([])
/** 任务（考核表）：市例行 / 市监督…；所有面板都按它渲染各自的列方案 */
const tasks = ref<ProgressTaskType[]>([])
const taskTypeId = ref(0)

const data = ref<ProgressOverview | null>(null)
const loading = ref(false)
const syncing = ref(false)
const error = ref('')
const notice = ref('')

const currentTask = computed(() => tasks.value.find((t) => t.id === taskTypeId.value))
const deadline = computed(() => (data.value?.sync.data_deadline || '').replace('T', ' '))
const syncedAt = computed(() => (data.value?.sync.synced_at || '').replace('T', ' '))
const hasError = computed(() => !!data.value?.sync.last_error)

async function loadYears(): Promise<void> {
  try {
    const r = await progressApi.years()
    years.value = r.years
    if (!years.value.includes(year.value)) years.value = [year.value, ...years.value]
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function loadTasks(): Promise<void> {
  try {
    const r = await progressApi.taskTypes()
    const enabled = r.task_types.filter((t) => t.enabled)
    tasks.value = enabled.length ? enabled : r.task_types
    if (!taskTypeId.value && tasks.value.length) taskTypeId.value = tasks.value[0].id
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function load(): Promise<void> {
  if (!taskTypeId.value) return
  loading.value = true
  error.value = ''
  try {
    data.value = await progressApi.overview(year.value, taskTypeId.value)
  } catch (e) {
    error.value = (e as Error).message
    data.value = null
  } finally {
    loading.value = false
  }
}

async function syncNow(): Promise<void> {
  if (syncing.value) return
  syncing.value = true
  notice.value = ''
  try {
    const r = await progressApi.sync(year.value)
    notice.value = r.message || '完成量同步完成'
    await load()
  } catch (e) {
    notice.value = (e as Error).message
  } finally {
    syncing.value = false
  }
}

function onChanged(msg: string): void {
  notice.value = msg
  void load()
}

function onGoto(key: string): void {
  if (TABS.some((t) => t.key === key)) tab.value = key as TabKey
}

/* ---- 状态写入 URL query：刷新与分享链接不丢上下文 ---- */
function syncQuery(): void {
  const q: Record<string, string> = { year: String(year.value), tab: tab.value }
  if (taskTypeId.value) q.task = String(taskTypeId.value)
  void router.replace({ query: q })
}

watch([year, taskTypeId], () => {
  syncQuery()
  void load()
})
watch(tab, syncQuery)

onMounted(async () => {
  if (uc) return // 建设中：不请求任何接口，避免占用镜像/进度库
  const q = route.query
  if (q.year) year.value = Number(q.year) || year.value
  if (typeof q.tab === 'string' && TABS.some((t) => t.key === q.tab)) tab.value = q.tab as TabKey
  await loadYears()
  await loadTasks()
  if (typeof q.task === 'string' && Number(q.task)) taskTypeId.value = Number(q.task)
  await load()
})
</script>

<template>
  <!-- 产品构建 + 未发布 → 只显示「建设中」；调试环境渲染下面的真实模块 -->
  <UnderConstruction v-if="uc" />

  <div v-else class="min-h-screen px-5 pb-16 pt-7">
    <div class="mx-auto max-w-[1680px] animate-fade-in-up">
      <header class="glass-card mb-5 flex flex-wrap items-end justify-between gap-4 px-6 py-5">
        <div>
          <h1
            class="bg-gradient-to-r from-[#60A5FA] via-[#A78BFA] to-[#F472B6] bg-clip-text text-[28px] font-extrabold tracking-wide text-transparent"
          >
            抽采样进度统计
          </h1>
          <p class="mt-1.5 text-[13px] text-ink-muted">
            按「任务（考核表）」对照任务量与完成量 · 行 = 县区市 · 完成量取自达梦镜像快照
          </p>
        </div>

        <div class="flex flex-wrap items-end gap-4">
          <!-- 考核表（任务）切换：切换后下方所有页签的表格 / 录入 / 配置都跟着换 -->
          <div class="flex flex-col gap-1">
            <span class="text-[11px] text-ink-muted">考核表</span>
            <div class="flex items-center gap-1.5">
              <button
                v-for="t in tasks"
                :key="t.id"
                class="cursor-pointer rounded-xl px-4 py-2 text-sm font-bold transition-all"
                :class="
                  taskTypeId === t.id
                    ? 'bg-gradient-to-r from-primary-light/90 to-primary/90 text-white shadow-[0_4px_14px_rgba(96,165,250,0.35)]'
                    : 'bg-white/5 text-ink-muted hover:bg-white/10 hover:text-ink'
                "
                @click="taskTypeId = t.id"
              >
                {{ t.name }}
              </button>
            </div>
          </div>

          <label class="flex flex-col gap-1">
            <span class="text-[11px] text-ink-muted">年份</span>
            <select v-model.number="year" class="year-select" title="按抽样日期归属年份">
              <option v-for="y in years" :key="y" :value="y">{{ y }} 年</option>
            </select>
          </label>

          <span
            class="status-pill"
            :class="hasError ? 'error' : 'connected'"
            :title="`完成量来源：本地 SQLite 镜像快照（每天 19:00 同步源库）\n镜像数据截止：${deadline || '尚未同步'}\n完成量重算：${syncedAt || '尚未重算'}`"
          >
            镜像截止 {{ deadline || '尚未同步' }}
          </span>

          <button class="tech-btn" :disabled="syncing" @click="syncNow">
            <RefreshCw :size="15" :class="syncing ? 'animate-spin' : ''" />
            {{ syncing ? '同步中…' : '同步完成量' }}
          </button>
        </div>
      </header>

      <!-- 当前考核表的概要（列数 / 任务量 / 完成量）；方案为空时提醒去配置 -->
      <div v-if="data" class="glass-card mb-4 flex flex-wrap items-center gap-2 px-4 py-2.5">
        <span class="text-[13px] font-bold text-ink">当前考核表：「{{ currentTask?.name }}」{{ year }} 年</span>
        <span class="text-[12px] text-ink-muted">
          {{ data.scheme.category_count }} 列 · 任务量 {{ data.summary.quota_total }} · 完成量
          {{ data.summary.done_total }} · 涉及合同 {{ data.summary.contract_count }} 份
        </span>
        <span
          v-if="data.summary.scheme_empty"
          class="ml-1 rounded-full bg-warn/20 px-2 py-0.5 text-[11px] text-warn"
        >
          本年还没有列方案：请到「品类配置」复制上一年的列
        </span>
      </div>

      <nav class="glass-card mb-5 flex flex-wrap items-center gap-2 px-3 py-2.5">
        <button
          v-for="t in TABS"
          :key="t.key"
          class="inline-flex cursor-pointer items-center gap-2 rounded-xl px-4 py-2 text-sm font-bold transition-all"
          :class="
            tab === t.key
              ? 'bg-gradient-to-r from-primary-light/90 to-primary/90 text-white shadow-[0_4px_14px_rgba(96,165,250,0.35)]'
              : 'text-ink-muted hover:bg-white/10 hover:text-ink'
          "
          @click="tab = t.key"
        >
          <component :is="t.icon" :size="15" />
          {{ t.label }}
          <span
            v-if="t.key === 'unmatched' && data && data.summary.done_unmatched > 0"
            class="rounded-full bg-warn/25 px-1.5 text-[11px] text-warn"
          >
            {{ data.summary.done_unmatched }}
          </span>
        </button>
        <span v-if="syncedAt" class="ml-auto pr-2 text-[12px] text-ink-muted">完成量重算于 {{ syncedAt }}</span>
      </nav>

      <div v-if="error" class="glass-card mb-4 border-err/40 bg-err/10 px-5 py-3 text-sm text-err">
        取数失败：{{ error }}
      </div>
      <div v-if="notice" class="glass-card mb-4 border-ok/40 bg-ok/10 px-5 py-3 text-sm text-ok">
        {{ notice }}
      </div>

      <div v-if="loading && !data" class="glass-card px-6 py-16 text-center">
        <div class="loader" />
        <p class="text-sm text-ink-muted">正在读取进度数据…</p>
      </div>

      <template v-else-if="data">
        <DashboardPanel v-show="tab === 'dashboard'" :data="data" @goto="onGoto" @refresh="load" />
        <QuotaPanel
          v-if="tab === 'quota'"
          :year="year"
          :task-type-id="taskTypeId"
          :tasks="tasks"
          @changed="onChanged"
        />
        <CategoryPanel
          v-if="tab === 'category'"
          :year="year"
          :task-type-id="taskTypeId"
          :tasks="tasks"
          @changed="onChanged"
        />
        <RegionPanel v-if="tab === 'region'" :year="year" @changed="onChanged" />
        <ContractPanel
          v-if="tab === 'contract'"
          :year="year"
          :tasks="tasks"
          @changed="onChanged"
        />
        <UnmatchedPanel
          v-if="tab === 'unmatched'"
          :year="year"
          :task-type-id="taskTypeId"
          @changed="onChanged"
        />
      </template>
    </div>
  </div>
</template>
