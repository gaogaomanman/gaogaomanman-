/**
 * 达梦页共享工具层（移植自 20260605160640/sql-query.html 的全局工具函数）。
 *
 * 迁移原则：SQL 拼接、列顺序、数据整形规则与导出样式**逐字保留**，仅把
 * `document.getElementById(...)` 取值改为由调用方以参数传入（Vue 侧用响应式状态）。
 * 函数名与签名尽量与原文一致，便于逐块对照验收。
 */
import ExcelJS from 'exceljs'
import { dmApi, type DmQueryResult } from '../../api/client'

/* ==================== 达梦查询薄封装（原 dmQuery） ==================== */
export async function dmQuery(sql: string): Promise<DmQueryResult> {
  return dmApi.query(sql)
}

/* ==================== 检测项目名称映射（数据库名 → 导出名） ==================== */
export const PESTICIDE_NAME_MAP: Record<string, string> = {
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
}

/* ==================== 检测项目排序 + 分组颜色（原 reorderPesticides） ==================== */
export interface ReorderResult {
  reordered: string[]
  colorMap: Record<string, string>
}

export function reorderPesticides(pesticides: string[], unitMap: Record<string, string>): ReorderResult {
  const groups = [
    { items: ['3-羟基克百威', '克百威'], color: 'FFC7CE' },
    { items: ['三唑酮', '三唑醇'], color: 'C6EFCE' },
    { items: ['乙基多杀菌素J', '乙基多杀菌素L'], color: 'BDD7EE' },
    { items: ['倍硫磷', '倍硫磷亚砜', '倍硫磷砜'], color: 'FCE4D6' },
    { items: ['多杀霉素A', '多杀霉素D'], color: 'E2D0F0' },
    { items: ['氟甲腈', '氟虫腈', '氟虫腈亚砜', '氟虫腈砜'], color: 'D4E6F1' },
    { items: ['涕灭威', '涕灭威亚砜', '涕灭威砜'], color: 'FFF2CC' },
    { items: ['甲拌磷', '甲拌磷亚砜', '甲拌磷砜'], color: 'D9D9D9' },
  ]
  const result = [...pesticides]
  const colorMap: Record<string, string> = {}
  for (const group of groups) {
    for (const item of group.items) {
      if (pesticides.includes(item)) {
        colorMap[item + (unitMap[item] || '')] = group.color
        colorMap[item] = group.color
      }
    }
  }
  for (const group of groups) {
    const positions = group.items.map((p) => result.indexOf(p)).filter((i) => i >= 0)
    if (positions.length <= 1) continue
    const sortedPos = positions.sort((a, b) => a - b)
    const firstIdx = sortedPos[0]
    const lastIdx = sortedPos[sortedPos.length - 1]
    if (lastIdx - firstIdx === sortedPos.length - 1) continue
    const groupItems = sortedPos.map((i) => result[i])
    sortedPos.reverse().forEach((i) => result.splice(i, 1))
    result.splice(firstIdx, 0, ...groupItems)
  }
  return { reordered: result, colorMap }
}

/* ==================== 年度数据快速统计：产品类别映射 ==================== */
export const NEW_KIND_MAP: Record<string, string[]> = {
  农产品: ['农产品', '小麦', '水果', '稻谷', '茶叶', '蔬果', '蔬菜', '谷物', '食用菌'],
  畜产品: ['尿液', '牛羊肉', '牛肉', '牛肉（肝）', '牛肝', '猪肉', '猪肉（肝）', '猪肝', '生鲜乳', '畜产品', '禽肉', '禽肉（蛋）', '禽蛋', '羊肉', '肉类'],
  水产品: ['水产品', '虾蟹类', '鱼类'],
  土壤: ['土壤'],
  水质: ['水质'],
  肥料: ['肥料'],
  饲料: ['饲料'],
}
export const NEW_KIND_NAMES = Object.keys(NEW_KIND_MAP)

/** 合同编号条件（逗号分隔多值，中文逗号兼容，模糊匹配）——原文逐字保留 */
export function buildContractCond(contractInput: string): string {
  const raw = String(contractInput || '').trim()
  if (!raw) return ''
  const parts = raw
    .split(/[,，;；]/)
    .map((p) => p.trim())
    .filter(Boolean)
  if (parts.length === 0) return ''
  return ' AND (' + parts.map((p) => `d.CONTRACTS_NO LIKE '%${p.replace(/'/g, "''")}%'`).join(' OR ') + ')'
}

