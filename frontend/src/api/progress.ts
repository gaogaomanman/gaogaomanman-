/**
 * 抽采样进度统计（/progress）接口与类型。
 *
 * 为什么单独一个文件：这个模块的接口面比其它工具大得多（三级表头配置 + 任务量 + 未归类处理），
 * 全塞进 `client.ts` 会淹没其它工具；契约与实现放一起也便于对照后端 `modules/progress/routes.py`。
 *
 * 响应约定：后端返回**扁平结构** `{success: true, ...}`（不是 `{success, data}`）。
 */
const BASE = import.meta.env.VITE_API_BASE || ''

async function get<T>(path: string): Promise<T> {
  const resp = await fetch(`${BASE}${path}`)
  const body = await resp.json()
  if (!resp.ok) throw new Error(body?.detail || body?.error || `请求失败 ${resp.status}`)
  return body as T
}

async function post<T>(path: string, payload?: unknown): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })
  const body = await resp.json()
  if (!resp.ok) throw new Error(body?.detail || body?.error || `请求失败 ${resp.status}`)
  return body as T
}

function qs(params: Record<string, unknown>): string {
  const sp = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && String(v) !== '') sp.set(k, String(v))
  })
  const s = sp.toString()
  return s ? `?${s}` : ''
}

/* ==================== 类型 ==================== */

/** 看板筛选（统计范围）：都为空 = 全部合同 / 全部业务类别 / 全年 */
export interface ProgressFilter {
  contracts: string[]
  bizs: string[]
  cutoff: string
}

/** 任务类型（考核表的单位）：市例行 / 市监督 …；看板第一层选择 */
export interface ProgressTaskType {
  id: number
  name: string
  sort_no: number
  enabled: boolean
}

/** 列方案 = 任务 × 年份 = 一套表头列（指标项每年可调整，因此按年各存一份） */
export interface ProgressScheme {
  id: number
  year: number
  task_type_id: number
  task_name: string
  note: string
  category_count: number
  category_enabled?: number
  quota_total: number
  done_total: number
}

/** 二级表头：产品类型（任务量录入的最小单位） */
export interface ProgressProductRef {
  id: number
  name: string
  category_id: number
  is_other: boolean
}

/** 一级表头：品类（含其下的产品类型列顺序） */
export interface ProgressCategoryCol {
  id: number
  name: string
  big_kind: string
  is_other: boolean
  product_ids: number[]
}

export interface ProgressCell {
  product_type_id: number
  quota: number
  done: number
  rate: number | null
}

export interface ProgressCategoryCell {
  category_id: number
  quota: number
  done: number
  rate: number | null
}

export interface ProgressMatrixRow {
  region: string
  /** 产品类型级格子（明细视图用），顺序与 `products` 一致 */
  cells: ProgressCell[]
  /** 品类级格子（汇总视图用，本区域口径，含品类级下达量），顺序与 `categories` 一致 */
  category_cells: ProgressCategoryCell[]
  quota: number
  done: number
  rate: number | null
}

export interface ProgressProductTotal {
  product_type_id: number
  quota: number
  done: number
  rate: number | null
}

export interface ProgressCategoryTotal {
  category_id: number
  quota: number
  done: number
  rate: number | null
}

export interface ProgressContractRow {
  contract_no: string
  name: string
  note: string
  enabled: boolean
  /** auto = 镜像自动发现；manual = 人工录入 */
  source: string
  quota_rows: number
  quota_total: number
  done_total: number
  /** 合同归并：非空表示本编号是变体，已并入该正式编号（仅配置页返回） */
  alias_of?: string
  /** 已并入本合同的变体编号（仅看板合同清单返回） */
  merged?: string[]
  /** 这份合同属于哪个任务（0 = 未纳入任何考核表）；仅配置页返回 */
  task_type_id?: number
  task_name?: string
}

export interface ProgressRegionRow {
  name: string
  /** 地址/COUNTY 里可能出现的写法（逗号分隔），决定这一行接住哪些样品 */
  keys: string
  enabled: boolean
  source: string
  sort_no: number
  quota_total: number
  done_total: number
}

export interface ProgressSyncMeta {
  data_deadline: string | null
  synced_at: string
  last_status: string
  last_error: string
  rows: number
  unmatched: number
  unmapped: number
  ignored: number
  no_contract: number
  raw_stats: Record<string, number>
}

export interface ProgressSummary {
  /** 当前任务（考核表） */
  task_type_id: number
  task_name: string
  quota_total: number
  done_total: number
  rate: number | null
  region_count: number
  category_count: number
  product_count: number
  done_unmatched: number
  done_unmapped_region: number
  done_ignored: number
  done_in_scope: number
  contract_count: number
  /** 选了单个合同时为 true：任务量按任务下达，不随合同筛选变化（完成率会失真） */
  contract_filtered: boolean
  /** 合同没归到任何任务的样品数（不进本表） */
  done_no_task: number
  /** 属于其它任务的样品数（不进本表） */
  done_other_task: number
  /** 本年该任务的列方案还是空的（需要"复制上一年的方案"） */
  scheme_empty: boolean
}

