/**
 * LIMS 查询模板规则层（A 类别名映射 + B 类多项目合并折算）。
 *
 * 数据来源：后端 `/api/lims-rules`（SQLite + 版本号，见 `backend/app/modules/limsrules/`）。
 *
 * ## 零口径变化原则
 *
 * 1. 后端接口不可用（离线、服务未起、超时）时，**退回本文件内嵌的 `BUILTIN_DEFAULTS`**，
 *    达梦页照常导出，不因规则服务故障而中断业务；页面会显示"规则来源：内置默认"。
 * 2. `BUILTIN_DEFAULTS` 必须与 `backend/app/modules/limsrules/defaults.py` 的 `TEMPLATES`
 *    **逐条逐值一致**（S3 测试里有前后端默认一致性断言，防止两边漂移）。
 * 3. 内嵌默认 = 改造前各块硬编码常量的原样搬迁，因此"从未保存过规则"时导出的结果与旧版逐格相同。
 *
 * ## 两层别名（模板优先）
 *
 * 旧代码里畜产品块专门用自己那份 map 覆盖全局表（「碱类物质」→「碱性物质」是错的，
 * 畜产品要保留「碱类物质」）。因此解析顺序固定为：全局表先应用 → 模板表后应用。
 */
import { limsRulesApi } from '../../api/client'

export type CombineMode = 'sum' | 'first' | 'max' | 'min'
/**
 * 判重分组键。当前只使用 `dn`（按数据库项目名分组，即只把"同一项目多条"视为重复）。
 * `group`（整组一个桶）曾用于复刻年度块的旧缺陷，**已于 2026-09-22 取消**，
 * 类型与引擎参数保留仅为兼容历史版本读取（读取时统一归一为 `dn`）。
 */
export type DupKey = 'dn' | 'group'
export type DupPolicy = 'keepLines' | 'latest' | 'preferReported'

export interface RuleMember {
  name: string
  factor: string
}

export interface RuleGroup {
  target: string
  members: RuleMember[]
  combine?: CombineMode
  dupKey?: DupKey
  dupPolicy?: DupPolicy
}

export interface TemplateRule {
  name: string
  scope: 'global' | 'template'
  note?: string
  nameMap: Record<string, string>
  groups: RuleGroup[]
  defaultCombine: CombineMode
  /** 该模板的判重方式被固化（年度块复刻旧版疑似缺陷），页面不允许改 */
  forcedDupKey?: DupKey
}

export interface RuleMeta {
  version: number
  source: 'builtin' | 'backend'
  updatedAt: string
  updatedBy: string
  note: string
}

/** 模板的**固有匹配行为**（不是业务规则，故不入规则库，避免被误改导致匹配失效） */
export const TEMPLATE_MATCH: Record<
  string,
  { useBaseName: boolean; fixedColumns: boolean; inheritGlobalNameMap: boolean }
> = {
  // 农产品：只做精确匹配，不做"去括号基础名"匹配
  provinceAgri: { useBaseName: false, fixedColumns: true, inheritGlobalNameMap: true },
  // 畜产品 / 水产品：列名去掉括号后与项目名相等也算命中
  provinceLivestock: { useBaseName: true, fixedColumns: true, inheritGlobalNameMap: true },
  provinceAquatic: { useBaseName: true, fixedColumns: true, inheritGlobalNameMap: true },
  // 年度：列名由数据动态生成，不参与"列名匹配"，只做合并组归并；
  // 且旧实现只用自己的 2 条别名表、**不查全局别名**，故 inheritGlobalNameMap = false
  yearlyStats: { useBaseName: false, fixedColumns: false, inheritGlobalNameMap: false },
}

export const TEMPLATE_LABELS: Record<string, string> = {
  __global__: '全局别名（所有模板共用）',
  provinceAgri: '省例行农产品',
  provinceLivestock: '省例行畜产品',
  provinceAquatic: '省例行水产品',
  yearlyStats: '年度数据快速统计',
}

