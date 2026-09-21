// 统一入口的 API 封装。VITE_API_BASE 驱动；未配置时同源（vite dev 走代理 /api → 8080）
import type {
  ApiResult,
  CompletionData,
  DetectRateData,
  ItemCountData,
  MonthlyData,
  ProductData,
  RegionData,
  SummaryData,
  TopData,
} from './types'

const BASE = import.meta.env.VITE_API_BASE || ''

async function get<T>(path: string): Promise<T> {
  const resp = await fetch(`${BASE}${path}`)
  const body = await resp.json()
  if (!resp.ok) {
    throw new Error((body as { detail?: string }).error || (body as { detail?: string }).detail || `请求失败 ${resp.status}`)
  }
  return body as T
}

async function post<T>(path: string, payload?: unknown): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })
  const body = await resp.json()
  if (!resp.ok) {
    throw new Error((body as { detail?: string }).error || (body as { detail?: string }).detail || `请求失败 ${resp.status}`)
  }
  return body as T
}

async function del<T>(path: string): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, { method: 'DELETE' })
  const body = await resp.json()
  if (!resp.ok) {
    throw new Error(body.error || body.detail || `请求失败 ${resp.status}`)
  }
  return body as T
}

/* ==================== 导航聚合状态 ==================== */

export interface NavModuleStatus {
  key: string
  port: number
  name: string
  online: boolean
  standalone: boolean
}

export interface NavStatus {
  success: boolean
  pool: { ok: boolean }
  data_sources: { dm: { ok: boolean; error: string | null }; sql: { ok: boolean; error: string | null } }
  modules: NavModuleStatus[]
  /** 数据来源模式：mirror=走本地 SQLite 镜像；direct=源库直连。data_deadline 为镜像快照的数据截止时间。 */
  mirror?: { mode: 'mirror' | 'direct'; data_deadline: string | null; fallback: string | null }
}

export const navApi = {
  status: () => get<NavStatus>('/api/nav/status'),
}

/* ==================== 镜像管理 ==================== */

export interface MirrorSyncState {
  syncing: boolean
  started_at: string | null
  finished_at: string | null
  last: Record<string, unknown> | null
  last_error: string | null
}

export interface MirrorStatus {
  success: boolean
  mode: 'mirror' | 'direct'
  fallback: string | null
  data_deadline: string | null
  exists: boolean
  sync: MirrorSyncState
}

export const mirrorApi = {
  status: () => get<MirrorStatus>('/api/mirror/status'),
  sync: () => post<{ success: boolean; message?: string }>('/api/mirror/sync'),
}

/* ==================== 达梦查询 ==================== */

export interface DmQueryResult {
  success: boolean
  error?: string
  type?: string
  columns?: string[]
  rows?: unknown[][]
  rowCount?: number
}

export const dmApi = {
  query: (sql: string) => post<DmQueryResult>('/api/dm/query', { sql }),
  autoConnect: () =>
    post<{ success: boolean; error?: string; message?: string; host?: string; port?: number; user?: string; schema?: string }>(
      '/api/dm/auto-connect',
    ),
  disconnect: () => post<{ success: boolean }>('/api/dm/disconnect'),
  idleTime: () => get<{ connected: boolean; remaining: number }>('/api/dm/idle-time'),
}

/* ==================== SQL Server ==================== */

export interface SqlQueryResult {
  success: boolean
  error?: string
  columns?: string[]
  rows?: unknown[][]
}

export const sqlserverApi = {
  query: (sql: string) => post<SqlQueryResult>('/api/sqlserver/query', { sql }),
  autoConnect: () =>
    post<{ success: boolean; error?: string; message?: string; server?: string; port?: number; database?: string; mirror?: boolean; dataDeadline?: string | null }>(
      '/api/sqlserver/auto-connect',
    ),
  disconnect: () => post<{ success: boolean }>('/api/sqlserver/disconnect'),
}

/* ==================== 数据看板 ==================== */

