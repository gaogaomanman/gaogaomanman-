<script setup lang="ts">
/**
 * 导航首页（移植自旧 AI工具合集/index.html）。
 *
 * UI 保真：DOM 结构、class 名、CSS 声明与原文一致（仅给选择器加 `.v-nav` 前缀以做样式隔离）。
 *
 * 与原实现的必要差异（因架构变化，均为"地址/文案"层面）：
 * 1. 状态来源：旧版由前端每 5 秒跨端口探测各服务首页；新版改为后端聚合接口 `/api/nav/status`
 *    （同源单服务，不再需要跨端口与 CORS）。
 * 2. 卡片右上角徽标：旧版显示端口（:3001 等），新版显示路径（/dm 等），否则文案错误。
 * 3. 数据源状态卡：旧版显示「中间池」与 `http://host:4000`；新版后端已直连达梦与 SQL Server，
 *    故文案改为「数据源」并列出两个库。
 * 4. 点击卡片仍为「新标签页打开」，与原体验一致。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { mirrorApi, navApi, type MirrorStatus, type NavModuleStatus, type NavStatus } from '../api/client'
import { isUnderConstruction } from '../features'

interface ToolCard {
  key: string
  path: string
  icon: string
  iconClass: string
  title: string
  desc: string
}

const TOOLS: ToolCard[] = [
  {
    key: 'dm',
    path: '/dm',
    icon: '🗄',
    iconClass: 'c-dm',
    title: 'LIMS统计查询',
    desc: '连接达梦 DETECTION 库，支持模板化 SQL 查询、表结构浏览',
  },
  {
    key: 'sqlserver',
    path: '/sqlserver',
    icon: '💾',
    iconClass: 'c-sql',
    title: '旧LIMS查询',
    desc: '连接 SQL Server Elims 库，数据库/表/字段浏览与数据预览',
  },
  {
    key: 'board',
    path: '/board',
    icon: '📊',
    iconClass: 'c-board',
    title: '数据看板',
    desc: '检测中心数据看板（仪器 / 样品 / 检测项次 / 检出率 / 风险等可视化）',
  },
  {
    key: 'personnel',
    path: '/personnel',
    icon: '👤',
    iconClass: 'c-people',
    title: '人员能力表梳理',
    desc: '跨达梦 + SQLServer 双库，按人员/项目/标准交叉查询',
  },
  {
    key: 'cma',
    path: '/cma',
    icon: '🔍',
    iconClass: 'c-cma',
    title: '一单一库核对（CMA）',
    desc: '检测标准资质核对，批量核对外部 CMA 标准库',
  },
  {
    key: 'progress',
    path: '/progress',
    icon: '📈',
    iconClass: 'c-progress',
    title: '抽采样进度统计',
    desc: '按合同/区域/产品类型对照任务量与完成量，动态表头 + 完成率看板',
  },
]

const status = ref<NavStatus | null>(null)

const moduleMap = computed<Record<string, NavModuleStatus>>(() => {
  const map: Record<string, NavModuleStatus> = {}
  for (const m of status.value?.modules ?? []) map[m.key] = m
  return map
})

const poolClass = computed(() => {
  if (!status.value) return 'dot on'
  return status.value.pool?.ok ? 'dot on' : 'dot off'
})

const poolMeta = computed(() => {
  if (!status.value) return '检测中…'
  return status.value.pool?.ok ? '镜像就绪 · 运行正常' : '镜像未就绪'
})

const poolAddr = computed(() => {
  const m = status.value?.mirror
  return m && m.mode === 'direct' ? '源库直连' : '本地 SQLite 镜像'
})

/* 数据来源：镜像模式必须把「数据截止时间」摆出来——
   否则使用者会以为看到的是实时数据，这比慢一点危险得多。 */
function _fmtDeadline(t: string | null | undefined): string {
  if (!t) return ''
  return t.replace('T', ' ')
}

const sourceMeta = computed(() => {
  const m = status.value?.mirror
  if (!m) return '数据来源：检测中…'
  if (m.mode === 'mirror') {
    const t = _fmtDeadline(m.data_deadline)
    return `数据来源：镜像快照${t ? ` · 截止 ${t}` : '（尚未同步）'}`
  }
  return `数据来源：直连源库（实时）${m.fallback ? ` · ${m.fallback}` : ''}`
})

const sourceTitle = computed(() => {
  const m = status.value?.mirror
  if (!m) return ''
  return m.mode === 'mirror'
    ? '查询走本地 SQLite 镜像：毫秒级返回，源库暂时不可达时页面仍可用'
    : '查询直接走源库：数据实时，但受源库可达性与驱动安装影响'
})

function dotClass(m: ToolCard): string {
  const s = moduleMap.value[m.key]
  if (!s) return 'st-dot'
  return s.online ? 'st-dot on' : 'st-dot off'
}