/* ==================== 内置默认（= 改造前硬编码常量，勿随意改动） ==================== */

function m(name: string, factor: string): RuleMember {
  return { name, factor }
}

function g(
  target: string,
  members: RuleMember[],
  extra?: Partial<Pick<RuleGroup, 'combine' | 'dupKey' | 'dupPolicy'>>,
): RuleGroup {
  return { target, members, combine: 'sum', dupKey: 'dn', dupPolicy: 'keepLines', ...extra }
}

/** 农产品 / 年度共用的 7 组（折算系数 = 主项目分子量 / 子项目分子量） */
const AGRI_LIKE_GROUPS: RuleGroup[] = [
  g('甲拌磷（包括甲拌磷砜和甲拌磷亚砜）', [
    m('甲拌磷', '1'), m('甲拌磷砜', '260.38/292.38'), m('甲拌磷亚砜', '260.38/276.38'),
  ]),
  g('克百威（包括3-羟基克百威）', [m('克百威', '1'), m('3-羟基克百威', '221.25/237.25')]),
  g('涕灭威（包括涕灭威砜和涕灭威亚砜）', [
    m('涕灭威', '1'), m('涕灭威砜', '190.26/222.26'), m('涕灭威亚砜', '190.26/206.26'),
  ]),
  g('氟虫腈（包括氟甲腈氟虫腈硫醚氟虫腈砜）', [
    m('氟虫腈', '1'), m('氟虫腈砜', '437.15/453.15'), m('氟虫腈亚砜', '437.15/421.15'), m('氟甲腈', '437.15/389.08'),
  ]),
  g('乙基多杀菌素', [m('乙基多杀菌素', '1'), m('乙基多杀菌素J', '1'), m('乙基多杀菌素L', '1')]),
  g('多杀霉素', [m('多杀霉素', '1'), m('多杀霉素A', '1'), m('多杀霉素D', '1')]),
  g('三唑酮', [m('三唑酮', '1'), m('三唑醇', '1')]),
]

/** 农产品块：比上面多一个「氟虫腈硫醚」（旧代码 FACTORS 未定义它，回退系数 1） */
const AGRI_GROUPS: RuleGroup[] = [
  AGRI_LIKE_GROUPS[0],
  AGRI_LIKE_GROUPS[1],
  AGRI_LIKE_GROUPS[2],
  g('氟虫腈（包括氟甲腈氟虫腈硫醚氟虫腈砜）', [
    m('氟虫腈', '1'), m('氟甲腈', '437.15/389.08'), m('氟虫腈硫醚', '1'),
    m('氟虫腈砜', '437.15/453.15'), m('氟虫腈亚砜', '437.15/421.15'),
  ]),
  AGRI_LIKE_GROUPS[4],
  AGRI_LIKE_GROUPS[5],
  AGRI_LIKE_GROUPS[6],
]

