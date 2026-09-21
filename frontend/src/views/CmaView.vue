<script setup lang="ts">
/**
 * 一单一库核对（CMA，移植自 cma-checker/cma-check.html，原 :3002）。
 *
 * UI 保真：header / 使用说明 / 输入卡 / 结果卡（统计卡、进度条、分页、表格）的 DOM、class 与文案逐字保留，
 * 样式统一加 `.v-cma` 前缀做隔离（`body` 规则改挂根容器）。
 *
 * JS 迁移：命令式 DOM/innerHTML → 响应式状态 + 模板渲染；`escapeHtml()` 由 Vue 插值取代（等价且更安全）；
 * 分页渲染改为计算属性；内联事件 → @click / @change。
 * 接口：`/api/check-one` → `/api/cma/check-one`、`/api/check-standards` → `/api/cma/check-standards`（同义改写）。
 *
 * 逐字保留：`normStdCode`（引号剥离 / CSV 取首字段 / 截到最后一个"-4位年号"）、按「编号（含年号）」表头
 * 定位（表头可在任意行/列，col===1 视为行标题横向读取）、Excel 回退识别首个像标准号的列、
 * 5 并发 + 按输入顺序占位回填、统计口径（在清单内 / 版本待确认 / 不在清单内 / 有备注）、
 * 清单覆盖率文案、分页省略号规则、Excel 导出（列宽、备注列配色、版本不符字体色、末尾汇总行）、
 * 复制不在清单标准号（含 execCommand 回退）。
 *
 * 注：原文件中的 `checkBatch()` 定义了但**从未被调用**（startCheck 直接调 `/api/check-one`），属死代码，未迁移。
 */
import { computed, onMounted, ref } from 'vue'
import ExcelJS from 'exceljs'
import CmaDispPicker from '../components/CmaDispPicker.vue'
import {
  cmaApi,
  type CmaLedgerBatch,
  type CmaLedgerItem,
  type CmaLedgerStats,
  type CmaResult,
} from '../api/client'

/* ==================== 状态 ==================== */
const inputText = ref('')
const results = ref<(CmaResult | null)[]>([])
const checking = ref(false)
const currentPage = ref(1)
const pageSize = ref(50)
const currentFilter = ref<string | null>(null)
const progressDone = ref(0)
const progressTotal = ref(0)
const inFlight = ref<string[]>([])
const showProgress = ref(false)
const showResultActions = ref(false)
const progressFinished = ref(false)

interface ToastItem {
  id: number
  msg: string
  type: string
}
const toasts = ref<ToastItem[]>([])
let toastSeq = 0

function toast(msg: string, type = 'info'): void {
  const id = ++toastSeq
  toasts.value.push({ id, msg, type })
  window.setTimeout(() => {
    toasts.value = toasts.value.filter((x) => x.id !== id)
  }, 3000)
}

/* ==================== 核对留痕（台账）信息 ==================== */
/**
 * 留痕目的：让"我们一直在核对、结论如何处理"这件事有据可查，支撑资质认定
 * 对"能力持续符合 + 管理体系持续有效运行"的检查。
 * 台账信息随每次核对写入后端 SQLite，**只增不删**；操作人/部门/用途在本机记忆，避免重复填写。
 */
const IDENTITY_KEY = 'cma_ledger_identity'
const operator = ref('')
const dept = ref('')
const purpose = ref('')
/** 本次输入来源（文件上传 / 能力表导入 / 填充示例 / 手工输入）——留痕时一并记录 */
const inputSource = ref('手工输入')
const lastBatchId = ref('')
const ledgerTip = ref('')

const identityText = computed(() => {
  const who = operator.value.trim()
  if (!who) return '未填写操作人（台账将记为匿名）'
  return `操作人：${who}${dept.value.trim() ? ` · ${dept.value.trim()}` : ''}`
})

function loadIdentity(): void {
  try {
    const raw = localStorage.getItem(IDENTITY_KEY)
    if (!raw) return
    const it = JSON.parse(raw) as { operator?: string; dept?: string; purpose?: string }
    operator.value = it.operator || ''
    dept.value = it.dept || ''
    purpose.value = it.purpose || ''
  } catch {
    /* 本地缓存损坏时忽略，不影响核对 */
  }
}

function saveIdentity(): void {
  try {
    localStorage.setItem(
      IDENTITY_KEY,
      JSON.stringify({
        operator: operator.value.trim(),
        dept: dept.value.trim(),
        purpose: purpose.value.trim(),
      }),
    )
  } catch {
    /* 隐私模式等不可写场景忽略 */
  }
}

/** 批次号：时间前缀便于人工对数，短随机后缀防并发撞号 */
function newBatchId(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  const ts = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
  return `CMA${ts}-${Math.random().toString(16).slice(2, 8).toUpperCase()}`
}

/* ==================== 标准号解析（逐字保留） ==================== */
/** 规范化表头文本：全角括号转半角、去首尾空白，便于匹配 */
function normHeader(s: unknown): string {
  return String(s == null ? '' : s)
    .replace(/[（）]/g, '()')
    .replace(/　/g, ' ')
    .trim()
}

/** 判断表头是否匹配"编号（含年号）" */
function isTargetHeader(s: unknown): boolean {
  const n = normHeader(s).toLowerCase()
  return n.includes('编号') && n.includes('年号')
}

