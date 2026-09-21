<script setup lang="ts">
/**
 * 达梦数据库查询工具（移植自 20260605160640/sql-query.html，原 :3001）。
 *
 * UI 保真：header / 连接卡 / 11 个功能块的 DOM 结构、class、内联样式与文案逐字保留
 * （样式统一加 `.v-dm` 前缀做视图隔离；body 规则改挂根容器）。
 *
 * JS 迁移：命令式 DOM 操作 → 响应式状态；内联 onclick → @click；
 * 结果区（innerHTML）→ `BlockResult` 组件（可点击芯片替代内联 onclick）；
 * 接口路径 /api/query → /api/dm/query、/api/auto-connect → /api/dm/auto-connect 等。
 *
 * 迁移进度：**11 个功能块已全部迁移**（逐块迁移、逐块以真实数据验收）。
 * 迁移时刻意保留的差异（均为"看着能合并、实际不能"，已逐处核对）：
 *   - `parseCityCounty` 在原文件中有 5 份副本 → 按原样逐份保留，未合并；
 *   - `getBigCategory` 存在两套规则 → 两套都保留，未合并；
 *   - `formatSigNum` / `notDetectedText` / `formatSampleProjects` / `formatDetectDrugs`
 *     各有多个版本 → 随块保留；
 *   - 硬编码列（73 列任务统计 / 38 列月报 / 33 列省例行-农产品）逐列核对顺序后照搬。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { dmApi, navApi } from '../api/client'
import BlockResult from './dm/BlockResult.vue'
import { noTypeLabel, parseNoList, resolveNoType, NEW_KIND_NAMES } from './dm/helpers'
import type { DmOutcome } from './dm/outcome'
import { queryByTaskNo } from './dm/blocks/taskNo'
import { queryByDetectionNo, queryByContractNo } from './dm/blocks/detectionNo'
import { queryJiangsuTaskNo, type JiangsuVariant } from './dm/blocks/jiangsu'
import { queryMonitorSummaryTable } from './dm/blocks/monitorSummary'
import { queryTaskStats } from './dm/blocks/taskStats'
import { queryMonthlyReport } from './dm/blocks/monthlyReport'
import { queryProvinceRoutineAgri } from './dm/blocks/provinceAgri'
import { queryProvinceRoutineLivestock } from './dm/blocks/provinceLivestock'
import { queryProvinceRoutineAquatic } from './dm/blocks/provinceAquatic'
import { queryNewTemplate } from './dm/blocks/yearlyStats'

/* ==================== 数据来源（本地 SQLite 镜像） ==================== */
/* 数据统一来自镜像：原「数据库连接」卡与 5 分钟空闲倒计时随直连架构一并移除；
   isConnected 保留为常真标记（个别功能块内部仍读取）。 */
const isConnected = ref(true)
const loading = ref(false)

/* 数据来源提示：镜像模式下必须把「数据截止时间」显示出来，避免被误认为实时数据 */
const sourceText = ref('')
const sourceTitle = ref('')

async function loadSource(): Promise<void> {
  try {
    const s = await navApi.status()
    const m = s.mirror
    if (!m) return
    if (m.mode === 'mirror') {
      const t = (m.data_deadline || '').replace('T', ' ')
      sourceText.value = `🧊 镜像数据${t ? ` · 截止 ${t}` : '（尚未同步）'}`
      sourceTitle.value = '查询走本地 SQLite 镜像：毫秒级返回，源库暂时不可达时页面仍可用'
    } else {
      sourceText.value = '🛰 直连源库（实时）'
      sourceTitle.value = m.fallback || '查询直接走源库'
    }
  } catch {
    /* 状态获取失败不影响主流程（与连接状态轮询失败的处理一致） */
  }
}

interface ToastItem {
  id: number
  msg: string
  type: string
  leaving: boolean
}
const toasts = ref<ToastItem[]>([])
let toastSeq = 0

function toast(msg: string, type = 'info'): void {
  const id = ++toastSeq
  toasts.value.push({ id, msg, type, leaving: false })
  window.setTimeout(() => {
    const t = toasts.value.find((x) => x.id === id)
    if (t) t.leaving = true
    window.setTimeout(() => {
      toasts.value = toasts.value.filter((x) => x.id !== id)
    }, 300)
  }, 2500)
}


/* ==================== 卡片折叠（原 toggleBody） ==================== */
const collapsedTpl1 = ref(false)
const collapsedTpl2 = ref(false)

/* ==================== 各功能块结果区 ==================== */
const results = reactive<Record<string, DmOutcome | null>>({
  taskQueryResult: null,
  taskQueryResult2: null,
  taskQueryResult3: null,
  taskQueryResult4: null,
  taskQueryResult5: null,
  taskQueryResult6: null,
  tplNewResult: null,
  taskQueryResultAgri: null,
  taskQueryResultLivestock: null,
  taskQueryResultAquatic: null,
  detectionNoResult: null,
})

function setProgress(key: string) {
  return (html: string): void => {
    results[key] = { type: 'html', html }
  }
}

/* ==================== ① 通用模板：分类通用数据汇总单列 ==================== */
const taskNoInput = ref('')