export const BUILTIN_DEFAULTS: Record<string, TemplateRule> = {
  __global__: {
    name: TEMPLATE_LABELS.__global__,
    scope: 'global',
    note: '所有模板共用；模板自己的别名优先于这里。',
    nameMap: {
      '呋喃唑酮代谢物[AOZ]': '呋喃唑酮代谢物',
      '呋喃它酮代谢物[AMOZ]': '呋喃它酮代谢物',
      '呋喃妥因代谢物[AHD]': '呋喃妥因代谢物',
      '呋喃西林代谢物[SEM]': '呋喃西林代谢物',
      '磺胺甲基异噁唑（磺胺甲噁唑）': '磺胺甲基异噁唑',
      '磺胺多辛（磺胺邻二甲氧嘧啶）': '磺胺多辛',
      '磺胺间甲氧嘧啶（磺胺-6-甲氧嘧啶）': '磺胺间甲氧嘧啶',
      '磺胺二甲氧嘧啶（磺胺间二甲氧嘧啶、磺胺二甲氧哒嗪）': '磺胺间二甲氧嘧啶',
      碱类物质: '碱性物质',
      克伦特罗: '克仑特罗',
    },
    groups: [],
    defaultCombine: 'sum',
  },
  provinceAgri: {
    name: TEMPLATE_LABELS.provinceAgri,
    scope: 'template',
    note: '73 个固定项目列；合并组按分子量比值折算后求和。',
    nameMap: {},
    groups: AGRI_GROUPS,
    defaultCombine: 'sum',
  },
  provinceLivestock: {
    name: TEMPLATE_LABELS.provinceLivestock,
    scope: 'template',
    note: '38 个固定项目列；氟苯尼考与氟苯尼考胺加和，其余取首个检出值。',
    nameMap: {
      强力霉素: '多西环素',
      磺胺间二甲氧嘧啶: '磺胺二甲氧嘧啶',
      '磺胺二甲氧嘧啶（磺胺间二甲氧嘧啶、磺胺二甲氧哒嗪）': '磺胺二甲氧嘧啶',
      '磺胺间甲氧嘧啶（磺胺-6-甲氧嘧啶）': '磺胺间甲氧嘧啶',
      '磺胺甲基异噁唑（磺胺甲噁唑）': '磺胺甲噁唑',
      克伦特罗: '克仑特罗',
      '呋喃唑酮代谢物[AOZ]': '呋喃唑酮代谢物',
      二甲硝咪唑: '地美硝唑',
      '二甲硝咪唑（地美硝唑）': '地美硝唑',
      '羟基二甲硝咪唑（羟基地美硝唑）': '羟基地美硝唑',
      碱类物质: '碱类物质',
    },
    groups: [
      g('氟苯尼考（氟苯尼考+氟苯尼考胺）', [m('氟苯尼考', '1'), m('氟苯尼考胺', '1')]),
      g('β-内酰胺酶(单位：U/ml)', [m('β-内酰胺酶', '1')], { combine: 'first' }),
    ],
    defaultCombine: 'first',
  },
  provinceAquatic: {
    name: TEMPLATE_LABELS.provinceAquatic,
    scope: 'template',
    note: '33 个固定兽药列，均为独立列（不合并），取值取首个检出。',
    nameMap: {
      '磺胺间甲氧嘧啶（磺胺-6-甲氧嘧啶）': '磺胺间甲氧嘧啶',
      '磺胺二甲氧嘧啶（磺胺间二甲氧嘧啶、磺胺二甲氧哒嗪）': '磺胺间二甲氧嘧啶',
      '磺胺甲基异噁唑（磺胺甲噁唑）': '磺胺甲基异噁唑',
      '呋喃唑酮代谢物[AOZ]': '呋喃唑酮代谢物',
      '呋喃西林代谢物[SEM]': '呋喃西林代谢物',
      '呋喃妥因代谢物[AHD]': '呋喃妥因代谢物',
      '呋喃它酮代谢物[AMOZ]': '呋喃它酮代谢物',
      强力霉素: '多西环素',
      '磺胺多辛（磺胺邻二甲氧嘧啶）': '磺胺多辛',
    },
    groups: [],
    defaultCombine: 'first',
  },
  yearlyStats: {
    name: TEMPLATE_LABELS.yearlyStats,
    scope: 'template',
    note: '动态项目列；合并组同农产品，另含畜产品两组。',
    nameMap: { 克伦特罗: '克仑特罗', 盐酸克伦特罗: '克仑特罗' },
    // 【2026-09-22 口径变更】原先为复刻旧代码缺陷，年度块每个组都设了「整组判重」，
    // 导致组内多条只分行标红、不做合并求和。现已取消该口径：与农产品块一致，
    // 一律「按项目名判重」，组内各子项按系数合并为一个值。
    groups: [
      ...AGRI_LIKE_GROUPS,
      g('氟苯尼考（氟苯尼考+氟苯尼考胺）', [m('氟苯尼考', '1'), m('氟苯尼考胺', '1')]),
      g('β-内酰胺酶', [m('β-内酰胺酶', '1')], { combine: 'first' }),
    ],
    defaultCombine: 'sum',
  },
}