export const dashboardApi = {
  health: () => get<{ status: string }>('/api/health'),

  years: () => get<{ success: boolean; years: number[] }>('/api/board/years'),

  history: (year?: number | null) =>
    get<{
      success: boolean
      data: {
        years: number[]
        samples: number[]
        items: number[]
        ok: number[]
        bad: number[]
        risk_items: { name: string; cnt: number }[]
        detail: {
          year: number
          samples: number
          items: number
          ok: number
          bad: number
          risk_items: { name: string; cnt: number }[]
          by_kind: { name: string; cnt: number }[]
        } | null
        source: string
        notes: string
      }
    }>(`/api/board/history${year ? `?year=${year}` : ''}`),

  /** 历年对比表（2020–2025 全量扫描，约 2 秒）——按需调用，不要放在首屏加载里。 */
  historyOverview: () =>
    get<{
      success: boolean
      data: {
        years: number[]
        samples: number[]
        items: number[]
        ok: number[]
        bad: number[]
      }
    }>('/api/board/history?overview=1'),

  summary: (year?: number) =>
    get<ApiResult<SummaryData>>(`/api/board/summary${year ? `?year=${year}` : ''}`),

  monthly: (year?: number) =>
    get<ApiResult<MonthlyData>>(`/api/board/monthly${year ? `?year=${year}` : ''}`),

  product: (year?: number) =>
    get<ApiResult<ProductData>>(`/api/board/product${year ? `?year=${year}` : ''}`),

  completion: (year?: number) =>
    get<ApiResult<CompletionData>>(`/api/board/completion${year ? `?year=${year}` : ''}`),

  itemCount: (year?: number) =>
    get<ApiResult<ItemCountData>>(`/api/board/item-count${year ? `?year=${year}` : ''}`),

  detectRate: (year?: number) =>
    get<ApiResult<DetectRateData>>(`/api/board/detect-rate${year ? `?year=${year}` : ''}`),

  top: (year?: number) => get<ApiResult<TopData>>(`/api/board/top${year ? `?year=${year}` : ''}`),

  risk: (year?: number) => get<ApiResult<TopData>>(`/api/board/risk${year ? `?year=${year}` : ''}`),

  region: (year?: number) =>
    get<ApiResult<RegionData>>(`/api/board/region${year ? `?year=${year}` : ''}`),

  productRisk: (year?: number) =>
    get<ApiResult<RegionData>>(`/api/board/product-risk${year ? `?year=${year}` : ''}`),
}

/* ==================== 人员能力表 ==================== */

export interface PersonnelErrors {
  dm: string | null
  sql: string | null
}

export interface PersonnelByPersonRow {
  project: string
  method: string
  /** 是否在能力表：`在` / `不在`；未上传能力表时为空串（前端据此不渲染该列） */
  inAbility?: string
  /** 宽松命中的差异原因：`年号不同` / `项目名别名` / `别名与年号均不同` */
  inAbilityNote?: string
}

export interface PersonnelByProjectRow {
  project: string
  method: string
  person: string
  lastDate: string
}

/** 能力表条目（前端从《检验检测能力表》解析出的「项目 + 标准」） */
export interface AbilityItem {
  project: string
  method: string
}

/** 能力表状态（字段与后端 `/api/personnel/ability-table` 一一对应） */
export interface AbilityStatus {
  success: boolean
  error?: string
  message?: string
  loaded: boolean
  fileName: string
  uploadedAt: string
  count: number
  uniquePairs: number
  uniqueProjects: number
}

export const personnelApi = {
  byPerson: (name: string) =>
    post<{
      success: boolean
      error?: string
      name: string
      total: number
      rows: PersonnelByPersonRow[]
      dbCount: { dm: number; sql: number }
      errors: PersonnelErrors
    }>('/api/personnel/query-by-person', { name }),

  byProject: (keyword: string) =>
    post<{
      success: boolean
      error?: string
      keyword: string
      total: number
      rows: PersonnelByProjectRow[]
      dbCount: { dm: number; sql: number }
      errors: PersonnelErrors
    }>('/api/personnel/query-by-project', { keyword }),

  /** 能力表状态（页面加载时回显：文件名 / 上传时间 / 条目数 / 唯一项目数） */
  abilityStatus: () => get<AbilityStatus>('/api/personnel/ability-table'),

  /** 上传（或直接替换）能力表：整表覆盖，失败时后端回滚、旧表保持不变 */
  uploadAbility: (fileName: string, items: AbilityItem[]) =>
    post<AbilityStatus>('/api/personnel/ability-table', { fileName, items }),

  /** 清除已生效的能力表 */
  clearAbility: () => del<AbilityStatus & { cleared: boolean }>('/api/personnel/ability-table'),
}

/* ==================== 抽采样进度统计 ==================== */

// 三级表头版（品类 → 产品类型 → 任务量/完成量）的接口与类型集中在 ./progress，
// 单独成文件是为了能与后端 `modules/progress/routes.py` 一一对照着看。
// 这里再导出一次，保证既有 `from '../api/client'` 的引用不必改动。
export * from './progress'

/* ==================== 一单一库核对（CMA） ==================== */

export interface CmaResult {
  found: boolean
  fuzzy: boolean
  versionMismatch: boolean
  matchType: string
  standardCode: string
  standardMethod: string
  remark: string
  error: string
  _input?: string
  /** 台账批次内序号（前端逐条核对时按输入顺序编号，用于回填处理意见） */
  _seq?: number
}

/** 留痕上下文：批次号由前端生成，随每条核对请求带上，后端据此归集成批次 */
export interface CmaLedgerMeta {
  batchId: string
  seq: number
  total: number
  operator: string
  dept: string
  purpose: string
  source: string
}