/** 未纳入清单：合同没归到任何任务的样品 */
export interface ProgressNoTask {
  total: number
  names: { name: string; count: number }[]
  contracts: { contract_no: string; count: number }[]
}

export interface ProgressOverview {
  year: number
  task: ProgressTaskType
  tasks: ProgressTaskType[]
  scheme: { id: number; year: number; task_type_id: number; note: string; category_count: number }
  schemes: ProgressScheme[]
  filters: ProgressFilter
  categories: ProgressCategoryCol[]
  products: ProgressProductRef[]
  rows: ProgressMatrixRow[]
  category_totals: ProgressCategoryTotal[]
  product_totals: ProgressProductTotal[]
  grand_total: { quota: number; done: number; rate: number | null }
  summary: ProgressSummary
  charts: {
    region_rate: { labels: string[]; rate: (number | null)[]; done: number[]; quota: number[] }
    category: { labels: string[]; quota: number[]; done: number[] }
    product: { labels: string[]; quota: number[]; done: number[] }
  }
  unmatched: { name: string; count: number }[]
  unmapped_regions: { region_key: string; count: number }[]
  ignored: { name: string; count: number }[]
  no_task: ProgressNoTask
  contracts: ProgressContractRow[]
  regions: ProgressRegionRow[]
  /** 业务类别下拉的可选项（来自当年明细） */
  biz_options: string[]
  sync: ProgressSyncMeta
}

export interface ProgressBigKind {
  name: string
  keywords: string
  sort_no: number
  enabled: boolean
}

export interface ProgressCategoryCfg {
  id: number
  name: string
  big_kind: string
  is_other: boolean
  sort_no: number
  enabled: boolean
  quota_total: number
  done_total: number
  /** 匹配关键词（这一列接住哪些样品；样品名/样品类别，逗号或竖线分隔） */
  keywords: string
  /** 关键词个数 */
  key_count: number
}

export interface ProgressProductTypeCfg {
  id: number
  name: string
  category_id: number
  category_name: string
  big_kind: string
  /** 匹配关键词（逗号/顿号/竖线分隔）：决定这一列接住哪些样品 */
  keywords: string
  is_other: boolean
  sort_no: number
  enabled: boolean
  key_count: number
  quota_total: number
  done_total: number
}

/** 区域判定冲突：COUNTY 与地址给出不同区域时的汇总（供「区域配置」页提示） */
export interface ProgressRegionConflict {
  county_region: string
  addr_region: string
  count: number
}

export interface ProgressConfig {
  year: number
  task: ProgressTaskType
  tasks: ProgressTaskType[]
  scheme: { id: number; year: number; task_type_id: number; note: string }
  schemes: ProgressScheme[]
  big_kinds: ProgressBigKind[]
  categories: ProgressCategoryCfg[]
  product_types: ProgressProductTypeCfg[]
  regions: ProgressRegionRow[]
  contracts: ProgressContractRow[]
  ignore_names: { name: string; note: string }[]
  /** 本任务的未归类样品（可一键挂到某一列） */
  unmatched: { name: string; count: number; category: string }[]
  unmapped_regions: { region_key: string; count: number }[]
  /** 未纳入清单：合同没归到任何任务的样品 */
  no_task: { name: string; count: number }[]
  /** 区域判定优先项：county（受检单位所在区县，默认）/ address（抽样地址） */
  region_priority: 'county' | 'address'
  region_conflicts: { total: number; items: ProgressRegionConflict[] }
  sync: { data_deadline: string | null; synced_at: string; last_error: string }
}

export interface ProgressQuotaPanel {
  year: number
  task: ProgressTaskType
  tasks: ProgressTaskType[]
  scheme: { id: number; task_type_id: number; note: string }
  /** 两级列：品类 → 产品类型 */
  columns: {
    category_id: number
    category: string
    big_kind: string
    products: { id: number; name: string; is_other: boolean }[]
  }[]
  rows: { region: string }[]
  /** 产品级任务量 */
  values: { region: string; product_type_id: number; quota: number; done: number }[]
  /** 品类级任务量（考核表的口径：任务量按品类下达）与对应完成量 */
  category_values: { region: string; category_id: number; quota: number; done: number }[]
  meta: { data_deadline: string | null; synced_at: string }
}

export interface ProgressSyncLog {
  id: number
  year: number
  started_at: string
  finished_at: string
  source: string
  data_deadline: string
  rows: number
  done_total: number
  contract_count: number
  unmatched: number
  unclassified: number
  no_contract: number
  ignored: number
  elapsed_ms: number
  status: string
  error: string
}

