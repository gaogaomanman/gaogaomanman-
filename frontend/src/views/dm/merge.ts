/**
 * 合并引擎：把 4 个模板里各自实现的「同一项目多条怎么显示 / 多个子项目怎么合并」统一成一份逻辑。
 *
 * 设计目标 = **零口径变化**：引擎用参数刻画旧代码的差异点，参数取值来自各块旧实现；
 * 默认规则下，输出与改造前逐格一致（S3 有新旧对比测试断言）。
 *
 * ## 旧实现差异对照（引擎参数 → 旧行为）
 *
 * | 模板 | 判重键 | 取值口径 | 合并方式 | anyFail 来源 |
 * |------|--------|----------|----------|--------------|
 * | 省例行农产品 | 按项目名 | numeric（须能 parseFloat） | 加权求和 | 有值项中任一条不合格 |
 * | 省例行畜产品 | 按项目名 | 非空即算有值 | 氟苯尼考求和 / 其余取首个 | 氟苯尼考：有值项；其余：首个检出那条 |
 * | 省例行水产品 | 按项目名 | 非空即算有值 | 取首个检出 | 不判（用 EVALUATE_RESULT） |
 * | 年度统计（合并组） | 整组（复刻旧缺陷） | numeric | 加权求和 | 有值项中任一条不合格 |
 *
 * ## 重复策略（`dupPolicy`）
 *
 * - `keepLines`（默认，= 旧行为）：判为重复时**分行列出全部有效值并标红**，不丢数据；
 * - `latest`：取创建时间最新（同日取 ID 最大）的那条；
 * - `preferReported`：优先取有报告值的；多条都有值且不同 → 仍分行标红；都无值 → 取最新。
 */
import type { CombineMode, DupKey, DupPolicy } from './rules'

/** 一个"列"下收集到的子项（数据库项目名 + 报告值） */
export interface SubValue {
  /** 归一化后的项目名（用于判重与系数匹配） */
  dn: string
  /** 报告值原文（可能是 `未检出(定量型:值)` 这类字符串） */
  v: string
  judge: string
  limitType?: string
  limitValue?: string
  /** 折算系数（由规则组解析得到；独立列恒为 1） */
  factor?: number
  /** 后端新增列：SP 主键（`latest` 策略的次级排序依据） */
  spId?: string
  /** 后端新增列：创建时间（`latest` 策略的首选排序依据） */
  createdAt?: string
}

/** 取值口径：`numeric` = 必须能 parseFloat（旧农产品/年度）；`nonEmpty` = 非空字符串即算（旧畜/水产品） */
export type ValueFilter = 'numeric' | 'nonEmpty'

/**
 * 不合格判定的来源（对应旧实现的差异）：
 * - `valued`：有值项中任一条不合格（农产品 / 年度）
 * - `groupSum`：组内"非空且非字面未检出"任一项不合格（畜产品氟苯尼考那类求和列）
 * - `firstDetected`：只看首个检出那条（畜产品其它列）
 * - `none`：本列不判（水产品的判定取 d.EVALUATE_RESULT）
 */
export type FailSource = 'valued' | 'groupSum' | 'firstDetected' | 'none'

export interface CellOptions {
  combine: CombineMode
  dupKey: DupKey
  dupPolicy: DupPolicy
  failSource: FailSource
  /**
   * 判为重复后要不要"有值"才分行：
   * 农产品/年度 = true（旧实现 `hasDup && anyVal`）；畜/水产品 = false（旧实现只要重复就标红）
   */
  linesRequireValue: boolean
  /** 取值口径；缺省按合并方式推导（sum → numeric，first/max/min → nonEmpty） */
  valueFilter?: ValueFilter
}

/**
 * 取值口径兜底推导：**各块都应显式传 `valueFilter`**，这里只是缺省值。
 *
 * 不能只按合并方式推导——农产品/年度块的合并组即便"取首个值"也是**数值口径**
 * （旧实现统一先 `parseFloat`，非数值即视为无值），只有畜/水产品的"取首个检出值"
 * 才是"非空即算"口径。S3 对比测试正是在 β-内酰胺酶列上抓到过这个差异。
 */
export function valueFilterOf(opts: CellOptions): ValueFilter {
  return opts.valueFilter || (opts.combine === 'sum' ? 'numeric' : 'nonEmpty')
}