/** 台账批次（一次"开始核对"= 一个批次） */
export interface CmaLedgerBatch {
  batch_id: string
  checked_at: string
  finished_at: string
  operator: string
  dept: string
  purpose: string
  source: string
  client_ip: string
  expected: number
  total: number
  in_cnt: number
  warn_cnt: number
  out_cnt: number
  err_cnt: number
  remark_cnt: number
  /** 需处理条数（版本待确认 / 不在清单内 / 有备注） */
  need_cnt: number
  /** 需处理但尚未填写处理意见的条数（>0 时不允许导出台账） */
  pending_cnt: number
  cover_rate: number
  duration_ms: number
  note: string
}

/** 台账明细（批次内每条标准号） */
export interface CmaLedgerItem {
  batch_id: string
  seq: number
  input_code: string
  platform_code: string
  standard_method: string
  remark: string
  conclusion: string
  match_type: string
  error: string
  /** 1 = 该条需要填写处理意见（版本待确认 / 不在清单内 / 有备注） */
  need_disposition: number
  /** 处理意见内容（只存意见本身，不记处理人与时间） */
  disposition: string
  checked_at: string
}

export interface CmaLedgerStats {
  batches: number
  items: number
  in_cnt: number
  warn_cnt: number
  out_cnt: number
  err_cnt: number
  remark_cnt: number
  need_cnt: number
  pending_cnt: number
  last_at: string | null
  cover_rate: number
  operators: string[]
  enabled: boolean
  dir: string
}

/** 历史候选项（处理意见 / 批次处置说明 共用同一结构） */
export interface CmaHistoryItem {
  text: string
  uses: number
  last_at?: string
}

export interface CmaLedgerQuery {
  start?: string
  end?: string
  keyword?: string
  conclusion?: string
  operator?: string
  /** '1' = 只看还有未填处理意见的批次 */
  pending?: string
  [key: string]: string | undefined
}

function qs(params: Record<string, unknown>): string {
  const sp = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && String(v) !== '') sp.set(k, String(v))
  })
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export const cmaApi = {
  checkOne: (code: string, ledger?: CmaLedgerMeta) =>
    post<{ success: boolean; error?: string; result: CmaResult; ledger?: { batchId: string; enabled: boolean; error?: string } }>(
      '/api/cma/check-one',
      { code, ledger },
    ),
  checkStandards: (codes: string[], ledger?: Partial<CmaLedgerMeta>) =>
    post<{ success: boolean; error?: string; results: CmaResult[]; total: number }>('/api/cma/check-standards', { codes, ledger }),

  /* ---- 台账（留痕）：只增不删，无删除接口 ---- */
  ledgerBatches: (page: number, pageSize: number, q: CmaLedgerQuery = {}) =>
    get<{ success: boolean; error?: string; page: number; pageSize: number; total: number; batches: CmaLedgerBatch[] }>(
      `/api/cma/ledger/batches${qs({ page, pageSize, ...q })}`,
    ),
  ledgerBatch: (batchId: string) =>
    get<{ success: boolean; error?: string; batch: CmaLedgerBatch; items: CmaLedgerItem[] }>(
      `/api/cma/ledger/batches/${encodeURIComponent(batchId)}`,
    ),
  ledgerStats: (q: CmaLedgerQuery = {}) =>
    get<{ success: boolean; error?: string; data: CmaLedgerStats }>(`/api/cma/ledger/stats${qs(q)}`),
  ledgerExport: (q: CmaLedgerQuery = {}) =>
    get<{ success: boolean; error?: string; batches: CmaLedgerBatch[]; items: CmaLedgerItem[] }>(
      `/api/cma/ledger/export${qs(q)}`,
    ),
  ledgerNote: (batchId: string, note: string) =>
    post<{ success: boolean; error?: string }>(`/api/cma/ledger/batches/${encodeURIComponent(batchId)}/note`, { note }),
  /** 逐条处理意见：核对结果表（当场填）与台账明细（事后补填/修改）共用 */
  /** 历史候选（处理意见 / 批次处置说明），用于下拉选取；只读 */
  ledgerHistory: (kind: 'all' | 'disposition' | 'note' = 'all', keyword = '', limit = 200) =>
    get<{
      success: boolean
      error?: string
      kind?: string
      dispositions?: CmaHistoryItem[]
      notes?: CmaHistoryItem[]
    }>(`/api/cma/ledger/history${qs({ kind, keyword, limit })}`),

  ledgerDispositions: (batchId: string, items: { seq: number; disposition: string }[]) =>
    post<{
      success: boolean
      error?: string
      batch_id?: string
      saved?: number[]
      failed?: { seq: number; error: string }[]
      pending_cnt?: number
    }>(`/api/cma/ledger/batches/${encodeURIComponent(batchId)}/dispositions`, { items }),
}