async function doTaskNo(exactTaskNo?: string): Promise<void> {
  results.taskQueryResult = null
  loading.value = true
  try {
    results.taskQueryResult = await queryByTaskNo({
      exactTaskNo,
      inputValue: taskNoInput.value,
      setProgress: setProgress('taskQueryResult'),
    })
  } finally {
    loading.value = false
  }
}

/* ==================== 其余功能块的输入项 ====================
   注意：这些 ref 必须声明在使用它们的处理函数（江苏追溯等）之前，
   否则 `const` 的暂时性死区会在模块求值时抛 ReferenceError，导致整个路由渲染为空。 */
const taskNoInput2 = ref('')
const taskNoInput3 = ref('')
const taskNoInput4 = ref('')
const taskNoInput5 = ref('')
const taskNoInput5b = ref('')
const monthlyContractNo = ref('')
const monthlyTaskNo = ref('')
const monthlyDateFrom = ref('')
const monthlyDateTo = ref('')
const taskNoInputAgri = ref('')
const taskNoInputLivestock = ref('')
const taskNoInputAquatic = ref('')

/* 按合同号导出（新增）：支持逗号/分号分隔多值，模糊匹配 CONTRACTS_NO */
const contractNo2 = ref('') // 江苏省追溯平台-监督抽查
const contractNo3 = ref('') // 江苏省追溯平台-例行监测
const contractNoAgri = ref('') // 省例行农产品
const contractNoLivestock = ref('') // 省例行畜产品
const contractNoAquatic = ref('') // 省例行水产品
const detectionNoContract = ref('') // 报检编号+小号 → 检测项目及方法

/* ==================== 江苏省追溯平台（监督抽查 / 例行监测） ==================== */
const jiangsuInputs = { 监督抽查: taskNoInput2, 例行监测: taskNoInput3 } as const
const jiangsuKeys = { 监督抽查: 'taskQueryResult2', 例行监测: 'taskQueryResult3' } as const

async function doJiangsu(variant: JiangsuVariant, exactTaskNo?: string): Promise<void> {
  const key = jiangsuKeys[variant]
  results[key] = null
  loading.value = true
  try {
    results[key] = await queryJiangsuTaskNo({
      variant,
      exactTaskNo,
      inputValue: jiangsuInputs[variant].value,
      setProgress: setProgress(key),
    })
  } finally {
    loading.value = false
  }
}

/* 按合同号导出（江苏省追溯平台：监督抽查 / 例行监测） */
async function doJiangsuContract(variant: JiangsuVariant): Promise<void> {
  const key = jiangsuKeys[variant]
  results[key] = null
  loading.value = true
  try {
    results[key] = await queryJiangsuTaskNo({
      variant,
      inputValue: jiangsuInputs[variant].value,
      contractNo: (variant === '监督抽查' ? contractNo2 : contractNo3).value,
      setProgress: setProgress(key),
    })
  } finally {
    loading.value = false
  }
}

/* ==================== 附件2 监测信息汇总表 ==================== */
async function doMonitorSummary(exactTaskNo?: string): Promise<void> {
  results.taskQueryResult4 = null
  loading.value = true
  try {
    results.taskQueryResult4 = await queryMonitorSummaryTable({
      exactTaskNo,
      inputValue: taskNoInput4.value,
      setProgress: setProgress('taskQueryResult4'),
    })
  } finally {
    loading.value = false
  }
}

/* ==================== 检测任务统计 ==================== */
async function doTaskStats(): Promise<void> {
  results.taskQueryResult5 = null
  loading.value = true
  try {
    results.taskQueryResult5 = await queryTaskStats({
      contractNo: taskNoInput5.value,
      taskNo: taskNoInput5b.value,
      setProgress: setProgress('taskQueryResult5'),
    })
  } finally {
    loading.value = false
  }
}

/* ==================== 检测结果月报 ==================== */
async function doMonthlyReport(): Promise<void> {
  results.taskQueryResult6 = null
  loading.value = true
  try {
    results.taskQueryResult6 = await queryMonthlyReport({
      contractNo: monthlyContractNo.value,
      taskNo: monthlyTaskNo.value,
      dateFrom: monthlyDateFrom.value,
      dateTo: monthlyDateTo.value,
      setProgress: setProgress('taskQueryResult6'),
    })
  } finally {
    loading.value = false
  }
}

/* ==================== 省例行农产品 ==================== */
async function doAgri(exactTaskNo?: string): Promise<void> {
  results.taskQueryResultAgri = null
  loading.value = true
  try {
    results.taskQueryResultAgri = await queryProvinceRoutineAgri({
      exactTaskNo,
      inputValue: taskNoInputAgri.value,
      setProgress: setProgress('taskQueryResultAgri'),
    })
  } finally {
    loading.value = false
  }
}

/* 按合同号导出（省例行农产品） */
async function doAgriContract(): Promise<void> {
  results.taskQueryResultAgri = null
  loading.value = true
  try {
    results.taskQueryResultAgri = await queryProvinceRoutineAgri({
      inputValue: taskNoInputAgri.value,
      contractNo: contractNoAgri.value,
      setProgress: setProgress('taskQueryResultAgri'),
    })
  } finally {
    loading.value = false
  }
}