/* ==================== 加载与缓存 ==================== */

let loaded = false
let loading: Promise<void> | null = null
let cache: Record<string, TemplateRule> = {}
let meta: Record<string, RuleMeta> = {}
let source: 'builtin' | 'backend' = 'builtin'
let lastError = ''

function normalizePayload(templateId: string, payload: Record<string, unknown>): TemplateRule {
  const base = BUILTIN_DEFAULTS[templateId]
  const nameMap: Record<string, string> = {}
  const rawMap = (payload.nameMap || {}) as Record<string, unknown>
  Object.keys(rawMap).forEach((k) => {
    const v = rawMap[k]
    if (typeof v === 'string' && v.trim()) nameMap[k] = v.trim()
  })
  const groups: RuleGroup[] = []
  const rawGroups = (payload.groups || []) as unknown[]
  rawGroups.forEach((item) => {
    if (!item || typeof item !== 'object') return
    const obj = item as Record<string, unknown>
    const target = typeof obj.target === 'string' ? obj.target : ''
    if (!target) return
    const members: RuleMember[] = []
    const rawMembers = (obj.members || []) as unknown[]
    rawMembers.forEach((mm) => {
      if (!mm || typeof mm !== 'object') return
      const mo = mm as Record<string, unknown>
      const nm = typeof mo.name === 'string' ? mo.name : ''
      if (!nm) return
      members.push({ name: nm, factor: mo.factor === undefined ? '1' : String(mo.factor) })
    })
    if (!members.length) return
    groups.push({
      target,
      members,
      combine: (obj.combine as CombineMode) || base?.defaultCombine || 'sum',
      // 判重方式固定为「按项目名」：历史版本里若残留 group，也在前端归一
      dupKey: 'dn',
      dupPolicy: (obj.dupPolicy as DupPolicy) || 'keepLines',
    })
  })
  const def: TemplateRule = {
    name: (typeof payload.name === 'string' && payload.name) || base?.name || templateId,
    scope: (payload.scope as 'global' | 'template') || base?.scope || 'template',
    note: typeof payload.note === 'string' ? payload.note : base?.note || '',
    nameMap,
    groups,
    defaultCombine: (payload.defaultCombine as CombineMode) || base?.defaultCombine || 'sum',
  }
  if (base?.forcedDupKey) def.forcedDupKey = base.forcedDupKey
  return def
}

/**
 * 拉取规则（幂等，带并发去重）。后端不可用时**保留上一次成功的规则**（若有），
 * 否则退回内置默认——不因规则服务故障中断导出。
 */
export async function ensureRules(force = false): Promise<void> {
  if (loaded && !force) return
  if (loading && !force) return loading
  loading = (async () => {
    try {
      const resp = await limsRulesApi.list()
      if (resp.success && Array.isArray(resp.templates)) {
        const nextCache: Record<string, TemplateRule> = {}
        const nextMeta: Record<string, RuleMeta> = {}
        resp.templates.forEach((t) => {
          if (!t || !t.templateId || !t.payload) return
          nextCache[t.templateId] = normalizePayload(t.templateId, t.payload)
          nextMeta[t.templateId] = {
            version: Number(t.version || 0),
            source: t.source === 'custom' ? 'backend' : 'builtin',
            updatedAt: t.updatedAt || '',
            updatedBy: t.updatedBy || '',
            note: t.note || '',
          }
        })
        if (Object.keys(nextCache).length) {
          cache = nextCache
          meta = nextMeta
          source = 'backend'
          lastError = ''
        }
      } else {
        lastError = resp.error || '规则接口返回失败'
      }
    } catch (e) {
      lastError = (e as Error).message || '规则接口不可用'
    } finally {
      loaded = true
      loading = null
    }
  })()
  return loading
}