/* ==================== 编号工具（原 sqlInList / parseNoList / detectNoType …） ==================== */
export function sqlInList(arr: unknown[]): string {
  return arr.map((v) => "'" + String(v).replace(/'/g, "''") + "'").join(',')
}

/** 解析批量编号：支持 Excel 整列粘贴（换行/Tab），兼容逗号、分号分隔；自动去重、去表头 */
export function parseNoList(text: string): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  const HEADER = /^(样品编号|报检编号|报验编号|原始编号|检测编号|编号|序号|no\.?|sample\s*no\.?)$/i
  String(text || '')
    .split(/[\r\n\t,，;；]+/)
    .forEach((tok) => {
      tok = tok.trim()
      if (!tok) return
      const pieces = /[\u4e00-\u9fa5]/.test(tok) ? [tok] : tok.split(/\s+/)
      pieces.forEach((p) => {
        p = p.trim()
        if (!p || HEADER.test(p) || seen.has(p)) return
        seen.add(p)
        out.push(p)
      })
    })
  return out
}

/** 自动识别编号类型：含中文 或 极短纯数字(如 01) 判为原始编号，其余判为报检编号+小号 */
export function detectNoType(values: string[]): string {
  let keyLike = 0
  values.forEach((v) => {
    const hasChinese = /[\u4e00-\u9fa5]/.test(v)
    const shortNumber = /^\d{1,6}$/.test(v)
    if (!hasChinese && !shortNumber) keyLike += 1
  })
  return keyLike * 2 >= values.length ? 'DETECTION_KEY' : 'ORIGINAL_NO'
}

export function resolveNoType(values: string[], selected: string): string {
  return selected === 'auto' ? detectNoType(values) : selected
}

export function noTypeLabel(col: string): string {
  return col === 'ORIGINAL_NO' ? '原始编号' : '报检编号+小号'
}

/** 报检编号+小号 组合键，例如 WN26090028 + 01 = WN2609002801 */
export function keySelect(col: string): string {
  return col === 'ORIGINAL_NO' ? 's.ORIGINAL_NO' : 's.DETECTION_NO || s.SMALL_NO'
}

/** 组合键模式同时兼容只填报检编号（不带小号） */
export function whereIn(col: string, part: string[]): string {
  const list = sqlInList(part)
  if (col === 'ORIGINAL_NO') return 's.ORIGINAL_NO IN (' + list + ')'
  return '(s.DETECTION_NO || s.SMALL_NO IN (' + list + ') OR s.DETECTION_NO IN (' + list + '))'
}

/* ==================== 任务编号模糊搜索（原 fuzzySearchTasks / fuzzySearchNos） ==================== */
export async function fuzzySearchTasks(keyword: string): Promise<{ tasks?: string[]; error?: string }> {
  try {
    const d = await dmQuery(
      'SELECT DISTINCT TASK_NO FROM DETECTION.DT_DETECTION ' +
        "WHERE TASK_NO LIKE '%" + keyword.replace(/'/g, "''") + "%' " +
        'AND IS_DELETED = 0 AND TASK_NO IS NOT NULL ORDER BY TASK_NO',
    )
    if (!d.success) return { error: d.error || '未知错误' }
    return { tasks: (d.rows || []).map((x) => x[0] as string).filter(Boolean) }
  } catch (e) {
    return { error: (e as Error).message }
  }
}

export async function fuzzySearchNos(col: string, keyword: string): Promise<{ nos?: string[]; error?: string }> {
  const kw = keyword.replace(/'/g, "''")
  const sql =
    col === 'ORIGINAL_NO'
      ? 'SELECT DISTINCT ORIGINAL_NO FROM DETECTION.DT_SAMPLE ' +
        "WHERE ORIGINAL_NO LIKE '%" + kw + "%' AND IS_DELETED = 0 AND ORIGINAL_NO IS NOT NULL ORDER BY ORIGINAL_NO"
      : 'SELECT DISTINCT DETECTION_NO || SMALL_NO FROM DETECTION.DT_SAMPLE ' +
        "WHERE (DETECTION_NO LIKE '%" + kw + "%' OR DETECTION_NO || SMALL_NO LIKE '%" + kw + "%') " +
        'AND IS_DELETED = 0 ORDER BY 1'
  const d = await dmQuery(sql)
  if (!d.success) return { error: d.error || '未知错误' }
  return { nos: (d.rows || []).map((x) => x[0] as string).filter(Boolean) }
}

/** 编号超过 300 个时自动分批查询，避免单条 SQL 过长（原 collectByChunks） */
export async function collectByChunks(
  col: string,
  values: string[],
  makeSql: (col: string, part: string[]) => string,
  onProgress?: (done: number, total: number) => void,
): Promise<{ columns?: string[]; rows: unknown[][]; error?: string }> {
  const CHUNK = 300
  let rows: unknown[][] = []
  let columns: string[] | undefined
  for (let i = 0; i < values.length; i += CHUNK) {
    const part = values.slice(i, i + CHUNK)
    const r = await dmQuery(makeSql(col, part))
    if (!r.success) return { error: r.error, rows }
    if (!columns) columns = r.columns
    rows = rows.concat(r.rows || [])
    if (values.length > CHUNK && onProgress) onProgress(Math.min(i + CHUNK, values.length), values.length)
  }
  return { columns, rows }
}

/* ==================== 地址 / 环节 / 地点归类（原 parseAddressLevels 等） ==================== */
export interface AddressLevels {
  prov: string
  city: string
  county: string
}

/** 从 "江苏省苏州市常熟市..." 解析 省/市/县（支持省+市+县、直辖市、仅市+区等） */
export function parseAddressLevels(addr: string): AddressLevels {
  if (!addr) return { prov: '', city: '', county: '' }
  let s = addr.trim()
  let prov = ''
  let city = ''
  let county = ''

  const pm = s.match(/^(.+?(?:省|自治区|特别行政区))/)
  if (pm) {
    prov = pm[1]
    s = s.substring(pm[1].length)
  }

  const cm = s.match(/^(.+?市)/)
  if (cm) {
    city = cm[1]
    s = s.substring(cm[1].length)
  }

  const mm = s.match(/^(.+?(?:区|县|市|旗))/)
  if (mm) {
    county = mm[1]
    s = s.substring(mm[1].length)
  }

  if (!prov && city) {
    prov = city
    city = ''
    if (county && county.endsWith('区')) {
      // city 保持空（直辖市，如 北京市朝阳区 -> prov=北京市, county=朝阳区）
    }
  }
  return { prov, city, county }
}

/** 抽样环节归类：生产/运输/销售/屠宰/储藏 五种 */
export function mapAquaLoop(val: unknown): string {
  if (!val) return ''
  const v = String(val)
  if (v.indexOf('运输') >= 0) return '运输环节'
  if (v.indexOf('屠宰') >= 0) return '屠宰环节'
  if (v.indexOf('冷库') >= 0 || v.indexOf('仓储') >= 0 || v.indexOf('仓库') >= 0 || v.indexOf('储藏') >= 0) return '储藏环节'
  if (
    v.indexOf('市场') >= 0 || v.indexOf('超市') >= 0 || v.indexOf('专卖店') >= 0 ||
    v.indexOf('交易') >= 0 || v.indexOf('展会') >= 0
  ) return '销售环节'
  return '生产环节'
}

/** 抽样地点归类：根据企业名称判断散户/家庭农场/合作社，其余按16种地点类型归类 */
export function mapAquaPlace(enterprise: unknown, samplingPos: unknown): string {
  const ent = String(enterprise || '').trim()
  if (ent) {
    if (ent.indexOf('养殖户') >= 0) return '散户'
    if (ent.indexOf('家庭农场') >= 0) return '家庭农场'
    if (ent.indexOf('合作社') >= 0) return '合作社（公司）'
    if (
      !/公司|经营部|批发|零售|市场|摊位|店|合作社|家庭农场|养殖户|厂|基地|中心|商行|配送|有限公司|水产|杂货|集团/.test(ent) &&
      ent.length <= 6
    ) {
      return '散户'
    }
  }
  const v = String(samplingPos || '')
  if (v.indexOf('散户') >= 0 || v === '个人') return '散户'
  if (v.indexOf('家庭农场') >= 0) return '家庭农场'
  if (v.indexOf('合作社') >= 0) return '合作社（公司）'
  if (v.indexOf('茶叶初制') >= 0) return '茶叶初制加工厂'
  if (v.indexOf('捕捞') >= 0 || v.indexOf('渔船') >= 0) return '捕捞渔船'
  if (v.indexOf('批发市场') >= 0) return '批发市场'
  if (v.indexOf('农贸市场') >= 0 || v.indexOf('菜市场') >= 0) return '农贸市场'
  if (v.indexOf('超市') >= 0) return '超市'
  if (v.indexOf('专卖店') >= 0) return '专卖店'
  if (v.indexOf('茶叶市场') >= 0) return '茶叶市场'
  if (v.indexOf('收购站') >= 0 || v.indexOf('收购') >= 0) return '收购站'
  if (v.indexOf('冷库') >= 0 || v.indexOf('仓储') >= 0 || v.indexOf('仓库') >= 0) return '仓储公司冷库'
  if (v.indexOf('养殖场') >= 0) return '养殖场'
  if (v.indexOf('屠宰场') >= 0 || v.indexOf('屠宰') >= 0) return '屠宰场'
  if (v.indexOf('暂养') >= 0) return '暂养池'
  if (v.indexOf('产地交易市场') >= 0 || (v.indexOf('产地') >= 0 && v.indexOf('交易') >= 0) || v.indexOf('田头交易') >= 0) {
    return '产地交易市场'
  }
  if (
    v.indexOf('养殖') >= 0 || v.indexOf('基地') >= 0 || v.indexOf('塘') >= 0 || v.indexOf('鱼塘') >= 0 ||
    v.indexOf('田块') >= 0 || v.indexOf('散养') >= 0 || v.indexOf('圈养') >= 0
  ) return '养殖场'
  if (/村|组|号|镇|街道|区|县|市/.test(v)) return '养殖场'
  return v
}

/* ==================== 样式化 Excel 导出（原 createStyledExcel，逐字保留） ==================== */
export type CellValue = string | number

export async function createStyledExcel(
  headers: string[],
  dataRows: CellValue[][],
  sheetName: string,
  fileName: string,
  headerColors: Record<string, string> | null,
  redCells?: Set<string> | null,
): Promise<void> {
  const wb = new ExcelJS.Workbook()
  wb.creator = '数据导出'
  const ws = wb.addWorksheet(sheetName)

  const thin = { style: 'thin' as const }
  const border = { top: thin, left: thin, bottom: thin, right: thin }
  const align = { horizontal: 'center' as const, vertical: 'middle' as const, wrapText: true }
  const fontData = { name: 'Times New Roman', size: 10 }
  const fontRed = { name: 'Times New Roman', size: 10, bold: true, color: { argb: 'FFFF0000' } }
  const fontHeader = { name: 'Times New Roman', size: 10, bold: true }

  const hRow = ws.addRow(headers)
  hRow.eachCell((c, colIdx) => {
    c.font = fontHeader
    c.alignment = align
    c.border = border
    const hText = headers[colIdx - 1]
    if (headerColors && headerColors[hText]) {
      c.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF' + headerColors[hText] } }
    }
  })

  dataRows.forEach((row, rowIdx) => {
    const r = ws.addRow(row)
    r.eachCell((c, colIdx) => {
      c.font = fontData
      c.alignment = align
      c.border = border
      if (redCells && redCells.has(rowIdx + '_' + (colIdx - 1))) {
        c.font = fontRed
        c.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFFFE1E1' } }
      }
    })
  })

  ws.columns.forEach((col, idx) => {
    let maxLen = 10
    ;[headers, ...dataRows].forEach((row) => {
      const v = row[idx]
      if (v != null) maxLen = Math.max(maxLen, String(v).length)
    })
    col.width = Math.min(maxLen + 3, 40)
  })

  const buf = await wb.xlsx.writeBuffer()
  const url = URL.createObjectURL(new Blob([buf]))
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  a.click()
  URL.revokeObjectURL(url)
}