export interface MergeOutcome {
  /** 是否判定为"同一项目多条" */
  duplicated: boolean
  /** 有效值（numeric 口径）折算后的数值序列，顺序与输入一致 */
  numericLines: number[]
  /** 有效值（nonEmpty 口径）的原文序列 */
  rawLines: string[]
  /** 加权求和结果（无有效值时为 0） */
  sum: number
  /** max / min 结果（无有效值时为 null） */
  extreme: number | null
  /** first 口径下"首个有值"的原文（无值时为 ''） */
  firstRaw: string
  /** 按 valueFilter 判定是否至少有一条有效值 */
  anyValue: boolean
  /** 判定不合格 */
  anyFail: boolean
  limitType: string
  limitValue: string
  /** latest / preferReported 策略选中的那一条（keepLines 时为 null） */
  picked: SubValue | null
  /** preferReported 下"多条有值但值不同"→ 无法自动取舍，仍分行标红 */
  forceLines: boolean
}

function isFailJudge(judge: unknown): boolean {
  const s = String(judge || '')
  return s.indexOf('不合格') >= 0 || s === '不符合'
}

function isNumericValue(v: string): boolean {
  if (v === '' || v === '未检出') return false
  return !isNaN(parseFloat(v))
}

/** 按 `latest` 策略排序：创建时间倒序，同日按 SP 主键倒序（字符串比较足够用）。 */
function compareLatest(a: SubValue, b: SubValue): number {
  const ta = String(a.createdAt || '')
  const tb = String(b.createdAt || '')
  if (ta !== tb) return ta < tb ? 1 : -1
  const ia = Number(a.spId || 0)
  const ib = Number(b.spId || 0)
  if (isFinite(ia) && isFinite(ib) && ia !== ib) return ib - ia
  return 0
}

/** 对一列子项执行判重与合并（纯函数，无副作用，便于测试）。 */
export function mergeValues(subs: SubValue[], opts: CellOptions): MergeOutcome {
  const limitType = (subs.find((x) => x.limitType) || {}).limitType || ''
  const limitValue = (subs.find((x) => x.limitValue) || {}).limitValue || ''

  // 判重：`group` = 整组一个桶（等价旧年度块"组内 >1 条即重复"）；`dn` = 按项目名分组
  let duplicated: boolean
  if (opts.dupKey === 'group') {
    duplicated = subs.length > 1
  } else {
    const counter = new Map<string, number>()
    subs.forEach((x) => counter.set(x.dn, (counter.get(x.dn) || 0) + 1))
    duplicated = Array.from(counter.values()).some((n) => n > 1)
  }

  const filter = valueFilterOf(opts)
  // 有效值序列：numeric 口径要求可转数字；nonEmpty 口径非空即可
  const numericItems = subs.filter((x) => isNumericValue(x.v))
  const rawItems = subs.filter((x) => x.v !== '')
  // 分行展示用的原文序列：与旧实现一致，排除字面「未检出」（"未检出(定量限:x)" 保留）
  const lineItems = rawItems.filter((x) => x.v !== '未检出')

  const numericLines = numericItems.map((x) => parseFloat(x.v) * (x.factor === undefined ? 1 : x.factor))
  const rawLines = lineItems.map((x) => x.v)

  // 求和（仅数值项参与，与旧代码 `if (isNaN(num)) continue` 等价）
  let sum = 0
  numericItems.forEach((x, i) => {
    sum += numericLines[i]
  })

  const scaled = numericLines
  const extreme = scaled.length ? (opts.combine === 'max' ? Math.max(...scaled) : Math.min(...scaled)) : null
  const firstRaw = stringFirstValue(subs, filter)

  // anyValue：按取值口径判定
  const anyValue = filter === 'numeric' ? numericItems.length > 0 : rawItems.length > 0

  // anyFail：按来源口径判定（与旧代码"在 continue 之后才判 judge"的写法等价）
  let anyFail = false
  if (opts.failSource === 'valued') {
    const valued = filter === 'numeric' ? numericItems : rawItems
    anyFail = valued.some((x) => isFailJudge(x.judge))
  } else if (opts.failSource === 'groupSum') {
    // 旧畜产品求和列：跳过空值与字面「未检出」后，其余都参与不合格判定
    anyFail = lineItems.some((x) => isFailJudge(x.judge))
  } else if (opts.failSource === 'firstDetected') {
    const first = rawItems.length ? rawItems[0] : null
    anyFail = !!first && isFailJudge(first.judge)
  }

  // 重复策略选择（keepLines 不需要挑）
  let picked: SubValue | null = null
  let forceLines = false
  if (opts.dupPolicy === 'latest') {
    picked = pickLatest(subs)
  } else if (opts.dupPolicy === 'preferReported') {
    const withValue = subs.filter((x) => x.v !== '')
    if (withValue.length > 1 && new Set(withValue.map((x) => x.v)).size > 1) {
      // 多条有值且值不同 → 无法自动取舍，仍分行列出并标红，交人工判断
      forceLines = true
    } else {
      picked = withValue.length ? pickLatest(withValue) : pickLatest(subs)
    }
  }

  return {
    duplicated,
    numericLines,
    rawLines,
    sum,
    extreme,
    firstRaw,
    anyValue,
    anyFail,
    limitType,
    limitValue,
    picked,
    forceLines,
  }
}