/** 同步取规则（未加载或加载失败 → 内置默认）。 */
export function getRule(templateId: string): TemplateRule {
  return cache[templateId] || BUILTIN_DEFAULTS[templateId]
}

export function getRulesMeta(templateId: string): RuleMeta {
  return (
    meta[templateId] || {
      version: 0,
      source: 'builtin',
      updatedAt: '',
      updatedBy: '',
      note: '',
    }
  )
}

export function rulesSource(): 'builtin' | 'backend' {
  return source
}

export function rulesLoadError(): string {
  return lastError
}

/** 全局别名表（每个模板都要先应用它）。 */
export function globalRule(): TemplateRule {
  return getRule('__global__')
}

/* ==================== 解析工具 ==================== */

/** 去括号基础名：`β-内酰胺酶(单位：U/ml)` → `β-内酰胺酶` */
export function baseName(column: string): string {
  return column.replace(/（.*）/g, '').replace(/\(.*\)/g, '')
}

/** 折算系数：支持「数字」与「分子/分母」；非法值退回 1（与旧代码 fallback 一致）。 */
export function parseFactor(text: unknown): number {
  if (typeof text === 'number') return isFinite(text) ? text : 1
  const s = String(text ?? '').trim()
  if (!s) return 1
  if (s.indexOf('/') >= 0) {
    const parts = s.split('/')
    if (parts.length === 2) {
      const a = parseFloat(parts[0])
      const b = parseFloat(parts[1])
      if (isFinite(a) && isFinite(b) && b !== 0) return a / b
    }
    return 1
  }
  const n = parseFloat(s)
  return isFinite(n) ? n : 1
}

/** 两层别名解析：全局先、模板后（模板优先）。 */
export function resolveName(templateId: string, dbName: string): string {
  const s = String(dbName ?? '').trim()
  if (!s) return ''
  const tMap = getRule(templateId).nameMap
  if (tMap[s]) return tMap[s]
  if (TEMPLATE_MATCH[templateId]?.inheritGlobalNameMap !== false) {
    const gMap = globalRule().nameMap
    if (gMap[s]) return gMap[s]
  }
  return s
}

/** 按成员项目名找合并组（年度块用：列名即 target）。 */
export function findGroupByMember(groups: RuleGroup[], dn: string): RuleGroup | null {
  for (const gp of groups) {
    if (gp.target === dn) return gp
    if (gp.members.some((x) => x.name === dn)) return gp
  }
  return null
}

/** 按列名找合并组。 */
export function findGroupByTarget(groups: RuleGroup[], column: string): RuleGroup | null {
  for (const gp of groups) if (gp.target === column) return gp
  return null
}

/** 取某项目在合并组里的折算系数（不在组内 → 1）。 */
export function factorOf(group: RuleGroup | null, dn: string): number {
  if (!group) return 1
  const hit = group.members.find((x) => x.name === dn)
  return hit ? parseFactor(hit.factor) : 1
}

/**
 * 固定列模板的「项目名 → 模板列名」匹配（三级，与旧代码逐级顺序一致）：
 * 1) 列名精确匹配（畜/水产品允许"列名去括号后相等"）
 * 2) 列名包含项目名
 * 3) 合并组成员精确匹配 → 归到该组的 target 列
 */
export function resolveColumn(
  templateId: string,
  dn: string,
  columns: string[],
  groups: RuleGroup[],
): { column: string; group: RuleGroup | null } | null {
  if (!dn) return null
  const useBase = TEMPLATE_MATCH[templateId]?.useBaseName === true
  let col = columns.find((x) => x === dn || (useBase && baseName(x) === dn))
  if (col) return { column: col, group: findGroupByTarget(groups, col) }
  col = columns.find((x) => x.indexOf(dn) >= 0)
  if (col) return { column: col, group: findGroupByTarget(groups, col) }
  for (const gp of groups) {
    if (gp.members.some((x) => x.name === dn)) return { column: gp.target, group: gp }
  }
  return null
}