/* ==================== 省例行畜产品 ==================== */
async function doLivestock(exactTaskNo?: string): Promise<void> {
  results.taskQueryResultLivestock = null
  loading.value = true
  try {
    results.taskQueryResultLivestock = await queryProvinceRoutineLivestock({
      exactTaskNo,
      inputValue: taskNoInputLivestock.value,
      setProgress: setProgress('taskQueryResultLivestock'),
    })
  } finally {
    loading.value = false
  }
}

/* 按合同号导出（省例行畜产品） */
async function doLivestockContract(): Promise<void> {
  results.taskQueryResultLivestock = null
  loading.value = true
  try {
    results.taskQueryResultLivestock = await queryProvinceRoutineLivestock({
      inputValue: taskNoInputLivestock.value,
      contractNo: contractNoLivestock.value,
      setProgress: setProgress('taskQueryResultLivestock'),
    })
  } finally {
    loading.value = false
  }
}

/* ==================== 省例行水产品 ==================== */
async function doAquatic(exactTaskNo?: string): Promise<void> {
  results.taskQueryResultAquatic = null
  loading.value = true
  try {
    results.taskQueryResultAquatic = await queryProvinceRoutineAquatic({
      exactTaskNo,
      inputValue: taskNoInputAquatic.value,
      setProgress: setProgress('taskQueryResultAquatic'),
    })
  } finally {
    loading.value = false
  }
}

/* 按合同号导出（省例行水产品） */
async function doAquaticContract(): Promise<void> {
  results.taskQueryResultAquatic = null
  loading.value = true
  try {
    results.taskQueryResultAquatic = await queryProvinceRoutineAquatic({
      inputValue: '',
      contractNo: contractNoAquatic.value,
      setProgress: setProgress('taskQueryResultAquatic'),
    })
  } finally {
    loading.value = false
  }
}

/* ==================== 年度数据快速统计 ==================== */
async function doNewTemplate(): Promise<void> {
  results.tplNewResult = null
  loading.value = true
  try {
    results.tplNewResult = await queryNewTemplate({
      year: tplNewYear.value,
      contract: tplNewContract.value,
      kinds: selectedNewKinds.value,
      isConnected: isConnected.value,
      setProgress: setProgress('tplNewResult'),
    })
  } finally {
    loading.value = false
  }
}

/* ==================== ⑪ 报检编号 → 检测项目及方法 ==================== */
const detectionNoInput = ref('')
const detectionNoType = ref('auto')
const detectionNoMode = ref('detail')

const detectionNoHint = computed(() => {
  const nos = parseNoList(detectionNoInput.value)
  if (!nos.length) return ''
  const col = resolveNoType(nos, detectionNoType.value)
  return `识别到 <b>${nos.length}</b> 个编号，编号类型：<b>${noTypeLabel(col)}</b>`
})

function clearDetectionNoInput(): void {
  detectionNoInput.value = ''
  results.detectionNoResult = null
}

async function doDetectionNo(forcedNo?: string, forcedCol?: string): Promise<void> {
  results.detectionNoResult = null
  loading.value = true
  try {
    results.detectionNoResult = await queryByDetectionNo({
      forcedNo,
      forcedCol,
      inputText: detectionNoInput.value,
      noTypeSel: detectionNoType.value,
      mode: detectionNoMode.value,
      setProgress: setProgress('detectionNoResult'),
    })
  } finally {
    loading.value = false
  }
}

/* 按合同号导出（报检编号+小号 → 检测项目及方法） */
async function doDetectionNoContract(): Promise<void> {
  results.detectionNoResult = null
  loading.value = true
  try {
    results.detectionNoResult = await queryByContractNo({
      contractNo: detectionNoContract.value,
      mode: detectionNoMode.value,
      setProgress: setProgress('detectionNoResult'),
    })
  } finally {
    loading.value = false
  }
}

/* ==================== 年度数据快速统计：年份 + 产品类别（原 initNewTemplate） ==================== */
const tplNewYear = ref(new Date().getFullYear())
const tplNewYears = (() => {
  const cur = new Date().getFullYear()
  const list: number[] = []
  for (let y = cur; y >= 2016; y -= 1) list.push(y)
  return list
})()
const tplNewContract = ref('')
const showKindBox = ref(false)
const kindChecked = reactive<Record<string, boolean>>(
  Object.fromEntries(NEW_KIND_NAMES.map((k) => [k, true])),
)
const selectedNewKinds = computed(() => NEW_KIND_NAMES.filter((k) => kindChecked[k]))



/* ==================== 生命周期 ==================== */
onMounted(() => {
  void loadSource()
})
</script>