/** 导入行：`产品类型` 留空表示按**品类**下达（与考核表列一致）；两者都填则按产品类型 */
export interface ProgressImportRow {
  contract_no?: string
  region: string
  category?: string
  product_type?: string
  quota: string | number | null
}

/* ==================== 接口 ==================== */

export const progressApi = {
  years: () => get<{ success: boolean; years: number[] }>('/api/progress/years'),

  /** 任务类型与它们的历年列方案 */
  taskTypes: () =>
    get<{ success: boolean; task_types: ProgressTaskType[]; schemes: ProgressScheme[] }>(
      '/api/progress/task-types',
    ),

  overview: (year: number, taskTypeId: number, filter: Partial<ProgressFilter> = {}) =>
    get<{ success: boolean } & ProgressOverview>(
      `/api/progress/overview${qs({
        year,
        task_type_id: taskTypeId,
        contracts: (filter.contracts || []).join(','),
        bizs: (filter.bizs || []).join(','),
        cutoff: filter.cutoff || '',
      })}`,
    ),

  config: (year: number, taskTypeId: number) =>
    get<{ success: boolean } & ProgressConfig>(
      `/api/progress/config${qs({ year, task_type_id: taskTypeId })}`,
    ),

  quotas: (year: number, taskTypeId: number) =>
    get<{ success: boolean } & ProgressQuotaPanel>(
      `/api/progress/quotas${qs({ year, task_type_id: taskTypeId })}`,
    ),

  exportMatrix: (year: number, taskTypeId: number, filter: Partial<ProgressFilter> = {}) =>
    get<{
      success: boolean
      year: number
      task: ProgressTaskType
      scheme: ProgressOverview['scheme']
      filters: ProgressFilter
      summary: ProgressSummary
      categories: ProgressCategoryCol[]
      products: ProgressProductRef[]
      rows: ProgressMatrixRow[]
      category_totals: ProgressCategoryTotal[]
      grand_total: { quota: number; done: number; rate: number | null }
    }>(
      `/api/progress/export${qs({
        year,
        task_type_id: taskTypeId,
        contracts: (filter.contracts || []).join(','),
        bizs: (filter.bizs || []).join(','),
        cutoff: filter.cutoff || '',
      })}`,
    ),

  /* ---- 任务类型与列方案 ---- */
  saveTaskType: (payload: { id?: number; name: string; sort_no?: number; enabled?: boolean }) =>
    post<{ success: boolean }>('/api/progress/task-types', payload),
  deleteTaskType: (id: number) =>
    post<{ success: boolean; message: string }>('/api/progress/task-types/delete', { id }),

  /**
   * 建/取列方案；带 `from_year`（默认上一年）或 `from_task_type_id` 时先复制源方案的列。
   * 典型用法：新年份 → 复制上一年；市监督 → 从市例行复制一份再改。
   */
  saveScheme: (payload: {
    year: number
    task_type_id: number
    note?: string
    from_year?: number
    from_task_type_id?: number
  }) =>
    post<{ success: boolean; message: string; categories?: number; product_types?: number }>(
      '/api/progress/schemes',
      payload,
    ),
  deleteScheme: (id: number) =>
    post<{ success: boolean; message: string }>('/api/progress/schemes/delete', { id }),

  syncLog: (year: number, limit = 20) =>
    get<{ success: boolean; rows: ProgressSyncLog[] }>(`/api/progress/sync-log${qs({ year, limit })}`),

  sync: (year: number) =>
    post<{
      success: boolean
      message?: string
      rows?: number
      contract_count?: number
      unmatched?: number
      unmapped?: number
      ignored?: number
      no_contract?: number
      elapsed_ms?: number
      data_deadline?: string
    }>('/api/progress/sync', { year }),

  /** 按最新配置重算归类（改关键词/区域别名后调用，秒级、不重连镜像） */
  reclassify: (year: number) =>
    post<{ success: boolean; updated: number; unmatched: number; unmapped: number }>(
      '/api/progress/reclassify',
      { year },
    ),

  /* ---- 大类 ---- */
  saveBigKind: (payload: { name: string; keywords?: string; sort_no?: number; enabled?: boolean }) =>
    post<{ success: boolean }>('/api/progress/big-kinds', payload),
  deleteBigKind: (key: string) => post<{ success: boolean; message: string }>('/api/progress/big-kinds/delete', { key }),

  /* ---- 品类（表头列） ---- */
  saveCategory: (payload: {
    id?: number
    name: string
    big_kind?: string
    is_other?: boolean
    sort_no?: number
    enabled?: boolean
    /** 匹配关键词；不传 = 不改动 */
    keywords?: string
    /** 新增时必填：这一列属于哪个列方案（任务 × 年份） */
    scheme_id?: number
  }) => post<{ success: boolean }>('/api/progress/categories', payload),
  deleteCategory: (id: number) => post<{ success: boolean; message: string }>('/api/progress/categories/delete', { id }),
  moveCategory: (id: number, direction: 'up' | 'down') =>
    post<{ success: boolean }>('/api/progress/categories/move', { id, direction }),

  /* ---- 产品类型（二级表头） ---- */
  saveProductType: (payload: {
    id?: number
    category_id?: number
    name: string
    keywords?: string
    is_other?: boolean
    sort_no?: number
    enabled?: boolean
  }) => post<{ success: boolean }>('/api/progress/product-types', payload),
  deleteProductType: (id: number) => post<{ success: boolean; message: string }>('/api/progress/product-types/delete', { id }),
  moveProductType: (id: number, direction: 'up' | 'down') =>
    post<{ success: boolean }>('/api/progress/product-types/move', { id, direction }),
  /** 批量定义：每行 `大类-品类名[：关键词|关键词]`，写入**当前任务当年**的列方案 */
  bulkDefineProductTypes: (text: string, dryRun: boolean, year: number, taskTypeId: number) =>
    post<{
      success: boolean
      message?: string
      created_categories?: number
      updated_categories?: number
      items: { big_kind: string; name: string; is_other: boolean; keywords: string; products: string[] }[]
      errors: { line: number; text: string; reason: string }[]
    }>('/api/progress/product-types/bulk', { text, dry_run: dryRun, year, task_type_id: taskTypeId }),

  /* ---- 区域 ---- */
  saveRegion: (payload: { name: string; keys?: string; sort_no?: number; enabled?: boolean }) =>
    post<{ success: boolean }>('/api/progress/regions', payload),
  deleteRegion: (key: string) => post<{ success: boolean; message: string }>('/api/progress/regions/delete', { key }),
  /** 把「未映射区域」挂到某个看板行，并立刻重算该年归类 */
  mapRegion: (year: number, regionKey: string, region: string) =>
    post<{ success: boolean; message: string; updated: number }>('/api/progress/regions/map', {
      year,
      region_key: regionKey,
      region,
    }),

  /* ---- 模块设置 ---- */
  /** 切换区域判定优先项（county / address），后端会本地重算归类 */
  setRegionPriority: (year: number, regionPriority: 'county' | 'address') =>
    post<{ success: boolean; message: string; updated: number }>('/api/progress/settings', {
      year,
      region_priority: regionPriority,
    }),

  /* ---- 合同 ---- */
  saveContract: (payload: {
    contract_no: string
    name?: string
    note?: string
    enabled?: boolean
    /** 归并到哪个正式合同编号；'' = 取消归并；不传 = 不改动 */
    alias_of?: string
    /** 这份合同属于哪个任务（0 = 未纳入考核表）；不传 = 不改动 */
    task_type_id?: number
  }) => post<{ success: boolean }>('/api/progress/contracts', payload),
  deleteContract: (key: string) => post<{ success: boolean; message: string }>('/api/progress/contracts/delete', { key }),

  /* ---- 任务量（按任务下达） ---- */
  /** 任务量保存：每条二选一给 `product_type_id`（产品级）或 `category_id`（品类级） */
  saveQuotas: (
    year: number,
    taskTypeId: number,
    items: { region: string; product_type_id?: number; category_id?: number; quota: number }[],
  ) => post<{ success: boolean; message: string; saved: number }>('/api/progress/quotas', {
    year,
    task_type_id: taskTypeId,
    items,
  }),

  clearQuotas: (year: number, taskTypeId: number) =>
    post<{ success: boolean; message: string; cleared: number }>('/api/progress/quotas/clear', {
      year,
      task_type_id: taskTypeId,
    }),

  importQuotas: (year: number, taskTypeId: number, rows: ProgressImportRow[]) =>
    post<{ success: boolean; message: string; saved: number; errors: { row: number; reason: string }[] }>(
      '/api/progress/quotas/import',
      { year, task_type_id: taskTypeId, rows },
    ),

  /* ---- 未归类处理 ---- */
  /** 把某样品名挂到某个品类：等于给该列加一个关键词，随后立刻重算归类 */
  assignUnmatched: (year: number, sampleName: string, categoryId: number) =>
    post<{ success: boolean; message: string; updated: number }>('/api/progress/unmatched/assign', {
      year,
      sample_name: sampleName,
      category_id: categoryId,
    }),
  ignoreSample: (name: string, note = '') =>
    post<{ success: boolean; message: string }>('/api/progress/unmatched/ignore', { name, note }),
  removeIgnore: (key: string) => post<{ success: boolean; message: string }>('/api/progress/ignore/remove', { key }),
}