function txtClass(m: ToolCard): string {
  const s = moduleMap.value[m.key]
  if (!s) return 'st-text'
  return s.online ? 'st-text on' : 'st-text off'
}

function statusText(m: ToolCard): string {
  if (ucTool(m)) return '建设中'
  const s = moduleMap.value[m.key]
  if (!s) return '检测中'
  if (s.online) return s.standalone ? '独立工具' : '在线'
  return s.standalone ? '未启动' : '离线'
}

/** 该工具是否还在建设（产品构建 + 未发布）：卡片上打「建设中」标，点进去是占位页 */
function ucTool(m: ToolCard): boolean {
  return isUnderConstruction(m.key)
}

function openTool(m: ToolCard): void {
  window.open(m.path, '_blank')
}

/* 镜像同步：显示数据截止时间 + 手动触发；随导航状态一起每 5 秒轮询 */
const mirrorSyncing = ref(false)
const mirrorNote = ref('')

async function refreshMirror(): Promise<void> {
  const s: MirrorStatus | null = await mirrorApi.status().catch(() => null)
  if (!s) return
  mirrorSyncing.value = !!s.sync?.syncing
  const t = (s.data_deadline || '').replace('T', ' ')
  // 同步耗时以最近一次实测为准（原地更新实测约 4 分钟），不再写死
  const lastSec = (s.sync?.last as { elapsed_sec?: number } | null)?.elapsed_sec
  const lastTxt = lastSec ? `上次用时 ${(lastSec / 60).toFixed(1)} 分钟` : '约 2~4 分钟'
  if (s.sync?.syncing) {
    mirrorNote.value = `⏳ 正在同步镜像…（${lastTxt}）`
  } else if (s.sync?.last_error) {
    mirrorNote.value = `⚠️ 上次同步失败：${s.sync.last_error}`
  } else {
    mirrorNote.value =
      s.mode === 'mirror' ? (t ? `镜像数据 · 截止 ${t}` : '镜像数据（尚未同步）') : '直连源库模式'
  }
}

async function triggerSync(): Promise<void> {
  if (mirrorSyncing.value) return
  try {
    const r = await mirrorApi.sync()
    if (!r.success) {
      mirrorNote.value = r.message || '同步未开始'
      return
    }
    mirrorNote.value = '⏳ 同步已开始（后台执行，约 2~4 分钟）…'
    mirrorSyncing.value = true
  } catch (e) {
    mirrorNote.value = `同步触发失败：${(e as Error).message}`
  }
}

let timer: number | undefined

async function refresh(): Promise<void> {
  void refreshMirror()
  try {
    status.value = await navApi.status()
  } catch {
    status.value = {
      success: false,
      pool: { ok: false },
      data_sources: { dm: { ok: false, error: 'unreachable' }, sql: { ok: false, error: 'unreachable' } },
      modules: [],
    }
  }
}