<template>
  <div class="v-dm">
    <div class="loading" :class="{ show: loading }"><div class="spinner"></div></div>
    <div class="toast-box">
      <div
        v-for="t in toasts"
        :key="t.id"
        class="toast"
        :class="t.type"
        :style="t.leaving ? { opacity: '0', transition: 'opacity 0.3s' } : undefined"
      >{{ t.msg }}</div>
    </div>

    <header class="header">
      <h1>🗄️ 达梦数据库 - 数据导出</h1>
      <div style="display:flex;align-items:center;gap:8px;">
        <span
          v-if="sourceText"
          :title="sourceTitle"
          style="font-size:12px;color:var(--text2);background:#eef2ff;border-radius:20px;padding:4px 12px;white-space:nowrap;"
        >{{ sourceText }}</span>
      </div>
    </header>

    <div class="main">
      <!-- ======= 通用模板 ======= -->
      <div class="card">
        <div class="card-header" @click="collapsedTpl1 = !collapsedTpl1">
          <h3>📋 通用模板</h3>
          <span>{{ collapsedTpl1 ? '▶' : '▼' }}</span>
        </div>
        <div class="card-body" :class="{ hide: collapsedTpl1 }" id="tplBody1">
          <div style="padding:14px;background:#f0f4ff;border-radius:10px;border:1px solid #d0d8ff;">
            <p style="font-size:14px;font-weight:700;margin-bottom:4px;color:var(--primary);">📋 分类通用数据汇总单列</p>
            <p style="font-size:12px;color:var(--text2);margin-bottom:10px;">自动关联 DT_DETECTION + DT_SAMPLE + DT_SAMPLE_PROJECT + DT_RESULT_CHECK_IN，按任务编号导出</p>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
              <input
                v-model="taskNoInput"
                type="text"
                id="taskNoInput"
                placeholder="请输入任务编号（如：RW2026049）"
                style="flex:1;min-width:200px;padding:8px 12px;border:2px solid var(--primary);border-radius:8px;font-size:14px;outline:none;"
                @keydown.enter="doTaskNo()"
              />
              <button class="btn btn-primary" style="padding:8px 24px;font-weight:700;" @click="doTaskNo()">🚀 查询导出</button>
            </div>
            <div id="taskQueryResult" style="margin-top:8px;">
              <BlockResult :outcome="results.taskQueryResult" @pick-task="doTaskNo" />
            </div>
          </div>
        </div>
      </div>

      <!-- ======= 业务科数据汇总 ======= -->
      <div class="card">
        <div class="card-header" @click="collapsedTpl2 = !collapsedTpl2">
          <h3>📁 业务科数据汇总</h3>
          <span>{{ collapsedTpl2 ? '▶' : '▼' }}</span>
        </div>
        <div class="card-body" :class="{ hide: collapsedTpl2 }" id="tplBody2">
          <div style="padding:12px;background:#e8f5e9;border-radius:8px;border:1px solid #a5d6a7;margin-bottom:10px;">
            <p style="font-size:13px;font-weight:700;margin-bottom:4px;color:#2e7d32;">📋 江苏省追溯平台-监督抽查</p>
            <p style="font-size:11px;color:var(--text2);margin-bottom:8px;">参数列以检测项目名横向展开；任务编号可多个（逗号分隔、可省略RW），也可按合同号导出</p>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
              <input v-model="taskNoInput2" type="text" id="taskNoInput2" placeholder="任务编号，可多个（逗号分隔，可省略RW）" style="flex:1;min-width:160px;padding:8px 12px;border:2px solid #2e7d32;border-radius:8px;font-size:14px;outline:none;" />
              <button class="btn btn-primary" style="padding:8px 24px;font-weight:700;background:#2e7d32;border-color:#2e7d32;" @click="doJiangsu('监督抽查')">🚀 查询导出</button>
              <input v-model="contractNo2" type="text" id="contractNo2" placeholder="合同号（多个用逗号分隔）" style="width:190px;padding:8px 12px;border:2px solid #2e7d32;border-radius:8px;font-size:14px;outline:none;" @keydown.enter="doJiangsuContract('监督抽查')" />
              <button class="btn btn-primary" style="padding:8px 18px;font-weight:700;background:#fff;color:#2e7d32;border:2px solid #2e7d32;" @click="doJiangsuContract('监督抽查')">📄 按合同号导出</button>
            </div>
            <div id="taskQueryResult2" style="margin-top:8px;">
              <BlockResult :outcome="results.taskQueryResult2" @pick-task="(t: string) => doJiangsu('监督抽查', t)" />
            </div>
          </div>

          <div style="padding:12px;background:#fff7e6;border-radius:8px;border:1px solid #ffd591;margin-bottom:10px;">
            <p style="font-size:13px;font-weight:700;margin-bottom:4px;color:#d48806;">📋 江苏省追溯平台-例行监测</p>
            <p style="font-size:11px;color:var(--text2);margin-bottom:8px;">与监督抽查格式相同，区别：监测类别=例行监测、抽样单位=苏州市农产品质量安全监测中心；任务编号可多个（可省略RW），也可按合同号导出</p>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
              <input v-model="taskNoInput3" type="text" id="taskNoInput3" placeholder="任务编号，可多个（逗号分隔，可省略RW）" style="flex:1;min-width:160px;padding:8px 12px;border:2px solid #d48806;border-radius:8px;font-size:14px;outline:none;" />
              <button class="btn btn-primary" style="padding:8px 24px;font-weight:700;background:#d48806;border-color:#d48806;" @click="doJiangsu('例行监测')">🚀 查询导出</button>
              <input v-model="contractNo3" type="text" id="contractNo3" placeholder="合同号（多个用逗号分隔）" style="width:190px;padding:8px 12px;border:2px solid #d48806;border-radius:8px;font-size:14px;outline:none;" @keydown.enter="doJiangsuContract('例行监测')" />
              <button class="btn btn-primary" style="padding:8px 18px;font-weight:700;background:#fff;color:#d48806;border:2px solid #d48806;" @click="doJiangsuContract('例行监测')">📄 按合同号导出</button>
            </div>
            <div id="taskQueryResult3" style="margin-top:8px;">
              <BlockResult :outcome="results.taskQueryResult3" @pick-task="(t: string) => doJiangsu('例行监测', t)" />
            </div>
          </div>

          <div style="padding:12px;background:#f0e6ff;border-radius:8px;border:1px solid #d4b8ff;margin-bottom:10px;">
            <p style="font-size:13px;font-weight:700;margin-bottom:4px;color:#7c3aed;">📋 附件2 监测信息汇总表</p>
            <p style="font-size:11px;color:var(--text2);margin-bottom:8px;">平坦表格式（无检测项目列）。抽样单位按监测类别区分，按任务编号导出</p>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
              <input v-model="taskNoInput4" type="text" id="taskNoInput4" placeholder="请输入任务编号（如：RW2026049）" style="flex:1;min-width:160px;padding:8px 12px;border:2px solid #7c3aed;border-radius:8px;font-size:14px;outline:none;" />
              <button class="btn btn-primary" style="padding:8px 24px;font-weight:700;background:#7c3aed;border-color:#7c3aed;" @click="doMonitorSummary()">🚀 查询导出</button>
            </div>
            <div id="taskQueryResult4" style="margin-top:8px;">
              <BlockResult :outcome="results.taskQueryResult4" @pick-task="(t: string) => doMonitorSummary(t)" />
            </div>
          </div>

          <div style="padding:12px;background:#e0f7fa;border-radius:8px;border:1px solid #80deea;">
            <p style="font-size:13px;font-weight:700;margin-bottom:4px;color:#00838f;">📋 检测任务统计</p>
            <p style="font-size:11px;color:var(--text2);margin-bottom:8px;">按合同号/任务编号统计检测任务完成情况，包含检出率、超标率等指标</p>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
              <input v-model="taskNoInput5" type="text" id="taskNoInput5" placeholder="合同号（如：PS2025001）" style="padding:8px 12px;border:2px solid #00838f;border-radius:8px;font-size:14px;outline:none;width:160px;" />
              <input v-model="taskNoInput5b" type="text" id="taskNoInput5b" placeholder="任务编号（如：RW2026049）" style="padding:8px 12px;border:2px solid #00838f;border-radius:8px;font-size:14px;outline:none;width:160px;" />
              <button class="btn btn-primary" style="padding:8px 24px;font-weight:700;background:#00838f;border-color:#00838f;" @click="doTaskStats()">🚀 查询导出</button>
            </div>
            <div id="taskQueryResult5" style="margin-top:8px;">
              <BlockResult :outcome="results.taskQueryResult5" />
            </div>
          </div>

          <!-- 检测结果月报 -->
          <div style="padding:12px;background:#fce4ec;border-radius:8px;border:1px solid #f48fb1;">
            <p style="font-size:13px;font-weight:700;margin-bottom:4px;color:#c62828;">📋 检测结果月报</p>
            <p style="font-size:11px;color:var(--text2);margin-bottom:8px;">按合同号/任务编号/受理日期导出按任务+大类的统计月报</p>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
              <input v-model="monthlyContractNo" type="text" id="monthlyContractNo" placeholder="合同号（多个用逗号分隔）" style="padding:8px 12px;border:2px solid #c62828;border-radius:8px;font-size:14px;outline:none;width:200px;" />
              <input v-model="monthlyTaskNo" type="text" id="monthlyTaskNo" placeholder="任务编号（多个用逗号分隔）" style="padding:8px 12px;border:2px solid #c62828;border-radius:8px;font-size:14px;outline:none;width:200px;" />
              <input v-model="monthlyDateFrom" type="date" id="monthlyDateFrom" placeholder="受理日期起" style="padding:8px 12px;border:2px solid #c62828;border-radius:8px;font-size:14px;outline:none;width:160px;" />
              <input v-model="monthlyDateTo" type="date" id="monthlyDateTo" placeholder="受理日期止" style="padding:8px 12px;border:2px solid #c62828;border-radius:8px;font-size:14px;outline:none;width:160px;" />
              <button class="btn btn-primary" style="padding:8px 24px;font-weight:700;background:#c62828;border-color:#c62828;" @click="doMonthlyReport()">🚀 查询导出</button>
            </div>
            <div id="taskQueryResult6" style="margin-top:8px;">
              <BlockResult :outcome="results.taskQueryResult6" />
            </div>
          </div>

          <!-- 年度数据快速统计 -->
          <div style="padding:12px;background:#e8f5e9;border-radius:8px;border:1px solid #81c784;margin-top:10px;">
            <p style="font-size:13px;font-weight:700;margin-bottom:4px;color:#2e7d32;">📊 年度数据快速统计</p>
            <p style="font-size:11px;color:var(--text2);margin-bottom:8px;">按受理年份 + 合同编号 + 产品类别导出，检测项目动态列，任务名称/合同编号随行</p>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
              <select v-model.number="tplNewYear" id="tplNewYear" style="padding:8px 12px;border:2px solid #2e7d32;border-radius:8px;font-size:14px;outline:none;width:120px;">
                <option v-for="y in tplNewYears" :key="y" :value="y">{{ y }}年</option>
              </select>
              <input v-model="tplNewContract" type="text" id="tplNewContract" placeholder="合同编号（可留空=全部，多个用逗号分隔）" style="padding:8px 12px;border:2px solid #2e7d32;border-radius:8px;font-size:14px;outline:none;width:220px;" />
              <button type="button" class="btn btn-primary" style="padding:8px 12px;font-size:13px;background:#2e7d32;border-color:#2e7d32;" @click="showKindBox = !showKindBox">产品类别▾</button>
              <div id="tplNewKindBox" style="width:100%;margin-top:8px;padding:10px;background:#f1f8e9;border:1px solid #c5e1a5;border-radius:8px;" :style="showKindBox ? undefined : { display: 'none' }">
                <div id="tplNewKinds" style="display:flex;flex-wrap:wrap;gap:8px;">
                  <label
                    v-for="k in NEW_KIND_NAMES"
                    :key="k"
                    class="tpl-kind"
                    style="display:inline-flex;align-items:center;gap:5px;padding:4px 10px;background:#fff;border:1px solid #c5e1a5;border-radius:16px;font-size:12px;cursor:pointer;user-select:none;"
                  >
                    <input v-model="kindChecked[k]" type="checkbox" :value="k" style="accent-color:#2e7d32;cursor:pointer;" />{{ k }}
                  </label>
                </div>
              </div>
              <button class="btn btn-primary" style="padding:8px 24px;font-weight:700;background:#2e7d32;border-color:#2e7d32;" @click="doNewTemplate()">🚀 查询导出</button>
            </div>
            <div id="tplNewResult" style="margin-top:8px;">
              <BlockResult :outcome="results.tplNewResult" />
            </div>
          </div>

          <!-- 省例行农产品数据汇总 -->
          <div style="padding:12px;background:#e8f5e9;border-radius:8px;border:1px solid #81c784;margin-top:10px;">
            <p style="font-size:13px;font-weight:700;margin-bottom:4px;color:#2e7d32;">📋 省例行农产品数据汇总</p>
            <p style="font-size:11px;color:var(--text2);margin-bottom:8px;">种植业产品例行监测检测数据；任务编号可多个（逗号分隔、可省略RW），也可按合同号导出（不做产品过滤）</p>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
              <input v-model="taskNoInputAgri" type="text" id="taskNoInputAgri" placeholder="任务编号，可多个（逗号分隔，可省略RW）" style="flex:1;min-width:160px;padding:8px 12px;border:2px solid #2e7d32;border-radius:8px;font-size:14px;outline:none;" />
              <button class="btn btn-primary" style="padding:8px 24px;font-weight:700;background:#2e7d32;border-color:#2e7d32;" @click="doAgri()">🚀 查询导出</button>
              <input v-model="contractNoAgri" type="text" id="contractNoAgri" placeholder="合同号（多个用逗号分隔）" style="width:190px;padding:8px 12px;border:2px solid #2e7d32;border-radius:8px;font-size:14px;outline:none;" @keydown.enter="doAgriContract()" />
              <button class="btn btn-primary" style="padding:8px 18px;font-weight:700;background:#fff;color:#2e7d32;border:2px solid #2e7d32;" @click="doAgriContract()">📄 按合同号导出</button>
            </div>
            <div id="taskQueryResultAgri" style="margin-top:8px;">
              <BlockResult :outcome="results.taskQueryResultAgri" @pick-task="(t: string) => doAgri(t)" />
            </div>
          </div>

          <!-- 省例行畜产品数据汇总 -->
          <div style="padding:12px;background:#fbe9e7;border-radius:8px;border:1px solid #e57373;margin-top:10px;">
            <p style="font-size:13px;font-weight:700;margin-bottom:4px;color:#c62828;">📋 省例行畜产品数据汇总</p>
            <p style="font-size:11px;color:var(--text2);margin-bottom:8px;">畜禽产品例行监测检测数据；任务编号可多个（逗号分隔、可省略RW），也可按合同号导出（不做产品过滤）</p>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
              <input v-model="taskNoInputLivestock" type="text" id="taskNoInputLivestock" placeholder="任务编号，可多个（逗号分隔，可省略RW）" style="flex:1;min-width:160px;padding:8px 12px;border:2px solid #c62828;border-radius:8px;font-size:14px;outline:none;" />
              <button class="btn btn-primary" style="padding:8px 24px;font-weight:700;background:#c62828;border-color:#c62828;" @click="doLivestock()">🚀 查询导出</button>
              <input v-model="contractNoLivestock" type="text" id="contractNoLivestock" placeholder="合同号（多个用逗号分隔）" style="width:190px;padding:8px 12px;border:2px solid #c62828;border-radius:8px;font-size:14px;outline:none;" @keydown.enter="doLivestockContract()" />
              <button class="btn btn-primary" style="padding:8px 18px;font-weight:700;background:#fff;color:#c62828;border:2px solid #c62828;" @click="doLivestockContract()">📄 按合同号导出</button>
            </div>
            <div id="taskQueryResultLivestock" style="margin-top:8px;">
              <BlockResult :outcome="results.taskQueryResultLivestock" @pick-task="(t: string) => doLivestock(t)" />
            </div>
          </div>

          <!-- 省例行水产品数据汇总 -->
          <div style="padding:12px;background:#e3f2fd;border-radius:8px;border:1px solid #64b5f6;margin-top:10px;">
            <p style="font-size:13px;font-weight:700;margin-bottom:4px;color:#1565c0;">📋 省例行水产品数据汇总</p>
            <p style="font-size:11px;color:var(--text2);margin-bottom:8px;">水产品质量安全例行监测结果汇总，按任务编号导出（可多个、逗号分隔、可省略RW），也可按合同号导出（不做产品过滤）</p>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
              <input v-model="taskNoInputAquatic" type="text" id="taskNoInputAquatic" placeholder="任务编号，可多个（逗号分隔，可省略RW）" style="flex:1;min-width:160px;padding:8px 12px;border:2px solid #1565c0;border-radius:8px;font-size:14px;outline:none;" />
              <button class="btn btn-primary" style="padding:8px 24px;font-weight:700;background:#1565c0;border-color:#1565c0;" @click="doAquatic()">🚀 查询导出</button>
              <input v-model="contractNoAquatic" type="text" id="contractNoAquatic" placeholder="合同号（多个用逗号分隔）" style="width:190px;padding:8px 12px;border:2px solid #1565c0;border-radius:8px;font-size:14px;outline:none;" @keydown.enter="doAquaticContract()" />
              <button class="btn btn-primary" style="padding:8px 18px;font-weight:700;background:#fff;color:#1565c0;border:2px solid #1565c0;" @click="doAquaticContract()">📄 按合同号导出</button>
            </div>
            <div id="taskQueryResultAquatic" style="margin-top:8px;">
              <BlockResult :outcome="results.taskQueryResultAquatic" @pick-task="(t: string) => doAquatic(t)" />
            </div>
          </div>

          <!-- 编号（报检/原始） -> 检测项目及方法 -->
          <div style="padding:12px;background:#ede7f6;border-radius:8px;border:1px solid #b39ddb;margin-top:10px;">
            <p style="font-size:13px;font-weight:700;margin-bottom:4px;color:#4527a0;">📋 报检编号+小号 → 检测项目及方法</p>
            <p style="font-size:11px;color:var(--text2);margin-bottom:8px;">
              按「报检编号+小号」组合键（如 <b>WN26090028</b> + <b>01</b> = <b>WN2609002801</b>）导出涉及的检测项目及其检测方法/标准；
              也兼容只填报检编号（不带小号），以及原始编号（ORIGINAL_NO）。
              <b>去重模式</b>导出为 <b>基质（样品名称）、检测项目、检测方法</b> 三列并自动去重；
              <b>支持从 Excel 整列复制后直接粘贴</b>（每行一个，也支持逗号/分号/Tab 分隔），重复值与表头会自动去掉；
              也可在下方合同号框填合同号（多个用逗号分隔），按合同号导出全部样品的检测项目及方法。
            </p>
            <textarea
              v-model="detectionNoInput"
              id="detectionNoInput"
              rows="5"
              placeholder="从 Excel 整列复制后粘贴到这里，例如：&#10;WN2609002801&#10;JS2609000701&#10;WX2609005901"
              style="width:100%;box-sizing:border-box;padding:10px 12px;border:2px solid #4527a0;border-radius:8px;font-size:13px;font-family:Consolas,Menlo,monospace;outline:none;resize:vertical;"
            ></textarea>
            <div id="detectionNoHint" style="margin-top:6px;font-size:12px;color:#4527a0;" v-html="detectionNoHint"></div>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px;">
              <select v-model="detectionNoType" id="detectionNoType" style="padding:8px 12px;border:2px solid #4527a0;border-radius:8px;font-size:14px;outline:none;">
                <option value="auto">编号类型：自动识别</option>
                <option value="DETECTION_KEY">按报检编号+小号（如 WN2609002801）</option>
                <option value="ORIGINAL_NO">按原始编号（如 常农（农产品）抽20260907103）</option>
              </select>
              <select v-model="detectionNoMode" id="detectionNoMode" style="padding:8px 12px;border:2px solid #4527a0;border-radius:8px;font-size:14px;outline:none;">
                <option value="detail">明细（每条样品项目一行）</option>
                <option value="distinct">去重（基质+项目+方法唯一）</option>
              </select>
              <button class="btn btn-primary" style="padding:8px 24px;font-weight:700;background:#4527a0;border-color:#4527a0;" @click="doDetectionNo()">🚀 查询导出</button>
              <input v-model="detectionNoContract" type="text" id="detectionNoContract" placeholder="合同号（多个用逗号分隔）" style="width:190px;padding:8px 12px;border:2px solid #4527a0;border-radius:8px;font-size:14px;outline:none;" @keydown.enter="doDetectionNoContract()" />
              <button class="btn btn-primary" style="padding:8px 18px;font-weight:700;background:#fff;color:#4527a0;border:2px solid #4527a0;" @click="doDetectionNoContract()">📄 按合同号导出</button>
              <button class="btn btn-outline btn-sm" style="padding:8px 16px;font-size:13px;" @click="clearDetectionNoInput">清空</button>
            </div>
            <div id="detectionNoResult" style="margin-top:8px;">
              <BlockResult :outcome="results.detectionNoResult" @pick-no="doDetectionNo" />
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style>
/* 原样式完整保留，仅加 `.v-dm` 前缀做隔离；body 规则改挂根容器 */
.v-dm { --primary:#4f6ef7; --primary-hover:#3b5de7; --danger:#e74c3c; --success:#27ae60; --warning:#f39c12;
  --bg:#f0f2f5; --card-bg:#fff; --text:#2c3e50; --text2:#7f8c8d; --border:#e0e4e8;
  --shadow:0 2px 12px rgba(0,0,0,0.08); --radius:10px; }