/** 仅保留"标准编号 + 年号"，忽略年号后的方法/附录等后缀（如"第二法""附录A"） */
function normStdCode(raw: unknown): string {
  let s = String(raw == null ? '' : raw).trim()
  if (!s) return ''
  // 去可能的引号包裹
  s = s.replace(/^["']|["']$/g, '').trim()
  // CSV/TSV：取第一个字段
  if (s.includes(',') || s.includes('\t') || s.includes(';')) s = s.split(/[,;\t]/)[0].trim()
  // 截取到最后一个"-年号"为止（年号通常为 4 位数字），丢弃其后内容
  const m = s.match(/(.*-\d{4})/)
  if (m) s = m[1]
  return s.replace(/\s+/g, ' ').trim()
}

/** 去重并保持顺序：先规范化（仅保留标准编号+年号），再按规范化结果去重 */
function dedupCodes(arr: unknown[]): string {
  const seen = new Set<string>()
  const r: string[] = []
  for (const x of arr) {
    const t = normStdCode(x)
    if (t && !seen.has(t)) {
      seen.add(t)
      r.push(t)
    }
  }
  return r.join('\n')
}

function parseCodes(text: string): string[] {
  const lines = String(text || '').split(/\r?\n/)
  const seen = new Set<string>()
  const out: string[] = []
  for (const line of lines) {
    const code = normStdCode(line) // 仅保留标准编号+年号，忽略"第二法/附录A"等后缀
    if (!code) continue
    if (seen.has(code)) continue // 按标准编号+年号去重
    seen.add(code)
    out.push(code)
  }
  return out
}

const inputCodes = computed(() => parseCodes(inputText.value))
const inputCountText = computed(() => `已输入 ${inputCodes.value.length} 条`)

/* ==================== Excel 读取（逐字保留） ==================== */
/** 在前若干行/列中查找"编号（含年号）"所在单元格，返回 {rowIdx, col}（0基），找不到返回 null */
function findHeaderCell(rows: unknown[][]): { rowIdx: number; col: number } | null {
  const MAXR = 30
  const MAXC = 60
  const lim = Math.min(rows.length, MAXR)
  for (let i = 0; i < lim; i += 1) {
    const row = rows[i]
    if (!row) continue
    const cLim = Math.min(row.length, MAXC)
    for (let j = 1; j < cLim; j += 1) {
      if (isTargetHeader(row[j])) return { rowIdx: i, col: j }
    }
  }
  return null
}

/** 按表头/行标题定位并提取：col===1 视为行标题（横向读取），否则列标题（纵向读取） */
function extractByHeader(rows: unknown[][]): { ok: boolean; text: string; n?: number; mode?: string } {
  const hdr = findHeaderCell(rows)
  if (!hdr) return { ok: false, text: '' }
  const pick = (v: unknown) => (v !== null && v !== undefined ? String(v).trim() : '')
  const out: string[] = []
  if (hdr.col === 1) {
    const row = rows[hdr.rowIdx]
    for (let j = 2; j < row.length; j += 1) {
      const s = pick(row[j])
      if (s && /[A-Za-z0-9]/.test(s)) out.push(s)
    }
  } else {
    for (let i = hdr.rowIdx + 1; i < rows.length; i += 1) {
      const s = pick(rows[i] ? rows[i][hdr.col] : undefined)
      if (s) out.push(s)
    }
  }
  const text = dedupCodes(out)
  const n = text ? text.split('\n').length : 0
  return { ok: true, text, n, mode: hdr.col === 1 ? 'row' : 'col' }
}

/** 从 Excel(.xlsx) ArrayBuffer 提取标准号（先按表头定位，回退识别首个像标准号的列） */
async function readExcelFromBuffer(buf: ArrayBuffer): Promise<string> {
  const wb = new ExcelJS.Workbook()
  await wb.xlsx.load(buf)
  const ws = wb.worksheets && wb.worksheets[0]
  if (!ws) return ''
  const rows: unknown[][] = []
  ws.eachRow((row) => {
    rows.push(row.values as unknown[])
  })
  if (!rows.length) return ''

  // 优先按"编号（含年号）"表头/行标题定位（支持表头位于任意行，如第3行、H列）
  const hdr = extractByHeader(rows)
  if (hdr.ok) return hdr.text

  // 回退：自动识别首个像标准号的列
  const pick = (v: unknown) => (v !== null && v !== undefined ? String(v).trim() : '')
  const isStd = (s: unknown) => typeof s === 'string' && /^[A-Za-z][A-Za-z0-9/.\- ]*\d/.test(s.trim())
  const out: string[] = []
  let startIdx = 0
  if (!rows[0].some((c) => isStd(c))) startIdx = 1 // 首行像表头则跳过
  for (let i = startIdx; i < rows.length; i += 1) {
    const cells = rows[i]
    if (!cells) continue
    let picked = ''
    for (let j = 1; j < cells.length; j += 1) {
      const c = cells[j]
      if (isStd(c)) {
        picked = String(c).trim()
        break
      }
    }
    if (!picked) {
      for (let j = 1; j < cells.length; j += 1) {
        const c = cells[j]
        const s = pick(c)
        if (s && /[A-Za-z0-9]/.test(s)) {
          picked = s
          break
        }
      }
    }
    if (picked) out.push(picked)
  }
  return dedupCodes(out)
}

/** 能力表专用读取：严格要求表头/行标题为"编号（含年号）" */
async function readAbilityTable(buf: ArrayBuffer): Promise<{ ok: boolean; text: string; msg: string }> {
  let wb: ExcelJS.Workbook
  try {
    wb = new ExcelJS.Workbook()
    await wb.xlsx.load(buf)
  } catch (e) {
    return { ok: false, text: '', msg: `Excel 解析失败（仅支持 .xlsx）：${(e as Error).message}` }
  }
  const ws = wb.worksheets && wb.worksheets[0]
  if (!ws) return { ok: false, text: '', msg: 'Excel 中未找到工作表' }
  const rows: unknown[][] = []
  ws.eachRow((row) => {
    rows.push(row.values as unknown[])
  })
  if (!rows.length) return { ok: false, text: '', msg: 'Excel 内容为空' }
  const hdr = extractByHeader(rows)
  if (hdr.ok) {
    const where = hdr.mode === 'row' ? '行' : '列'
    return {
      ok: true,
      text: hdr.text,
      msg: `已读取"编号（含年号）"${where}，按标准编号+年号去重后 ${hdr.n} 条（已忽略第二法/附录等后缀）`,
    }
  }
  return {
    ok: false,
    text: '',
    msg: '未找到表头（或行标题）为"编号（含年号）"的数据（表头可在任意行/列，如第3行、H列），请确认 Excel 格式或改用"上传文件"',
  }
}

/* ==================== 输入区按钮 ==================== */
const fileInput = ref<HTMLInputElement | null>(null)
const abilityFileInput = ref<HTMLInputElement | null>(null)

function fillSample(): void {
  inputText.value = 'GB 2749-2015\nGB 5009.12-2023\nGB/T 5009.1-2003\nGB9744-2024/XG1-2025\nGB 2749\nNOTEXIST-STANDARD-9999'
  inputSource.value = '填充示例'
}

function clearAll(): void {
  inputText.value = ''
  resetResults()
}

async function onAbilityFileChange(e: Event): Promise<void> {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return
  try {
    const buf = await file.arrayBuffer()
    const res = await readAbilityTable(buf)
    if (res.ok) {
      inputText.value = res.text
      inputSource.value = '能力表导入'
      toast(res.msg || '能力表已导入', 'ok')
    } else {
      toast(res.msg || '能力表导入失败', 'err')
    }
  } catch (err) {
    toast(`能力表解析失败（仅支持 .xlsx）：${(err as Error).message}`, 'err')
  }
  target.value = ''
}

async function onFileChange(e: Event): Promise<void> {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return
  const ext = file.name.toLowerCase().split('.').pop()
  if (ext === 'xlsx' || ext === 'xls') {
    try {
      const buf = await file.arrayBuffer()
      const text = await readExcelFromBuffer(buf)
      inputText.value = text
      if (text) inputSource.value = '文件上传'
      toast(text ? 'Excel 已载入' : 'Excel 未识别到标准号', text ? 'ok' : 'err')
    } catch (err) {
      toast(`Excel 解析失败（仅支持 .xlsx）：${(err as Error).message}`, 'err')
    }
  } else {
    let text = (await file.text()) || ''
    if (ext === 'json') {
      try {
        const arr = JSON.parse(text)
        if (Array.isArray(arr)) {
          text = arr
            .map((x) => (typeof x === 'string' ? x : x.standardCode || x.code || x.standard || ''))
            .filter(Boolean)
            .join('\n')
        }
      } catch {
        /* 当作普通文本 */
      }
    }
    inputText.value = text
    if (text) inputSource.value = '文件上传'
    toast('文件已载入', 'ok')
  }
  target.value = ''
}

/* ==================== 统计 / 筛选 / 分页 ==================== */
const validResults = computed(() => results.value.filter(Boolean) as CmaResult[])

const stats = computed(() => {
  const valid = validResults.value
  return {
    total: valid.length,
    inCnt: valid.filter((r) => r.found && !r.versionMismatch).length,
    warnCnt: valid.filter((r) => r.found && r.versionMismatch).length,
    remarkCnt: valid.filter((r) => r.found && r.remark).length,
    outCnt: valid.filter((r) => !r.found).length,
  }
})

const rateText = computed(() => {
  const valid = validResults.value
  if (valid.length === 0) return ''
  const rate = ((stats.value.inCnt / valid.length) * 100).toFixed(1)
  return `清单覆盖率 ${rate}%` + (stats.value.warnCnt > 0 ? `（含版本待确认 ${stats.value.warnCnt} 条）` : '')
})

function getFilteredResults(): CmaResult[] {
  const f = currentFilter.value
  const valid = validResults.value
  if (!f || f === 'total') return valid
  if (f === 'in') return valid.filter((r) => r.found && !r.versionMismatch)
  if (f === 'warn') return valid.filter((r) => r.found && r.versionMismatch)
  if (f === 'out') return valid.filter((r) => !r.found)
  if (f === 'remark') return valid.filter((r) => r.found && r.remark)
  return valid
}

const filteredResults = computed(() => getFilteredResults())

const totalPages = computed(() => Math.max(1, Math.ceil(filteredResults.value.length / pageSize.value)))

const pageRows = computed(() => {
  const filtered = filteredResults.value
  const total = filtered.length
  const tp = totalPages.value
  const page = Math.min(currentPage.value, tp) || 1
  const start = (page - 1) * pageSize.value
  const end = Math.min(start + pageSize.value, total)
  const out: { idx: number; r: CmaResult }[] = []
  for (let i = start; i < end; i += 1) out.push({ idx: i, r: filtered[i] })
  return out
})

/** 分页页码序列（含省略号），与原 renderPager 规则一致 */
const pageList = computed<(number | string)[]>(() => {
  const tp = totalPages.value
  if (tp <= 1) return []
  const pages: (number | string)[] = []
  const win = 2
  for (let p = 1; p <= tp; p += 1) {
    if (p === 1 || p === tp || (p >= currentPage.value - win && p <= currentPage.value + win)) pages.push(p)
    else if (pages[pages.length - 1] !== '...') pages.push('...')
  }
  return pages
})

const pagerInfo = computed(() =>
  currentFilter.value
    ? `筛选 ${filteredResults.value.length} / ${results.value.length} 条`
    : `共 ${results.value.length} 条`,
)

function setFilter(f: string): void {
  currentFilter.value = currentFilter.value === f ? null : f
  currentPage.value = 1
}

function gotoPage(p: number): void {
  const tp = totalPages.value
  currentPage.value = Math.min(Math.max(1, p), tp)
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function onPageSizeChange(e: Event): void {
  pageSize.value = parseInt((e.target as HTMLSelectElement).value, 10) || 50
  currentPage.value = 1
}

function resetResults(): void {
  results.value = []
  currentFilter.value = null
  currentPage.value = 1
  showResultActions.value = false
  showProgress.value = false
  progressDone.value = 0
  progressTotal.value = 0
  inFlight.value = []
  progressFinished.value = false
  resDisp.value = {}
  resDispSaved.value = {}
}

/* ==================== 核对（5 并发，按输入顺序回填） ==================== */
const progressPercent = computed(() =>
  progressTotal.value ? Math.floor((progressDone.value / progressTotal.value) * 100) : 0,
)
const currentStdText = computed(() => (inFlight.value.length ? `正在查询: ${inFlight.value.join('、')}` : ''))
const progressText = computed(() =>
  progressFinished.value
    ? `核对完成 ${progressTotal.value} / ${progressTotal.value}`
    : `已核对 ${progressDone.value} / ${progressTotal.value}`,
)

async function startCheck(): Promise<void> {
  if (checking.value) return
  const codes = parseCodes(inputText.value)
  if (codes.length === 0) {
    toast('请先输入标准号', 'err')
    return
  }

  checking.value = true
  showProgress.value = true
  showResultActions.value = false
  progressFinished.value = false
  const total = codes.length
  progressTotal.value = total
  progressDone.value = 0
  inFlight.value = []
  currentPage.value = 1
  results.value = new Array(total) // 按输入顺序占位，逐条并发完成后回填，保证顺序稳定

  const flying = new Set<string>() // 正在核对的标准号
  let done = 0
  let idx = 0
  const CONCURRENCY = 5 // 同时核对 5 条，兼顾速度与进度细度

  // ===== 留痕：本次核对 = 1 个台账批次；批次号随每条请求带上，后端据此归集明细 =====
  const batchId = newBatchId()
  lastBatchId.value = batchId
  ledgerTip.value = ''
  resDisp.value = {}
  resDispSaved.value = {}
  saveIdentity()
  const ledgerMeta = {
    batchId,
    operator: operator.value.trim(),
    dept: dept.value.trim(),
    purpose: purpose.value.trim(),
    source: inputSource.value,
  }
  let ledgerError = ''

  function refreshProgress(): void {
    progressDone.value = done
    inFlight.value = [...flying]
  }

  async function worker(): Promise<void> {
    for (;;) {
      const my = idx++ // 取下一个待核对序号（单线程，无竞态）
      if (my >= total) break
      const code = codes[my]
      flying.add(code)
      refreshProgress()
      try {
        const json = await cmaApi.checkOne(code, { ...ledgerMeta, seq: my + 1, total })
        if (json.ledger?.error && !ledgerError) ledgerError = json.ledger.error
        if (json.success) results.value[my] = { ...json.result, _seq: my + 1 }
        else {
          results.value[my] = {
            _input: code,
            _seq: my + 1,
            standardCode: code,
            found: false,
            fuzzy: false,
            versionMismatch: false,
            matchType: '',
            standardMethod: '',
            remark: '',
            error: json.error || '核对失败',
          }
        }
      } catch (e) {
        results.value[my] = {
          _input: code,
          _seq: my + 1,
          standardCode: code,
          found: false,
          fuzzy: false,
          versionMismatch: false,
          matchType: '',
          standardMethod: '',
          remark: '',
          error: (e as Error)?.message || '核对失败',
        }
      } finally {
        flying.delete(code)
        done += 1
        refreshProgress()
      }
    }
  }

  try {
    const workers: Promise<void>[] = []
    const n = Math.min(CONCURRENCY, total)
    for (let i = 0; i < n; i += 1) workers.push(worker())
    await Promise.all(workers)
    results.value = results.value.filter(Boolean) // 理论上已全部回填，保险过滤
    progressFinished.value = true
    showResultActions.value = true
    const outCnt = validResults.value.filter((r) => !r.found).length
    if (outCnt > 0) toast(`核对完成：有 ${outCnt} 条不在清单内，请关注！`, 'err')
    else toast('核对完成：全部在清单内 ✓', 'ok')
    // 台账留痕结果：写入异常必须让使用者知道（否则"以为留了痕其实没留"是合规风险）
    if (ledgerError) {
      ledgerTip.value = `⚠ 台账写入异常：${ledgerError}`
      toast('核对完成，但台账留痕写入异常，请联系管理员检查台账目录', 'err')
    } else {
      ledgerTip.value = `本次核对已记入台账，批次号 ${batchId}`
    }
    await refreshLedger()
  } catch (err) {
    toast(`核对出错：${(err as Error).message}`, 'err')
  } finally {
    checking.value = false
  }
}

/* ==================== 核对台账（留痕）查询与维护 ==================== */
const ledgerEnabled = ref(true)
const ledgerDir = ref('')
const ledgerStats = ref<CmaLedgerStats | null>(null)
const ledgerBatches = ref<CmaLedgerBatch[]>([])
const ledgerTotal = ref(0)
const ledgerPage = ref(1)
const ledgerPageSize = ref(10)
const ledgerLoading = ref(false)
const ledgerF = ref({ start: '', end: '', keyword: '', conclusion: '', operator: '', pending: false })
const ledgerExpanded = ref('')
const ledgerItems = ref<CmaLedgerItem[]>([])
const ledgerItemsLoading = ref(false)
const ledgerNoteDraft = ref('')
const ledgerNoteSaving = ref(false)

/* ---- 逐条处理意见（版本待确认 / 不在清单内 / 有备注 三类必须填写） ---- */
/** 当前核对结果表里各条的处理意见（key = 批次内序号） */
const resDisp = ref<Record<number, string>>({})
/** 已保存成功的序号（用于把"待处理"变成"已填写"） */
const resDispSaved = ref<Record<number, boolean>>({})
/** 台账明细里各条的处理意见草稿（key = `${batchId}:${seq}`） */
const ledgerDisp = ref<Record<string, string>>({})
const savingDisp = ref(false)

/** 历史候选（全台账跨批次累积）：逐条处理意见 / 批次处置说明 —— 供下拉直接选取 */
const dispHistory = ref<{ text: string; uses: number }[]>([])
const noteHistory = ref<{ text: string; uses: number }[]>([])

async function loadHistory(): Promise<void> {
  try {
    const res = await cmaApi.ledgerHistory('all')
    if (res.success) {
      dispHistory.value = (res.dispositions || []).map((x) => ({ text: x.text, uses: x.uses }))
      noteHistory.value = (res.notes || []).map((x) => ({ text: x.text, uses: x.uses }))
    }
  } catch {
    /* 历史候选加载失败不影响手输与保存 */
  }
}

/** 该条是否需要处理意见：不在清单内 / 版本待确认 / 在清单内但有平台备注 */
function needDisposition(r: CmaResult): boolean {
  if (r.error) return false
  if (!r.found) return true
  if (r.versionMismatch) return true
  return !!r.remark
}

function dispKey(batchId: string, seq: number): string {
  return `${batchId}:${seq}`
}

/** 当前核对结果中"需要处理但还没填"的条数 */
const resPendingCount = computed(
  () => validResults.value.filter((r) => needDisposition(r) && !(resDisp.value[r._seq || 0] || '').trim()).length,
)
/** 当前核对结果中"需要处理"的条数 */
const resNeedCount = computed(() => validResults.value.filter((r) => needDisposition(r)).length)

const ledgerTotalPages = computed(() => Math.max(1, Math.ceil(ledgerTotal.value / ledgerPageSize.value)))

/* ---- 批次明细视图（全部 / 只看需处理 / 只看待处理） ----
 * 过滤与计数一律基于**已保存的 disposition**，绝不用输入框草稿：
 * 否则在「只看待处理」视图里一打字，该行就被判定为"已填"而从列表消失、输入框被抽走，根本没法填。
 * ---- */
const detailFilter = ref<'all' | 'need' | 'pending'>('all')

function isNeed(it: CmaLedgerItem): boolean {
  return Number(it.need_disposition || 0) === 1
}

function isPending(it: CmaLedgerItem): boolean {
  return isNeed(it) && !(it.disposition || '').trim()
}

const detailNeedCount = computed(() => ledgerItems.value.filter(isNeed).length)
const detailPendingCount = computed(() => ledgerItems.value.filter(isPending).length)

const detailItems = computed(() => {
  if (detailFilter.value === 'need') return ledgerItems.value.filter(isNeed)
  if (detailFilter.value === 'pending') return ledgerItems.value.filter(isPending)
  return ledgerItems.value
})

/** 草稿值与已存值不同、且草稿非空 = "填了但还没保存"的条数 */
const detailDraftCount = computed(
  () =>
    ledgerItems.value.filter((i) => {
      if (!isNeed(i)) return false
      const draft = (ledgerDisp.value[dispKey(i.batch_id, i.seq)] ?? '').trim()
      return !!draft && draft !== (i.disposition || '').trim()
    }).length,
)
const ledgerCountText = computed(() => (ledgerTotal.value ? `共 ${ledgerTotal.value} 个核对批次` : '暂无台账记录'))
const ledgerDirText = computed(() =>
  ledgerEnabled.value
    ? `台账文件：${ledgerDir.value || '（后端未返回路径）'}`
    : '⚠ 台账功能未启用（后端 CMA_LEDGER_ENABLED=false）',
)

function ledgerQuery(): {
  start: string
  end: string
  keyword: string
  conclusion: string
  operator: string
  pending: string
} {
  return {
    start: ledgerF.value.start,
    end: ledgerF.value.end,
    keyword: ledgerF.value.keyword.trim(),
    conclusion: ledgerF.value.conclusion,
    operator: ledgerF.value.operator.trim(),
    pending: ledgerF.value.pending ? '1' : '',
  }
}

async function loadLedger(): Promise<void> {
  ledgerLoading.value = true
  try {
    const res = await cmaApi.ledgerBatches(ledgerPage.value, ledgerPageSize.value, ledgerQuery())
    if (res.success) {
      ledgerBatches.value = res.batches
      ledgerTotal.value = res.total
      if (ledgerPage.value > ledgerTotalPages.value) {
        // 筛选后当前页越界（如删无可删但总数变小）：退回最后一页重取
        ledgerPage.value = ledgerTotalPages.value
        const again = await cmaApi.ledgerBatches(ledgerPage.value, ledgerPageSize.value, ledgerQuery())
        if (again.success) ledgerBatches.value = again.batches
      }
    } else {
      ledgerBatches.value = []
      ledgerTotal.value = 0
      ledgerEnabled.value = false
    }
  } catch (e) {
    toast(`台账读取失败：${(e as Error).message}`, 'err')
  } finally {
    ledgerLoading.value = false
  }
}

async function loadLedgerStats(): Promise<void> {
  try {
    const res = await cmaApi.ledgerStats()
    if (res.success) {
      ledgerStats.value = res.data
      ledgerDir.value = res.data.dir
      ledgerEnabled.value = res.data.enabled
    }
  } catch {
    /* 统计失败不影响核对主流程 */
  }
}

async function refreshLedger(): Promise<void> {
  await Promise.all([loadLedger(), loadLedgerStats()])
  // 「只看待处理」是闭环过程中的临时筛选：一旦已无待处理，自动解除——
  // 否则填完后列表会一直空着，看起来像"台账没了"。
  if (ledgerF.value.pending && Number(ledgerStats.value?.pending_cnt ?? 0) === 0) {
    ledgerF.value.pending = false
    await loadLedger()
  }
}

async function searchLedger(): Promise<void> {
  ledgerPage.value = 1
  ledgerExpanded.value = ''
  await refreshLedger()
}

async function resetLedgerFilter(): Promise<void> {
  ledgerF.value = { start: '', end: '', keyword: '', conclusion: '', operator: '', pending: false }
  await searchLedger()
}

async function ledgerGoPage(p: number): Promise<void> {
  ledgerPage.value = Math.min(Math.max(1, p), ledgerTotalPages.value)
  await loadLedger()
}

function onLedgerPageSize(e: Event): void {
  ledgerPageSize.value = parseInt((e.target as HTMLSelectElement).value, 10) || 10
  ledgerPage.value = 1
  void loadLedger()
}

async function toggleLedgerDetail(b: CmaLedgerBatch): Promise<void> {
  if (ledgerExpanded.value === b.batch_id) {
    ledgerExpanded.value = ''
    return
  }
  ledgerExpanded.value = b.batch_id
  ledgerItems.value = []
  ledgerNoteDraft.value = b.note || ''
  ledgerItemsLoading.value = true
  try {
    const res = await cmaApi.ledgerBatch(b.batch_id)
    if (res.success) {
      ledgerItems.value = res.items
      res.items.forEach((i) => {
        ledgerDisp.value[dispKey(i.batch_id, i.seq)] = i.disposition || ''
      })
      // 默认视图：还有待处理就直接呈现要填的行（几百条明细里翻找 19 条才是真痛点）
      const pend = res.items.filter(
        (i) => Number(i.need_disposition || 0) === 1 && !(i.disposition || '').trim(),
      ).length
      detailFilter.value = pend > 0 ? 'pending' : 'all'
    } else toast(res.error || '批次明细读取失败', 'err')
  } catch (e) {
    toast(`批次明细读取失败：${(e as Error).message}`, 'err')
  } finally {
    ledgerItemsLoading.value = false
  }
}

async function saveLedgerNote(b: CmaLedgerBatch): Promise<void> {
  ledgerNoteSaving.value = true
  try {
    const res = await cmaApi.ledgerNote(b.batch_id, ledgerNoteDraft.value)
    if (res.success) {
      b.note = ledgerNoteDraft.value
      toast('处置说明已记录', 'ok')
      void loadHistory()
    } else {
      toast(res.error || '处置说明保存失败', 'err')
    }
  } catch (e) {
    toast(`处置说明保存失败：${(e as Error).message}`, 'err')
  } finally {
    ledgerNoteSaving.value = false
  }
}

/* ---- 逐条处理意见：保存（结果表 / 台账明细共用同一后端接口） ---- */

/** 保存结果表里某一条的处理意见 */
async function saveResDisposition(r: CmaResult): Promise<void> {
  const seq = r._seq || 0
  if (!seq || !lastBatchId.value) {
    toast('批次信息缺失，无法保存处理意见', 'err')
    return
  }
  const text = (resDisp.value[seq] || '').trim()
  if (!text) {
    toast('请先填写处理意见', 'err')
    return
  }
  savingDisp.value = true
  try {
    const res = await cmaApi.ledgerDispositions(lastBatchId.value, [{ seq, disposition: text }])
    if (res.success && (res.saved || []).includes(seq)) {
      resDispSaved.value[seq] = true
      toast(`第 ${seq} 条处理意见已保存`, 'ok')
      await refreshLedger()
      void loadHistory()
    } else {
      toast(res.error || res.failed?.[0]?.error || '处理意见保存失败', 'err')
    }
  } catch (e) {
    toast(`处理意见保存失败：${(e as Error).message}`, 'err')
  } finally {
    savingDisp.value = false
  }
}

/** 保存结果表里全部已填写的处理意见 */
async function saveAllResDispositions(): Promise<void> {
  if (!lastBatchId.value) {
    toast('批次信息缺失，无法保存处理意见', 'err')
    return
  }
  const items = validResults.value
    .filter((r) => needDisposition(r) && (resDisp.value[r._seq || 0] || '').trim())
    .map((r) => ({ seq: r._seq || 0, disposition: (resDisp.value[r._seq || 0] || '').trim() }))
    .filter((x) => x.seq > 0)
  if (items.length === 0) {
    toast(resNeedCount.value > 0 ? '还没有填写任何处理意见' : '本次核对没有需要处理的条目', 'info')
    return
  }
  savingDisp.value = true
  try {
    const res = await cmaApi.ledgerDispositions(lastBatchId.value, items)
    if (res.success) {
      ;(res.saved || []).forEach((s) => {
        resDispSaved.value[s] = true
      })
      const rest = res.pending_cnt ?? 0
      if (rest > 0) toast(`已保存 ${(res.saved || []).length} 条，本批次还有 ${rest} 条待处理`, 'err')
      else toast(`已保存 ${(res.saved || []).length} 条，本批次处理意见已闭环 ✓`, 'ok')
      await refreshLedger()
      void loadHistory()
    } else {
      toast(res.error || '处理意见保存失败', 'err')
    }
  } catch (e) {
    toast(`处理意见保存失败：${(e as Error).message}`, 'err')
  } finally {
    savingDisp.value = false
  }
}

/** 保存台账明细里某一条的处理意见（事后补填/修改） */
async function saveItemDisposition(it: CmaLedgerItem): Promise<void> {
  const key = dispKey(it.batch_id, it.seq)
  const text = (ledgerDisp.value[key] ?? it.disposition ?? '').trim()
  if (!text) {
    toast('请先填写处理意见', 'err')
    return
  }
  savingDisp.value = true
  try {
    const res = await cmaApi.ledgerDispositions(it.batch_id, [{ seq: it.seq, disposition: text }])
    if (res.success && (res.saved || []).includes(it.seq)) {
      it.disposition = text
      toast(`第 ${it.seq} 条处理意见已保存`, 'ok')
      await refreshLedger()
      void loadHistory()
    } else {
      toast(res.error || res.failed?.[0]?.error || '处理意见保存失败', 'err')
    }
  } catch (e) {
    toast(`处理意见保存失败：${(e as Error).message}`, 'err')
  } finally {
    savingDisp.value = false
  }
}

/** 批量保存本批次"已填写但未保存"的处理意见（一条也能提，多条一次提交） */
async function saveAllItemDispositions(): Promise<void> {
  const changed = ledgerItems.value.filter((i) => {
    if (!isNeed(i)) return false
    const draft = (ledgerDisp.value[dispKey(i.batch_id, i.seq)] ?? '').trim()
    return !!draft && draft !== (i.disposition || '').trim()
  })
  if (changed.length === 0) {
    toast('没有待保存的处理意见（填写后与已保存内容不同才会提交）', 'info')
    return
  }
  const batchId = changed[0].batch_id
  savingDisp.value = true
  try {
    const res = await cmaApi.ledgerDispositions(
      batchId,
      changed.map((i) => ({ seq: i.seq, disposition: (ledgerDisp.value[dispKey(i.batch_id, i.seq)] || '').trim() })),
    )
    if (res.success) {
      ;(res.saved || []).forEach((s) => {
        const it = ledgerItems.value.find((x) => x.seq === s)
        if (it) it.disposition = (ledgerDisp.value[dispKey(it.batch_id, it.seq)] || '').trim()
      })
      const rest = res.pending_cnt ?? 0
      if (rest > 0) toast(`已保存 ${(res.saved || []).length} 条，本批次还有 ${rest} 条待处理`, 'err')
      else toast(`已保存 ${(res.saved || []).length} 条，本批次处理意见已闭环 ✓`, 'ok')
      await refreshLedger()
      void loadHistory()
    } else {
      toast(res.error || '处理意见保存失败', 'err')
    }
  } catch (e) {
    toast(`处理意见保存失败：${(e as Error).message}`, 'err')
  } finally {
    savingDisp.value = false
  }
}

/** 结论 → 徽标样式（与核对结果配色语义一致） */
function conclusionCls(c: string): string {
  if (c === '在清单内') return 'badge in'
  if (c === '版本不符·待确认') return 'badge warn'
  if (c === '不在清单内') return 'badge out'
  return 'badge fuzzy'
}

/** 耗时：批次未完成（被中断）才显示"—"；已完成批次即使不足 1 秒也照实显示 0.x 秒——
 *  留痕里"耗时 —"会被误读成"没有记录"，而它其实是有值的。 */
function fmtDuration(b: CmaLedgerBatch): string {
  if (!b.finished_at) return '—'
  return `${(Math.max(0, Number(b.duration_ms) || 0) / 1000).toFixed(1)}s`
}

function fmtRate(v: number): string {
  return `${Number(v || 0).toFixed(1)}%`
}

/** 批次状态：明细条数未达应有条数说明核对被中断（关页面 / 断网）——留痕里必须看得出来 */
function batchState(b: CmaLedgerBatch): string {
  if (b.expected > 0 && b.total < b.expected) return `未完成（${b.total}/${b.expected}）`
  return '已完成'
}

/** 结果表单元格里显示的处理意见文本（未填返回空串） */
function resDispText(r: CmaResult): string {
  return (resDisp.value[r._seq || 0] || '').trim()
}

/* ==================== 导出 Excel（逐字保留配色与列宽） ==================== */
async function exportExcel(): Promise<void> {
  if (results.value.length === 0) {
    toast('没有可导出的数据', 'err')
    return
  }
  // 这是"当次核对结果"的原始凭证，不阻断导出；但未填处理意见必须提醒（台账导出才是硬门槛）
  if (resPendingCount.value > 0) {
    toast(`注意：本批次还有 ${resPendingCount.value} 条需处理但未填处理意见`, 'err')
  }
  const wb = new ExcelJS.Workbook()
  const ws = wb.addWorksheet('标准核对结果')
  ws.columns = [
    { header: '#', width: 6 },
    { header: '输入标准号', width: 18 },
    { header: '平台标准号', width: 18 },
    { header: '标准名称', width: 60 },
    { header: '平台备注', width: 50 },
    { header: '核对结果', width: 14 },
    { header: '匹配方式', width: 16 },
    { header: '处理意见', width: 40 },
  ]
  ws.getRow(1).font = { bold: true }
  ws.getRow(1).alignment = { vertical: 'middle' }
  let inCnt = 0
  let outCnt = 0
  let warnCnt = 0
  ;(results.value.filter(Boolean) as CmaResult[]).forEach((r, i) => {
    const found = r.found
    const vm = found && r.versionMismatch
    if (found && !vm) inCnt += 1
    else if (vm) warnCnt += 1
    else outCnt += 1
    const resultText = vm ? '版本不符·待确认' : found ? '在清单内' : '不在清单内'
    const row = ws.addRow([
      i + 1,
      r._input || r.standardCode,
      r.standardCode || '',
      r.standardMethod || '',
      found ? r.remark || '' : '',
      resultText,
      r.matchType || r.error || '',
      resDispText(r),
    ])
    row.getCell(5).fill = {
      type: 'pattern',
      pattern: 'solid',
      fgColor: { argb: found ? (vm ? 'FFFFF1D6' : 'FFE8F5E9') : 'FFFEF0EF' },
    }
    row.getCell(5).font = {
      color: { argb: found ? (vm ? 'FFD48806' : 'FF27AE60') : 'FFE74C3C' },
      bold: true,
    }
    if (vm) {
      row.getCell(6).font = { color: { argb: 'FFD48806' }, bold: true }
    }
    // 需处理但未填意见 → 标黄提示（原始凭证允许导出，但要让看的人立刻发现未闭环）
    if (needDisposition(r) && !resDispText(r)) {
      row.getCell(8).fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFFFF1D6' } }
      row.getCell(8).font = { color: { argb: 'FFD48806' }, bold: true }
    }
  })
  ws.addRow([])
  ws.addRow([
    '汇总',
    `总数: ${results.value.length}`,
    `在清单内: ${inCnt}`,
    `版本待确认: ${warnCnt}`,
    `不在清单内: ${outCnt}`,
    `需处理: ${resNeedCount.value}`,
    `待填处理意见: ${resPendingCount.value}`,
  ])
  const buf = await wb.xlsx.writeBuffer()
  const blob = new Blob([buf], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
  a.href = url
  a.download = `标准资质核对_${ts}.xlsx`
  a.click()
  URL.revokeObjectURL(url)
  toast('Excel 已导出', 'ok')
}

/* ==================== 导出台账 Excel（体系运行记录，可直接归档） ==================== */
async function exportLedger(): Promise<void> {
  try {
    // ===== 强制闭环：只要还有应填未填的处理意见，就不允许导出台账 =====
    // （按"全量台账"判断，而不是当前筛选：筛选只影响导出范围，不该成为绕过闭环的手段）
    const st = await cmaApi.ledgerStats()
    const pendingAll = st.success ? Number(st.data.pending_cnt || 0) : 0
    if (pendingAll > 0) {
      toast(`还有 ${pendingAll} 条需处理但未填处理意见，填完才能导出台账`, 'err')
      ledgerF.value.pending = true
      ledgerExpanded.value = ''
      await searchLedger()
      return
    }
    // 已全部闭环：把上次自动勾上的「只看待处理」放开，否则导出会被自己的筛选清空
    if (ledgerF.value.pending) {
      ledgerF.value.pending = false
      await loadLedger()
    }

    const res = await cmaApi.ledgerExport(ledgerQuery())
    if (!res.success) {
      toast(res.error || '台账导出失败', 'err')
      return
    }
    if (!res.batches.length) {
      toast('当前筛选条件下没有台账记录', 'info')
      return
    }
    const wb = new ExcelJS.Workbook()
    wb.creator = 'AI工具合集 · 一单一库核对'

    /* ---- 表1：批次汇总 ---- */
    const s1 = wb.addWorksheet('批次汇总')
    s1.columns = [
      { header: '批次号', width: 24 },
      { header: '核对时间', width: 20 },
      { header: '完成时间', width: 20 },
      { header: '操作人', width: 12 },
      { header: '部门', width: 16 },
      { header: '核对用途', width: 24 },
      { header: '输入来源', width: 12 },
      { header: '客户端IP', width: 16 },
      { header: '核对条数', width: 10 },
      { header: '在清单内', width: 10 },
      { header: '版本待确认', width: 12 },
      { header: '不在清单内', width: 12 },
      { header: '查询异常', width: 10 },
      { header: '有备注', width: 9 },
      { header: '需处理', width: 9 },
      { header: '待处理', width: 9 },
      { header: '覆盖率', width: 10 },
      { header: '耗时(秒)', width: 10 },
      { header: '状态', width: 16 },
      { header: '处置/复核说明', width: 40 },
    ]
    s1.getRow(1).font = { bold: true }
    s1.getRow(1).alignment = { vertical: 'middle', horizontal: 'center' }
    res.batches.forEach((b) => {
      const row = s1.addRow([
        b.batch_id,
        b.checked_at,
        b.finished_at || '',
        b.operator,
        b.dept,
        b.purpose,
        b.source,
        b.client_ip,
        b.total,
        b.in_cnt,
        b.warn_cnt,
        b.out_cnt,
        b.err_cnt,
        b.remark_cnt,
        b.need_cnt,
        b.pending_cnt,
        `${Number(b.cover_rate || 0).toFixed(1)}%`,
        b.finished_at ? Number((b.duration_ms / 1000).toFixed(1)) : '',
        batchState(b),
        b.note || '',
      ])
      // 风险项标红：归档页一眼看出哪些批次有待处置项
      if (b.out_cnt > 0 || b.err_cnt > 0) row.getCell(12).font = { color: { argb: 'FFE74C3C' }, bold: true }
      if (b.expected > 0 && b.total < b.expected) row.getCell(19).font = { color: { argb: 'FFD48806' }, bold: true }
      // 未闭环处理意见必须显眼：导出门槛是 0，能导出的文件理论上不该有值（防御性着色）
      if (b.pending_cnt > 0) row.getCell(16).font = { color: { argb: 'FFE74C3C' }, bold: true }
    })
    if (res.batches.length > 1) {
      const sum = res.batches.reduce(
        (a, b) => ({
          total: a.total + b.total,
          inList: a.inList + b.in_cnt,
          warn: a.warn + b.warn_cnt,
          out: a.out + b.out_cnt,
          err: a.err + b.err_cnt,
          remark: a.remark + b.remark_cnt,
          need: a.need + b.need_cnt,
          pending: a.pending + b.pending_cnt,
        }),
        { total: 0, inList: 0, warn: 0, out: 0, err: 0, remark: 0, need: 0, pending: 0 },
      )
      const r = s1.addRow([
        '合计', '', '', '', '', '', '', '', sum.total, sum.inList, sum.warn, sum.out, sum.err, sum.remark, sum.need, sum.pending,
      ])
      r.font = { bold: true }
    }

    /* ---- 表2：核对明细 ---- */
    const s2 = wb.addWorksheet('核对明细')
    s2.columns = [
      { header: '批次号', width: 24 },
      { header: '序号', width: 6 },
      { header: '核对时间', width: 20 },
      { header: '操作人', width: 12 },
      { header: '输入标准号', width: 20 },
      { header: '平台标准号', width: 20 },
      { header: '标准名称', width: 56 },
      { header: '结论', width: 16 },
      { header: '匹配方式', width: 16 },
      { header: '平台备注', width: 40 },
      { header: '异常信息', width: 24 },
      { header: '需处理', width: 9 },
      { header: '处理意见', width: 46 },
    ]
    s2.getRow(1).font = { bold: true }
    s2.getRow(1).alignment = { vertical: 'middle', horizontal: 'center' }
    const opMap = new Map(res.batches.map((b) => [b.batch_id, b.operator]))
    res.items.forEach((it) => {
      const need = Number(it.need_disposition || 0) === 1
      const row = s2.addRow([
        it.batch_id,
        it.seq,
        it.checked_at,
        opMap.get(it.batch_id) || '',
        it.input_code,
        it.platform_code,
        it.standard_method,
        it.conclusion,
        it.match_type || '',
        it.remark || '',
        it.error || '',
        need ? '是' : '',
        it.disposition || '',
      ])
      const cell = row.getCell(8)
      if (it.conclusion === '在清单内') cell.font = { color: { argb: 'FF27AE60' }, bold: true }
      else if (it.conclusion === '不在清单内') cell.font = { color: { argb: 'FFE74C3C' }, bold: true }
      else cell.font = { color: { argb: 'FFD48806' }, bold: true }
      // 需处理但没写意见 → 红字提示（导出本应被阻止，这里是兜底显示）
      if (need && !it.disposition) row.getCell(13).font = { color: { argb: 'FFE74C3C' }, bold: true }
      else if (it.disposition) row.getCell(13).font = { color: { argb: 'FF27AE60' } }
    })

    const buf = await wb.xlsx.writeBuffer()
    const blob = new Blob([buf], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
    a.href = url
    a.download = `一单一库核对台账_${ts}.xlsx`
    a.click()
    URL.revokeObjectURL(url)
    toast(`台账已导出（${res.batches.length} 个批次 / ${res.items.length} 条明细）`, 'ok')
  } catch (e) {
    toast(`台账导出失败：${(e as Error).message}`, 'err')
  }
}

/* ==================== 复制不在清单的标准号 ==================== */
async function copyOut(): Promise<void> {
  const out = validResults.value.filter((r) => !r.found).map((r) => r._input || r.standardCode)
  if (out.length === 0) {
    toast('没有不在清单内的标准', 'info')
    return
  }
  const text = out.join('\n')
  try {
    await navigator.clipboard.writeText(text)
    toast(`已复制 ${out.length} 条不在清单内的标准号`, 'ok')
  } catch {
    // 回退方案
    const ta = document.createElement('textarea')
    ta.value = text
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    ta.remove()
    toast(`已复制 ${out.length} 条`, 'ok')
  }
}

/* ==================== 行样式（与原 renderRow 一致） ==================== */
function rowClass(r: CmaResult): string {
  const hasRemark = !!(r.found && r.remark)
  const vm = !!(r.found && r.versionMismatch)
  let cls = 'out'
  if (r.found && !vm) cls = 'in'
  else if (vm) cls = 'warn'
  return cls + (hasRemark ? ' has-remark' : '')
}

function resultBadge(r: CmaResult): { text: string; cls: string } {
  const vm = !!(r.found && r.versionMismatch)
  if (vm) return { text: '版本不符·待确认', cls: 'badge warn' }
  return r.found ? { text: '在清单内', cls: 'badge in' } : { text: '不在清单内', cls: 'badge out' }
}

function resultNote(r: CmaResult): { text: string; cls: string } {
  const vm = !!(r.found && r.versionMismatch)
  if (r.error) return { text: r.error, cls: 'note-fuzzy' }
  if (vm) return { text: `版本不符·待确认（${r.matchType}）`, cls: 'note-warn' }
  if (r.fuzzy) return { text: r.matchType, cls: 'note-fuzzy' }
  return { text: '精确匹配', cls: '' }
}

/* ==================== 初始化 ==================== */
onMounted(async () => {
  loadIdentity()
  await Promise.all([refreshLedger(), loadHistory()])
})
</script>

<template>
  <div class="v-cma">
    <div class="loading" :class="{ show: false }"><div class="spinner"></div></div>
    <div class="toast-box">
      <div v-for="t in toasts" :key="t.id" class="toast" :class="t.type">{{ t.msg }}</div>
    </div>

    <div class="header">
      <h1>检测标准资质核对工具</h1>
      <div class="links">
        <a href="https://cma.caqit.org.cn/#/home" target="_blank" rel="noopener">一单一库平台 ↗</a>
      </div>
    </div>

    <div class="main">
      <!-- 说明 -->
      <div class="card">
        <div class="card-header"><h3>📋 使用说明</h3></div>
        <div class="card-body">
          <p style="font-size:13px; line-height:1.8; color:var(--text2);">
            将实验室<strong>实际使用的检测标准号</strong>粘贴或上传到下方（每行一个），工具会逐条去
            <strong>"一单一库"平台</strong>（检验检测机构资质认定能力项目库管理平台 cma.caqit.org.cn）核对，
            判断该标准是否在资质认定清单内。<br />
            核对规则：先按<strong>标准号精确匹配</strong>（含去除多余空格），命中即视为<strong>在清单内</strong>（绿色）。
            若精确未命中，自动做<strong>模糊回退</strong>（去空格、去版本号年份后再比对），
            但<strong>仅当平台返回的标准号与输入"主体一致（标准号相同）"才视为匹配</strong>。
            ⚠️ 若主体相同但<strong>版本（年份，如 2022）不一致</strong>，结果标记为橙色<strong>"版本不符·待确认"</strong>，
            不直接算在清单内，需人工核对实际版本是否可用；若<strong>标准号主体不同</strong>（如 41906 与 4996），则视为<strong>不在清单内</strong>（红色）。
            命中时表格的<strong>"平台备注"</strong>列会抓取并展示该标准在平台上的备注内容（如有限制说明、限制范围等）。
          </p>
        </div>
      </div>

      <!-- 核对留痕信息 -->
      <div class="card">
        <div class="card-header">
          <h3>🧾 核对留痕信息（写入台账）</h3>
          <span class="hint">{{ identityText }}</span>
        </div>
        <div class="card-body">
          <div class="field-row">
            <label class="field">
              <span>操作人</span>
              <input v-model="operator" class="inp" placeholder="姓名" @change="saveIdentity" />
            </label>
            <label class="field">
              <span>部门</span>
              <input v-model="dept" class="inp" placeholder="如：检测中心" @change="saveIdentity" />
            </label>
            <label class="field wide">
              <span>核对用途</span>
              <input v-model="purpose" class="inp" placeholder="如：资质扩项前自查 / 年度内审 / 日常监督" @change="saveIdentity" />
            </label>
          </div>
          <div class="hint">
            每次核对都会在服务端自动留痕（<b>批次 + 逐条明细</b>），记录时间、操作人、部门、用途、输入来源、条数、结论分布、覆盖率与耗时，
            用于证明检测能力<b>持续符合</b>、管理体系<b>持续有效运行</b>；台账<b>只增不删</b>，请如实填写操作人。
            <span v-if="lastBatchId">上次批次号：<b>{{ lastBatchId }}</b></span>
            <span v-if="ledgerTip"> · {{ ledgerTip }}</span>
          </div>
        </div>
      </div>

      <!-- 输入 -->
      <div class="card">
        <div class="card-header">
          <h3>📥 输入检测标准清单</h3>
          <span class="hint">{{ inputCountText }}</span>
        </div>
        <div class="card-body">
          <textarea
            v-model="inputText"
            placeholder="每行一个标准号，例如：&#10;GB 2749-2015&#10;GB 5009.12-2023&#10;GB/T 5009.1-2003"
            @input="inputSource = '手工输入'"
          ></textarea>
          <div class="toolbar">
            <button class="btn btn-primary" :disabled="checking" @click="startCheck">▶ 开始核对</button>
            <button class="btn btn-outline" @click="fileInput?.click()">📁 上传文件</button>
            <button class="btn btn-success" @click="abilityFileInput?.click()">📊 能力表导入</button>
            <button class="btn btn-outline" @click="fillSample">✨ 填充示例</button>
            <button class="btn btn-outline" @click="clearAll">🗑 清空</button>
            <input ref="fileInput" type="file" accept=".txt,.csv,.json,.xlsx" style="display:none;" @change="onFileChange" />
            <input ref="abilityFileInput" type="file" accept=".xlsx" style="display:none;" @change="onAbilityFileChange" />
          </div>
          <div class="hint">
            支持上传 .txt / .csv / .json / .xlsx 文件；.csv 自动取每行第一个字段；.xlsx 优先读取表头为<strong>"编号（含年号）"</strong>的列内容（找不到该列时，自动识别含标准号的列；仅支持 .xlsx，老版 .xls 请另存为 .xlsx）；读取内容会自动忽略空白并去重后核对。若上传的是带"编号（含年号）"列的复杂能力表，可直接点 <strong>📊 能力表导入</strong> 按钮。
          </div>
        </div>
      </div>

      <!-- 结果 -->
      <div class="card">
        <div class="card-header">
          <h3>📊 核对结果</h3>
          <span class="hint">{{ rateText }}</span>
        </div>
        <div class="card-body">
          <div class="stats">
            <div class="stat total" :class="{ active: currentFilter === 'total' }" data-filter="total" @click="setFilter('total')"><div class="num">{{ stats.total }}</div><div class="lbl">核对总数</div></div>
            <div class="stat ok" :class="{ active: currentFilter === 'in' }" data-filter="in" @click="setFilter('in')"><div class="num">{{ stats.inCnt }}</div><div class="lbl">在清单内</div></div>
            <div class="stat warn" :class="{ active: currentFilter === 'warn' }" data-filter="warn" @click="setFilter('warn')"><div class="num">{{ stats.warnCnt }}</div><div class="lbl">版本待确认</div></div>
            <div class="stat no" :class="{ active: currentFilter === 'out' }" data-filter="out" @click="setFilter('out')"><div class="num">{{ stats.outCnt }}</div><div class="lbl">不在清单内</div></div>
            <div class="stat remark" :class="{ active: currentFilter === 'remark' }" data-filter="remark" @click="setFilter('remark')"><div class="num">{{ stats.remarkCnt }}</div><div class="lbl">有备注</div></div>
          </div>

          <div class="progress-wrap" :class="{ show: showProgress }">
            <div class="progress-bar"><div class="progress-fill" :style="{ width: progressPercent + '%' }"></div></div>
            <div class="progress-text">{{ progressText }}</div>
            <div class="current-std">{{ currentStdText }}</div>
          </div>

          <div class="result-actions" :style="showResultActions ? undefined : { display: 'none' }">
            <button class="btn btn-success btn-sm" @click="exportExcel">⬇ 导出 Excel</button>
            <button class="btn btn-outline btn-sm" @click="copyOut">📋 复制不在清单的标准号</button>
            <button
              class="btn btn-primary btn-sm"
              :disabled="savingDisp || resNeedCount === 0"
              @click="saveAllResDispositions"
            >💬 保存全部处理意见（需处理 {{ resNeedCount }} · 未填 {{ resPendingCount }}）</button>
            <span class="pager-info" style="margin-left:auto;">每页
              <select :value="pageSize" class="page-size" @change="onPageSizeChange">
                <option :value="20">20</option>
                <option :value="50">50</option>
                <option :value="100">100</option>
                <option :value="200">200</option>
              </select> 条</span>
          </div>

          <table>
            <thead>
              <tr>
                <th style="width:44px;">#</th>
                <th style="white-space:nowrap;">输入标准号</th>
                <th style="white-space:nowrap;">平台标准号</th>
                <th style="white-space:nowrap;min-width:120px;">标准名称</th>
                <th>平台备注</th>
                <th style="width:120px;">核对结果</th>
                <th style="width:130px;">匹配方式</th>
                <th style="min-width:240px;">处理意见（版本待确认 / 不在清单内 / 有备注 必填）</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="pageRows.length === 0">
                <td colspan="8" class="empty">
                  {{ currentFilter ? '无匹配的记录，点击统计卡片可清除筛选。' : '暂无数据，请输入标准号后点击"开始核对"。' }}
                </td>
              </tr>
              <tr v-for="row in pageRows" :key="row.idx" :class="rowClass(row.r)">
                <td>{{ row.idx + 1 }}</td>
                <td class="code-cell">{{ row.r._input || row.r.standardCode }}</td>
                <td class="code-cell">{{ row.r.standardCode || '' }}</td>
                <td>{{ row.r.standardMethod || '' }}</td>
                <td>
                  <template v-if="row.r.found && row.r.remark">
                    <span class="badge remark" :title="row.r.remark">有备注</span>
                    <span class="remark-text" :title="row.r.remark">{{ row.r.remark }}</span>
                  </template>
                  <span v-else style="color:var(--text2);">—</span>
                </td>
                <td><span :class="resultBadge(row.r).cls">{{ resultBadge(row.r).text }}</span></td>
                <td>
                  <span v-if="resultNote(row.r).cls" :class="resultNote(row.r).cls">{{ resultNote(row.r).text }}</span>
                  <template v-else>{{ resultNote(row.r).text }}</template>
                </td>
                <td>
                  <div v-if="needDisposition(row.r)" class="disp-cell">
                    <CmaDispPicker
                      v-model="resDisp[row.r._seq as number]"
                      :options="dispHistory"
                      placeholder="如：已换版为 GB xxx-2025 / 已停止出具该项目报告"
                      empty-text="暂无历史处理意见：先手动填写并保存，下次就能从这里直接选。"
                    />
                    <button
                      class="btn btn-primary btn-sm"
                      :disabled="savingDisp"
                      @click="saveResDisposition(row.r)"
                    >{{ resDispSaved[row.r._seq as number] ? '已保存✓' : '保存' }}</button>
                  </div>
                  <span v-else style="color:var(--text2);">—</span>
                </td>
              </tr>
            </tbody>
          </table>

          <div class="pager" :style="totalPages > 1 ? undefined : { display: 'none' }">
            <template v-if="totalPages > 1">
              <span class="pager-info">{{ pagerInfo }} · 第 {{ currentPage }}/{{ totalPages }} 页</span>
              <button :disabled="currentPage === 1" @click="gotoPage(currentPage - 1)">上一页</button>
              <template v-for="(p, i) in pageList" :key="`${p}-${i}`">
                <span v-if="p === '...'" class="ellipsis">…</span>
                <button v-else :class="{ active: p === currentPage }" @click="gotoPage(p as number)">{{ p }}</button>
              </template>
              <button :disabled="currentPage === totalPages" @click="gotoPage(currentPage + 1)">下一页</button>
            </template>
          </div>
        </div>
      </div>

      <!-- 核对台账（留痕） -->
      <div class="card">
        <div class="card-header">
          <h3>📒 核对台账（留痕记录）</h3>
          <span class="hint">{{ ledgerCountText }} · {{ ledgerDirText }}</span>
        </div>
        <div class="card-body">
          <div class="stats">
            <div class="stat total"><div class="num">{{ ledgerStats?.batches ?? 0 }}</div><div class="lbl">累计核对批次</div></div>
            <div class="stat total"><div class="num">{{ ledgerStats?.items ?? 0 }}</div><div class="lbl">累计核对条目</div></div>
            <div class="stat ok"><div class="num">{{ ledgerStats?.in_cnt ?? 0 }}</div><div class="lbl">累计在清单内</div></div>
            <div class="stat warn"><div class="num">{{ ledgerStats?.warn_cnt ?? 0 }}</div><div class="lbl">累计版本待确认</div></div>
            <div class="stat no"><div class="num">{{ ledgerStats?.out_cnt ?? 0 }}</div><div class="lbl">累计不在清单内</div></div>
            <div class="stat remark" :class="{ 'stat-alert': (ledgerStats?.pending_cnt ?? 0) > 0 }">
              <div class="num">{{ ledgerStats?.pending_cnt ?? 0 }}</div>
              <div class="lbl">累计待处理（需填处理意见）</div>
            </div>
          </div>
          <div class="hint" style="margin-bottom:10px;">
            累计覆盖率 <b>{{ fmtRate(ledgerStats?.cover_rate ?? 0) }}</b>
            · 最近核对 <b>{{ ledgerStats?.last_at || '—' }}</b>
            · 累计查询异常 <b>{{ ledgerStats?.err_cnt ?? 0 }}</b> 条
            · 累计需处理 <b>{{ ledgerStats?.need_cnt ?? 0 }}</b> 条（已填 {{ (ledgerStats?.need_cnt ?? 0) - (ledgerStats?.pending_cnt ?? 0) }} 条）
            · 台账<b>只增不删</b>（历史批次与明细不可修改、不可删除，保证留痕可信）
          </div>
          <div v-if="(ledgerStats?.pending_cnt ?? 0) > 0" class="hint pending-tip">
            ⚠ 还有 <b>{{ ledgerStats?.pending_cnt }}</b> 条需处理但未填处理意见——<b>填完才能导出台账</b>；
            勾选下方「只看待处理」可快速定位。
          </div>

          <div class="ledger-filter">
            <label class="lf">日期起
              <input v-model="ledgerF.start" type="date" class="inp" />
            </label>
            <label class="lf">日期止
              <input v-model="ledgerF.end" type="date" class="inp" />
            </label>
            <label class="lf">关键词
              <input v-model="ledgerF.keyword" class="inp" placeholder="标准号 / 标准名称" @keyup.enter="searchLedger" />
            </label>
            <label class="lf">结论
              <select v-model="ledgerF.conclusion" class="inp">
                <option value="">全部</option>
                <option value="在清单内">在清单内</option>
                <option value="版本不符·待确认">版本不符·待确认</option>
                <option value="不在清单内">不在清单内</option>
                <option value="查询异常">查询异常</option>
              </select>
            </label>
            <label class="lf">操作人
              <input v-model="ledgerF.operator" class="inp" placeholder="姓名" @keyup.enter="searchLedger" />
            </label>
            <label class="lf lf-check">
              <input v-model="ledgerF.pending" type="checkbox" @change="searchLedger" />
              只看待处理
            </label>
            <button class="btn btn-primary btn-sm" @click="searchLedger">🔍 查询</button>
            <button class="btn btn-outline btn-sm" @click="resetLedgerFilter">重置</button>
            <button class="btn btn-success btn-sm" @click="exportLedger">⬇ 导出台账 Excel</button>
          </div>

          <table>
            <thead>
              <tr>
                <th style="width:44px;">#</th>
                <th style="white-space:nowrap;">批次号</th>
                <th style="white-space:nowrap;">核对时间</th>
                <th>操作人</th>
                <th>部门</th>
                <th>核对用途</th>
                <th>来源</th>
                <th style="white-space:nowrap;">条数</th>
                <th>在清单内</th>
                <th>待确认</th>
                <th>不在清单</th>
                <th>异常</th>
                <th>待处理</th>
                <th>覆盖率</th>
                <th>耗时</th>
                <th style="width:110px;">明细</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="ledgerLoading">
                <td colspan="16" class="empty">台账读取中…</td>
              </tr>
              <tr v-else-if="ledgerBatches.length === 0">
                <td colspan="16" class="empty">
                  {{ ledgerEnabled ? '暂无台账记录：完成一次核对后会自动留痕。' : '台账功能未启用，请联系管理员检查后端 CMA_LEDGER_ENABLED 配置。' }}
                </td>
              </tr>
              <template v-for="(b, i) in ledgerBatches" :key="b.batch_id">
                <tr :class="{ 'row-risk': b.out_cnt > 0 || b.err_cnt > 0 }">
                  <td>{{ (ledgerPage - 1) * ledgerPageSize + i + 1 }}</td>
                  <td class="code-cell">{{ b.batch_id }}</td>
                  <td>{{ b.checked_at }}</td>
                  <td>{{ b.operator || '—' }}</td>
                  <td>{{ b.dept || '—' }}</td>
                  <td>{{ b.purpose || '—' }}</td>
                  <td>{{ b.source || '—' }}</td>
                  <td>
                    {{ b.total }}<span v-if="b.expected > b.total" class="warn-text">/{{ b.expected }}</span>
                  </td>
                  <td>{{ b.in_cnt }}</td>
                  <td>{{ b.warn_cnt }}</td>
                  <td :class="{ 'danger-text': b.out_cnt > 0 }">{{ b.out_cnt }}</td>
                  <td :class="{ 'danger-text': b.err_cnt > 0 }">{{ b.err_cnt }}</td>
                  <td :class="{ 'danger-text': b.pending_cnt > 0 }">
                    {{ b.pending_cnt }}<span v-if="b.need_cnt" class="pending-total">/{{ b.need_cnt }}</span>
                  </td>
                  <td>{{ fmtRate(b.cover_rate) }}</td>
                  <td>{{ fmtDuration(b) }}</td>
                  <td>
                    <button class="btn btn-outline btn-sm" @click="toggleLedgerDetail(b)">
                      {{ ledgerExpanded === b.batch_id ? '收起' : '查看明细' }}
                    </button>
                  </td>
                </tr>
                <tr v-if="ledgerExpanded === b.batch_id">
                  <td colspan="16" class="detail-cell">
                    <div class="detail-box">
                      <div class="detail-head">
                        批次 <b>{{ b.batch_id }}</b> · {{ batchState(b) }} · 来源 {{ b.source || '—' }} · IP {{ b.client_ip || '—' }}
                        · 完成时间 {{ b.finished_at || '—' }}
                      </div>
                      <div class="detail-filter">
                        <span class="df-label">明细显示：</span>
                        <button
                          class="df-btn"
                          :class="{ active: detailFilter === 'all' }"
                          @click="detailFilter = 'all'"
                        >全部 {{ ledgerItems.length }}</button>
                        <button
                          class="df-btn"
                          :class="{ active: detailFilter === 'need' }"
                          @click="detailFilter = 'need'"
                        >只看需处理 {{ detailNeedCount }}</button>
                        <button
                          class="df-btn"
                          :class="{ active: detailFilter === 'pending' }"
                          @click="detailFilter = 'pending'"
                        >只看待处理 {{ detailPendingCount }}</button>
                        <span class="df-hint">（需处理 = 版本待确认 / 不在清单内 / 有备注）</span>
                        <button
                          class="btn btn-primary btn-sm df-save"
                          :disabled="savingDisp || detailDraftCount === 0"
                          @click="saveAllItemDispositions"
                        >💬 保存已填的处理意见（{{ detailDraftCount }}）</button>
                      </div>
                      <div v-if="ledgerItemsLoading" class="hint">明细读取中…</div>
                      <table v-else class="inner-table">
                        <thead>
                          <tr>
                            <th style="width:44px;">#</th>
                            <th>输入标准号</th>
                            <th>平台标准号</th>
                            <th>标准名称</th>
                            <th style="width:150px;">结论</th>
                            <th>匹配方式</th>
                            <th>平台备注</th>
                            <th style="min-width:220px;">处理意见</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr v-if="ledgerItems.length === 0">
                            <td colspan="8" class="empty">该批次暂无明细。</td>
                          </tr>
                          <tr v-else-if="detailItems.length === 0">
                            <td colspan="8" class="empty">
                              {{ detailFilter === 'pending' ? '该批次没有待处理的条目（处理意见已全部填写 ✓）。' : '当前视图下没有条目。' }}
                            </td>
                          </tr>
                          <tr v-for="it in detailItems" :key="it.seq">
                            <td>{{ it.seq }}</td>
                            <td class="code-cell">{{ it.input_code }}</td>
                            <td class="code-cell">{{ it.platform_code || '—' }}</td>
                            <td>{{ it.standard_method || '—' }}</td>
                            <td><span :class="conclusionCls(it.conclusion)">{{ it.conclusion }}</span></td>
                            <td>{{ it.match_type || it.error || '—' }}</td>
                            <td>
                              <span v-if="it.remark" class="remark-text" :title="it.remark">{{ it.remark }}</span>
                              <span v-else style="color:var(--text2);">—</span>
                            </td>
                            <td>
                              <div v-if="it.need_disposition" class="disp-cell">
                                <CmaDispPicker
                                  v-model="ledgerDisp[dispKey(it.batch_id, it.seq)]"
                                  :options="dispHistory"
                                  placeholder="填写该条的处理意见"
                                  empty-text="暂无历史处理意见：先手动填写并保存，下次就能从这里直接选。"
                                />
                                <button
                                  class="btn btn-primary btn-sm"
                                  :disabled="savingDisp"
                                  @click="saveItemDisposition(it)"
                                >保存</button>
                              </div>
                              <span v-else style="color:var(--text2);">—</span>
                            </td>
                          </tr>
                        </tbody>
                      </table>
                      <div v-if="b.note" class="detail-note">历史处置说明：{{ b.note }}</div>
                      <div class="note-row">
                        <span class="note-label">处置 / 复核说明</span>
                        <CmaDispPicker
                          v-model="ledgerNoteDraft"
                          :options="noteHistory"
                          placeholder="如：版本不符已换版为 GB xxx-2025；不在清单内项目已停止出具该项目报告"
                          empty-text="暂无历史处置说明：先填写并保存，下次就能从这里直接选。"
                        />
                        <button class="btn btn-primary btn-sm" :disabled="ledgerNoteSaving" @click="saveLedgerNote(b)">
                          {{ ledgerNoteSaving ? '保存中…' : '保存说明' }}
                        </button>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>

          <div class="pager" :style="ledgerTotalPages > 1 ? undefined : { display: 'none' }">
            <template v-if="ledgerTotalPages > 1">
              <span class="pager-info">共 {{ ledgerTotal }} 个批次 · 第 {{ ledgerPage }}/{{ ledgerTotalPages }} 页</span>
              <button :disabled="ledgerPage === 1" @click="ledgerGoPage(ledgerPage - 1)">上一页</button>
              <button :disabled="ledgerPage === ledgerTotalPages" @click="ledgerGoPage(ledgerPage + 1)">下一页</button>
              <span class="pager-info">每页
                <select :value="ledgerPageSize" class="page-size" @change="onLedgerPageSize">
                  <option :value="10">10</option>
                  <option :value="20">20</option>
                  <option :value="50">50</option>
                </select> 条</span>
            </template>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style>
/* 原样式完整保留，仅加 `.v-cma` 前缀做隔离；body 规则改挂根容器 */
.v-cma, .v-cma * { margin: 0; padding: 0; box-sizing: border-box; }
.v-cma {
  --primary: #4f6ef7; --primary-hover: #3b5de7;
  --danger: #e74c3c; --success: #27ae60; --warning: #f39c12;
  --bg: #f0f2f5; --card-bg: #fff; --text: #2c3e50;
  --text1: #2c3e50; --text2: #7f8c8d; --border: #e0e4e8;
  --shadow: 0 2px 12px rgba(0,0,0,0.08); --radius: 10px;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  background: var(--bg); color: var(--text); min-height: 100vh;
}
.v-cma .header { background: var(--card-bg); border-bottom: 1px solid var(--border);
  padding: 0 24px; height: 60px; display: flex; align-items: center; justify-content: space-between;
  box-shadow: 0 1px 4px rgba(0,0,0,0.04); position: sticky; top: 0; z-index: 100; }
.v-cma .header h1 { font-size: 20px; font-weight: 700; color: var(--primary); }
.v-cma .header .links a { color: var(--text2); text-decoration: none; font-size: 13px; margin-left: 16px; }
.v-cma .header .links a:hover { color: var(--primary); }
.v-cma .main { max-width: 1200px; margin: 0 auto; padding: 20px 24px; }

.v-cma .card { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); margin-bottom: 16px; overflow: hidden; }
.v-cma .card-header { display: flex; align-items: center; justify-content: space-between;
  padding: 12px 18px; border-bottom: 1px solid var(--border); background: #fafbfc; }
.v-cma .card-header h3 { font-size: 14px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.v-cma .card-body { padding: 16px 18px; }

.v-cma .btn { padding: 8px 18px; border-radius: 8px; border: none; font-size: 14px; font-weight: 500;
  cursor: pointer; transition: 0.2s; display: inline-flex; align-items: center; gap: 6px; }
.v-cma .btn:disabled { opacity: 0.5; cursor: not-allowed; }
.v-cma .btn-primary { background: var(--primary); color: #fff; }
.v-cma .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
.v-cma .btn-success { background: var(--success); color: #fff; }
.v-cma .btn-success:hover:not(:disabled) { background: #219a52; }
.v-cma .btn-outline { background: transparent; border: 1.5px solid var(--border); color: var(--text); }
.v-cma .btn-outline:hover:not(:disabled) { background: var(--bg); }
.v-cma .btn-sm { padding: 5px 12px; font-size: 12px; border-radius: 6px; }

.v-cma textarea { width: 100%; min-height: 180px; border: 1.5px solid var(--border); border-radius: 8px;
  padding: 10px 12px; font-size: 14px; font-family: 'Consolas','PingFang SC','Microsoft YaHei',monospace;
  line-height: 1.7; resize: vertical; outline: none; }
.v-cma textarea:focus { border-color: var(--primary); }
.v-cma .hint { font-size: 12px; color: var(--text2); margin-top: 6px; line-height: 1.6; }

.v-cma .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 12px; }

.v-cma .stats { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 14px; }
.v-cma .stat { cursor: pointer; flex: 1; min-width: 110px; background: #fafbfc; border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 14px; text-align: center; transition: box-shadow 0.15s, border-color 0.15s; }
.v-cma .stat:hover { border-color: var(--primary); }
.v-cma .stat.active { box-shadow: 0 0 0 2px var(--primary); border-color: var(--primary); }
.v-cma .stat .num { font-size: 24px; font-weight: 700; }
.v-cma .stat .lbl { font-size: 12px; color: var(--text2); margin-top: 2px; }
.v-cma .stat.ok .num { color: var(--success); }
.v-cma .stat.no .num { color: var(--danger); }
.v-cma .stat.fuzzy .num { color: var(--warning); }
.v-cma .stat.total .num { color: var(--primary); }
.v-cma .stat.remark .num { color: var(--warning); background: #fff7e6; border-color: #ffd591; }
.v-cma .stat.warn .num { color: #d48806; }

.v-cma .progress-wrap { display: none; margin-bottom: 14px; }
.v-cma .progress-wrap.show { display: block; }
.v-cma .progress-bar { height: 8px; background: var(--border); border-radius: 5px; overflow: hidden; }
.v-cma .progress-fill { height: 100%; width: 0%; background: var(--primary); transition: width 0.2s; }
.v-cma .progress-text { font-size: 12px; color: var(--text2); margin-top: 6px; line-height: 1.6; word-break: break-all; }
.v-cma .current-std { font-size: 13px; color: var(--primary); margin-top: 4px; font-weight: 600; min-height: 1.4em; }

.v-cma table { width: 100%; border-collapse: collapse; font-size: 13px; }
.v-cma th, .v-cma td { padding: 9px 10px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
.v-cma th { background: #fafbfc; font-weight: 600; position: sticky; top: 0; text-align: center; white-space: nowrap; }
.v-cma tbody tr.in { background: #f4fbf6; }
.v-cma tbody tr.out { background: #fef6f5; }
.v-cma tbody tr.has-remark { background: #fffbe6; box-shadow: inset 4px 0 0 var(--warning); }
.v-cma tbody tr.warn { background: #fff7e6; box-shadow: inset 4px 0 0 var(--warning); }
.v-cma .badge { display: inline-block; padding: 2px 9px; border-radius: 12px; font-size: 12px; font-weight: 600; }
.v-cma .badge.in { background: #e8f5e9; color: var(--success); }
.v-cma .badge.out { background: #fef0ef; color: var(--danger); }
.v-cma .badge.remark { background: #fff1d6; color: #d48806; border: 1px solid #ffd591; }
.v-cma .badge.warn { background: #fff1d6; color: #d48806; border: 1px solid #ffd591; }
.v-cma .code-cell { font-family: 'Consolas', monospace; word-break: break-all; }
.v-cma .note-fuzzy { color: var(--warning); font-weight: 600; }
.v-cma .note-warn { color: #d48806; font-weight: 600; }
.v-cma .remark-text { color: #ad6800; font-weight: 600; }
.v-cma .empty { text-align: center; color: var(--text2); padding: 30px; font-size: 14px; }

.v-cma .result-actions { display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }
.v-cma .pager { display: flex; align-items: center; justify-content: center; gap: 6px; flex-wrap: wrap; margin-top: 16px; }
.v-cma .pager button { min-width: 34px; padding: 6px 10px; border: 1.5px solid var(--border); background: #fff; border-radius: 6px; cursor: pointer; font-size: 13px; color: var(--text1); transition: .15s; }
.v-cma .pager button:hover:not(:disabled) { border-color: var(--primary); color: var(--primary); }
.v-cma .pager button.active { background: var(--primary); color: #fff; border-color: var(--primary); }
.v-cma .pager button:disabled { opacity: .45; cursor: not-allowed; }
.v-cma .pager .pager-info { font-size: 13px; color: var(--text2); margin: 0 6px; }
.v-cma .pager .ellipsis { color: var(--text2); padding: 0 4px; }
.v-cma .page-size { padding: 5px 8px; border: 1.5px solid var(--border); border-radius: 6px; font-size: 13px; background: #fff; color: var(--text1); }

.v-cma .loading { display: none; position: fixed; inset: 0; background: rgba(255,255,255,0.7);
  z-index: 500; align-items: center; justify-content: center; }
.v-cma .loading.show { display: flex; }
.v-cma .spinner { width: 40px; height: 40px; border: 4px solid var(--border); border-top-color: var(--primary);
  border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.v-cma .toast-box { position: fixed; top: 20px; right: 20px; z-index: 1000; display: flex; flex-direction: column; gap: 8px; }
.v-cma .toast { padding: 12px 20px; border-radius: 8px; color: #fff; font-size: 14px; box-shadow: 0 4px 16px rgba(0,0,0,0.15); animation: slideIn 0.3s ease; }
.v-cma .toast.ok { background: var(--success); }
.v-cma .toast.err { background: var(--danger); }
.v-cma .toast.info { background: var(--primary); }
@keyframes slideIn { from { opacity: 0; transform: translateX(100%); } to { opacity: 1; transform: translateX(0); } }

/* ==================== 核对留痕 / 台账（新增） ==================== */
.v-cma .field-row { display: flex; flex-wrap: wrap; gap: 12px; }
.v-cma .field { flex: 1; min-width: 180px; display: flex; flex-direction: column; gap: 4px; }
.v-cma .field.wide { flex: 2; min-width: 260px; }
.v-cma .field span { font-size: 12px; color: var(--text2); }
.v-cma .inp { height: 34px; border: 1.5px solid var(--border); border-radius: 8px; padding: 0 10px;
  font-size: 13px; color: var(--text1); background: #fff; outline: none; }
.v-cma .inp:focus { border-color: var(--primary); }
.v-cma select.inp { padding: 0 6px; }

.v-cma .ledger-filter { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 12px;
  padding: 10px 12px; background: #fafbfc; border: 1px solid var(--border); border-radius: 8px; }
.v-cma .ledger-filter .lf { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text2); }
.v-cma .ledger-filter .lf .inp { width: 150px; height: 32px; }
.v-cma .ledger-filter .lf input[type="date"] { width: 140px; }

.v-cma .badge.fuzzy { background: #fff1d6; color: #d48806; }
.v-cma tbody tr.row-risk { background: #fff7f6; }
.v-cma .danger-text { color: var(--danger); font-weight: 700; }
.v-cma .warn-text { color: #d48806; font-weight: 700; }

.v-cma .detail-cell { background: #f7f9fc; padding: 12px 14px; }
.v-cma .detail-box { border: 1px solid var(--border); border-radius: 8px; background: #fff; padding: 10px 12px; }
.v-cma .detail-head { font-size: 12px; color: var(--text2); margin-bottom: 8px; line-height: 1.7; }
.v-cma .inner-table { font-size: 12px; }
.v-cma .inner-table th { background: #f2f5f9; }
.v-cma .detail-note { font-size: 12px; color: #ad6800; margin-top: 8px; line-height: 1.7; word-break: break-all; }
.v-cma .note-row { display: flex; align-items: flex-start; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
.v-cma .note-label { font-size: 12px; color: var(--text2); white-space: nowrap; padding-top: 7px; }

@media (max-width: 900px) {
  .v-cma .ledger-filter .lf .inp { width: 120px; }
}

/* ==================== 处理意见（逐条闭环，新增） ==================== */
.v-cma .disp-cell { display: flex; align-items: flex-start; gap: 6px; }
.v-cma .disp-cell .btn { flex: none; }
.v-cma .stat.stat-alert { border-color: var(--danger); background: #fef6f5; }
.v-cma .stat.stat-alert .num { color: var(--danger); }
.v-cma .pending-tip { color: var(--danger); font-weight: 600; }
.v-cma .pending-total { color: var(--text2); font-weight: 400; font-size: 12px; }
.v-cma .ledger-filter .lf-check { gap: 4px; }
.v-cma .ledger-filter .lf-check input { width: 14px; height: 14px; }

/* ==================== 批次明细视图切换（新增） ==================== */
.v-cma .detail-filter { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 8px 0 10px; }
.v-cma .detail-filter .df-label { font-size: 12px; color: var(--text2); }
.v-cma .detail-filter .df-btn { padding: 4px 12px; border: 1.5px solid var(--border); background: #fff; border-radius: 14px;
  font-size: 12px; color: var(--text1); cursor: pointer; transition: .15s; }
.v-cma .detail-filter .df-btn:hover { border-color: var(--primary); color: var(--primary); }
.v-cma .detail-filter .df-btn.active { background: var(--primary); border-color: var(--primary); color: #fff; font-weight: 600; }
.v-cma .detail-filter .df-hint { font-size: 12px; color: var(--text2); }
.v-cma .detail-filter .df-save { margin-left: auto; }
</style>