function stringFirstValue(subs: SubValue[], filter: ValueFilter): string {
  for (const x of subs) {
    if (filter === 'numeric') {
      if (isNumericValue(x.v)) return x.v
    } else if (x.v !== '') {
      return x.v
    }
  }
  return ''
}

function pickLatest(items: SubValue[]): SubValue | null {
  if (!items.length) return null
  return [...items].sort(compareLatest)[0] || null
}

/** 按合并方式取"单值结果"（各块据此决定是否再经过 `formatSigNum`）。 */
export function combineSingle(o: MergeOutcome, mode: CombineMode): number | null {
  if (!o.anyValue) return null
  if (mode === 'sum') return o.sum
  if (mode === 'first') {
    // first 口径：numeric 时取首个可转数字的值，nonEmpty 时取首个非空原文
    if (o.numericLines.length) return o.numericLines[0]
    const n = parseFloat(o.firstRaw)
    return isNaN(n) ? null : n
  }
  return o.extreme
}

/**
 * 渲染方案：把「判重 + 取值 + 合并 + 重复策略」的结论压缩成三选一，
 * 各块只需按 `mode` 决定用什么格式化（有效数字 / 原始值 / 未检出文案）。
 */
export interface RenderPlan {
  /** empty = 写"未检出"文案；single = 单值；lines = 分行列出 + 标红 */
  mode: 'empty' | 'single' | 'lines'
  /** single：数值结果（null 表示该值无法转数字，按 `singleRaw` 原文渲染） */
  single: number | null
  /** single：原文结果（畜/水产品"取首个检出值"用） */
  singleRaw: string
  /** lines：逐行数值（已按系数折算） */
  numericLines: number[]
  /** lines：逐行原文 */
  rawLines: string[]
  /** 是否标红 */
  red: boolean
  anyFail: boolean
  limitType: string
  limitValue: string
  /** 该列是否存在"同一项目多条" */
  duplicated: boolean
}

/** 一步到位：判重 + 取值 + 合并 + 策略 → 渲染方案。 */
export function resolveCell(subs: SubValue[], opts: CellOptions): RenderPlan {
  const o = mergeValues(subs, opts)
  const base = {
    anyFail: o.anyFail,
    limitType: o.limitType,
    limitValue: o.limitValue,
    duplicated: o.duplicated,
  }
  // latest / preferReported：能唯一确定一条 → 按单值渲染（无需标红）
  if (opts.dupPolicy !== 'keepLines' && o.picked && !o.forceLines) {
    const f = o.picked.factor === undefined ? 1 : o.picked.factor
    const num = parseFloat(o.picked.v)
    const single = isNaN(num) ? null : num * f
    return {
      mode: 'single',
      single,
      singleRaw: o.picked.v,
      numericLines: single === null ? [] : [single],
      rawLines: [o.picked.v],
      red: false,
      ...base,
    }
  }
  if (o.duplicated && (!opts.linesRequireValue || o.anyValue)) {
    return {
      mode: 'lines',
      single: null,
      singleRaw: '',
      numericLines: o.numericLines,
      rawLines: o.rawLines,
      red: true,
      ...base,
    }
  }
  if (!o.anyValue) {
    return {
      mode: 'empty',
      single: null,
      singleRaw: '',
      numericLines: [],
      rawLines: [],
      red: false,
      ...base,
    }
  }
  return {
    mode: 'single',
    single: combineSingle(o, opts.combine),
    singleRaw: o.firstRaw,
    numericLines: o.numericLines,
    rawLines: o.rawLines,
    red: false,
    ...base,
  }
}