.v-dm, .v-dm * { margin: 0; padding: 0; box-sizing: border-box; }
.v-dm {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  background: var(--bg); color: var(--text); min-height: 100vh;
}

/* Header */
.v-dm .header {
  background: var(--card-bg); border-bottom: 1px solid var(--border);
  padding: 0 24px; height: 60px;
  display: flex; align-items: center; justify-content: space-between;
  box-shadow: 0 1px 4px rgba(0,0,0,0.04); position: sticky; top: 0; z-index: 100;
}
.v-dm .header h1 { font-size: 20px; font-weight: 700; color: var(--primary); }
.v-dm .conn-status {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 13px; padding: 4px 14px; border-radius: 20px; font-weight: 500;
}
.v-dm .conn-status.ok { background: #e8f5e9; color: var(--success); }
.v-dm .conn-status.no { background: #fef0ef; color: var(--danger); }
.v-dm .conn-status .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.v-dm .conn-status.ok .dot { background: var(--success); }
.v-dm .conn-status.no .dot { background: var(--danger); }

.v-dm .main { max-width: 1300px; margin: 0 auto; padding: 20px 24px; }

/* 卡片 */
.v-dm .card {
  background: var(--card-bg); border-radius: var(--radius);
  box-shadow: var(--shadow); margin-bottom: 16px; overflow: hidden;
}
.v-dm .card-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 18px; border-bottom: 1px solid var(--border);
  background: #fafbfc; cursor: pointer;
}
.v-dm .card-header h3 { font-size: 14px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.v-dm .card-body { padding: 16px 18px; }
.v-dm .card-body.hide { display: none; }

/* 按钮 */
.v-dm .btn {
  padding: 8px 18px; border-radius: 8px; border: none;
  font-size: 14px; font-weight: 500; cursor: pointer;
  transition: 0.2s; display: inline-flex; align-items: center; gap: 6px;
}
.v-dm .btn:disabled { opacity: 0.5; cursor: not-allowed; }
.v-dm .btn-primary { background: var(--primary); color: #fff; }
.v-dm .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
.v-dm .btn-success { background: var(--success); color: #fff; }
.v-dm .btn-success:hover:not(:disabled) { background: #219a52; }
.v-dm .btn-danger { background: var(--danger); color: #fff; }
.v-dm .btn-danger:hover:not(:disabled) { background: #c0392b; }
.v-dm .btn-outline { background: transparent; border: 1.5px solid var(--border); color: var(--text); }
.v-dm .btn-outline:hover:not(:disabled) { background: var(--bg); }
.v-dm .btn-sm { padding: 5px 12px; font-size: 12px; border-radius: 6px; }
.v-dm .btn-lg { padding: 10px 28px; font-size: 15px; }

/* 连接表单（页面顶部卡片） */
.v-dm .conn-bar {
  display: flex; align-items: center; gap: 8px;
  background: var(--card-bg); padding: 6px 12px;
  border-radius: 20px; box-shadow: var(--shadow);
  font-size: 13px; cursor: pointer; flex-shrink: 0;
}
.v-dm .conn-bar:hover { background: #f0f2f5; }
.v-dm .conn-card { margin-bottom: 16px; }

/* 加载 */
.v-dm .loading {
  display: none; position: fixed; inset: 0;
  background: rgba(255,255,255,0.7); z-index: 500;
  align-items: center; justify-content: center;
}
.v-dm .loading.show { display: flex; }
.v-dm .spinner {
  width: 40px; height: 40px;
  border: 4px solid var(--border); border-top-color: var(--primary);
  border-radius: 50%; animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Toast */
.v-dm .toast-box {
  position: fixed; top: 20px; right: 20px; z-index: 1000;
  display: flex; flex-direction: column; gap: 8px;
}
.v-dm .toast {
  padding: 12px 20px; border-radius: 8px; color: #fff;
  font-size: 14px; box-shadow: 0 4px 16px rgba(0,0,0,0.15);
  animation: slideIn 0.3s ease;
}
.v-dm .toast.ok { background: var(--success); }
.v-dm .toast.err { background: var(--danger); }
.v-dm .toast.info { background: var(--primary); }
@keyframes slideIn { from { opacity: 0; transform: translateX(100%); } to { opacity: 1; transform: translateX(0); } }
</style>