onMounted(() => {
  refresh()
  timer = window.setInterval(refresh, 5000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="v-nav">
    <div class="container">
      <header>
        <h1>🛠 AI 工具合集</h1>
        <p>统一入口 · 数据库与检测工具导航</p>
      </header>

      <!-- 数据源状态 -->
      <div class="pool-status">
        <div class="info">
          <div :class="poolClass"></div>
          <div>
            <div class="label">数据服务状态</div>
            <div class="meta">{{ poolMeta }}</div>
            <div class="meta" :title="sourceTitle">{{ sourceMeta }}</div>
          </div>
        </div>
        <div class="actions">
          <span class="addr">{{ poolAddr }}</span>
          <span class="mirror-note" :title="sourceMeta">{{ mirrorNote }}</span>
          <button class="sync-btn" :disabled="mirrorSyncing" @click="triggerSync">
            {{ mirrorSyncing ? '⏳ 同步中…' : '⟳ 同步镜像' }}
          </button>
        </div>
      </div>

      <!-- 工具卡片 -->
      <div class="grid">
        <div v-for="m in TOOLS" :key="m.key" class="card" @click="openTool(m)">
          <div class="status-line">
            <span :class="dotClass(m)"></span>
            <span :class="txtClass(m)">{{ statusText(m) }}</span>
            <span v-if="ucTool(m)" class="build-badge">建设中</span>
          </div>
          <div class="icon" :class="m.iconClass">{{ m.icon }}</div>
          <div class="title">{{ m.title }}</div>
          <div class="desc">{{ m.desc }}</div>
          <span class="port">{{ m.path }}</span>
        </div>
      </div>

      <footer>AI工具合集 · 统一 FastAPI + Vue3 工程 · 2026-09-17</footer>
    </div>
  </div>
</template>

<style>
/* 原样式完整保留，仅统一加 `.v-nav` 前缀做视图隔离 */
.v-nav,
.v-nav * { margin: 0; padding: 0; box-sizing: border-box; }
.v-nav {
  font-family: -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif;
  background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
  min-height: 100vh;
  color: #e2e8f0;
  padding: 40px 20px 60px;
}
.v-nav .container { max-width: 1080px; margin: 0 auto; }

/* 头部 */
.v-nav header { text-align: center; margin-bottom: 40px; }
.v-nav header h1 {
  font-size: 34px; font-weight: 700;
  background: linear-gradient(90deg, #38bdf8, #818cf8, #c084fc);
  -webkit-background-clip: text; background-clip: text; color: transparent;
  letter-spacing: 2px;
}
.v-nav header p { color: #94a3b8; margin-top: 10px; font-size: 15px; }

/* 镜像同步按钮与提示 */
.v-nav .mirror-note { font-size: 12px; color: #94a3b8; }
.v-nav .sync-btn {
  background: rgba(255,255,255,0.12);
  border: 1px solid rgba(255,255,255,0.22);
  color: #e2e8f0;
  font-size: 13px;
  padding: 6px 14px;
  border-radius: 8px;
  cursor: pointer;
  transition: background .15s;
  white-space: nowrap;
}
.v-nav .sync-btn:hover:not(:disabled) { background: rgba(255,255,255,0.2); }
.v-nav .sync-btn:disabled { opacity: .6; cursor: default; }

/* 数据源状态 */
.v-nav .pool-status {
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: 16px;
  padding: 20px 24px;
  display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 12px;
  margin-bottom: 32px;
  backdrop-filter: blur(8px);
}
.v-nav .pool-status .info { display: flex; align-items: center; gap: 12px; }
.v-nav .dot {
  width: 12px; height: 12px; border-radius: 50%;
  background: #f59e0b; box-shadow: 0 0 10px currentColor;
  flex-shrink: 0;
}
.v-nav .dot.on { background: #22c55e; color: #22c55e; }
.v-nav .dot.off { background: #ef4444; color: #ef4444; }
.v-nav .pool-status .label { font-weight: 600; font-size: 15px; }
.v-nav .pool-status .meta { color: #94a3b8; font-size: 13px; margin-top: 3px; }
.v-nav .pool-status .actions { display: flex; gap: 10px; }
.v-nav .pool-status .addr {
  font-family: Consolas, monospace; background: rgba(0,0,0,0.3);
  padding: 6px 12px; border-radius: 8px; font-size: 13px; color: #7dd3fc;
}

/* 工具网格 */
.v-nav .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 18px; }
.v-nav .card {
  position: relative;
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: 16px;
  padding: 22px;
  transition: transform .2s, border-color .2s, box-shadow .2s;
  cursor: pointer;
  overflow: hidden;
}
.v-nav .card:hover {
  transform: translateY(-4px);
  border-color: #38bdf8;
  box-shadow: 0 12px 30px rgba(56,189,248,0.15);
}
.v-nav .card .icon {
  width: 46px; height: 46px; border-radius: 12px;
  display: flex; align-items: center; justify-content: center;
  font-size: 22px; margin-bottom: 14px;
}
.v-nav .card .title { font-size: 17px; font-weight: 600; margin-bottom: 6px; }
.v-nav .card .desc { font-size: 13px; color: #94a3b8; line-height: 1.6; margin-bottom: 14px; }
.v-nav .card .port {
  display: inline-block; font-family: Consolas, monospace;
  font-size: 12px; color: #7dd3fc; background: rgba(56,189,248,0.12);
  padding: 3px 10px; border-radius: 20px;
}
.v-nav .card .status-line { position: absolute; top: 22px; right: 22px; display: flex; align-items: center; gap: 6px; font-size: 12px; }
.v-nav .card .st-dot { width: 8px; height: 8px; border-radius: 50%; background: #475569; }
.v-nav .card .st-dot.on { background: #22c55e; }
.v-nav .card .st-dot.off { background: #ef4444; }
.v-nav .card .st-text { color: #64748b; }
.v-nav .card .st-text.on { color: #22c55e; }
.v-nav .card .st-text.off { color: #ef4444; }

/* 颜色 */
.v-nav .c-dm { background: linear-gradient(135deg,#3b82f6,#2563eb); }
.v-nav .c-sql { background: linear-gradient(135deg,#0ea5e9,#0284c7); }
.v-nav .c-board { background: linear-gradient(135deg,#8b5cf6,#6d28d9); }
.v-nav .c-people { background: linear-gradient(135deg,#ec4899,#db2777); }
.v-nav .c-cma { background: linear-gradient(135deg,#f59e0b,#d97706); }
.v-nav .c-progress { background: linear-gradient(135deg,#10b981,#0ea5e9); }

/* 「建设中」标：与卡片右上角的状态点同行，颜色沿用警示色，避免与"在线"混淆 */
.v-nav .build-badge {
  margin-left: 6px;
  padding: 1px 7px;
  border-radius: 6px;
  background: rgba(251,191,36,0.22);
  color: #fbbf24;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.v-nav footer { text-align: center; color: #64748b; font-size: 12px; margin-top: 40px; }
</style>
