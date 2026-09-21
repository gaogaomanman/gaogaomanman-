/**
 * 《检验检测能力表》解析器（exceljs）。
 *
 * 用途：把页面上传的能力表解析成 `{ project, method }` 明细（**项目 + 依据的标准（方法）编号**），
 * 交给后端与人员登记的检测项目/标准逐行比对。
 *
 * 为什么在前端解析：`exceljs` 已是本工程依赖（CMA 页「能力表导入」同款），后端 requirements 没有
 * xlsx 解析库，没必要为 1270 行数据再加一个依赖与一套环境；解析后只上传结构化明细（JSON < 200KB）。
 *
 * 表头识别（不写死行列号）：
 * - 支持**双层合并表头**（样例：第 1 行「填写说明」、第 2~3 行合并表头、数据自第 4 行起）——
 *   定位到 `编号（含年号）`（方法列）后，其左侧同级还有一个 `名称`（依据的标准（方法）的名称），
 *   再往左的那个 `名称` 才是 `产品/项目/参数` 的名称，即项目列；
 * - 也支持单层表头（`检测项目` / `检测标准` 这类直白列名）；
 * - 找不到表头 / 表头下无数据时**抛可读错误**，而不是静默产出 0 条。
 */
import ExcelJS from 'exceljs'

export interface AbilityPair {
  project: string
  method: string
}

export interface ParsedAbilityTable {
  sheetName: string
  headerRow: number
  projectCol: number
  methodCol: number
  items: AbilityPair[]
  /** 因「项目」或「标准」为空被跳过的行数（空行、小计行、说明行等） */
  skipped: number
  /** 表头定位依据（回显给用户，便于排错） */
  how: string
}

/** 表头最多扫描前几行（样例表头在第 3 行，第 1 行是填写说明） */
const MAX_HEADER_SCAN_ROWS = 30

/** 表头列名别名（去空白后精确比较） */
const METHOD_HEADERS = new Set(['标准编号', '检测标准', '标准号', '方法编号', '检测方法'])
const PROJECT_HEADERS = new Set(['检测项目', '项目名称', '检验项目', '检测项目名称', '参数名称', '检测参数', '项目', '参数'])

function norm(s: string): string {
  return s.replace(/\s+/g, '').trim()
}

/** 取单元格文本：兼容富文本 / 公式 / 超链接 / 数字 */
function cellText(cell: ExcelJS.Cell): string {
  const v = cell.value
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') {
    const anyV = v as unknown as Record<string, unknown>
    const rich = anyV.richText as Array<{ text?: string }> | undefined
    if (Array.isArray(rich)) return rich.map((t) => t.text ?? '').join('')
    if (anyV.text !== undefined && anyV.text !== null) return String(anyV.text)
    if (anyV.result !== undefined && anyV.result !== null) return String(anyV.result)
    return ''
  }
  return String(v)
}

/** 合并单元格取"主格"文本（双层表头里分组标题只写在主格上） */
export function masterText(cell: ExcelJS.Cell): string {
  const anyCell = cell as unknown as { isMerged?: boolean; master?: ExcelJS.Cell }
  if (anyCell.isMerged && anyCell.master) return norm(cellText(anyCell.master))
  return norm(cellText(cell))
}

function isMethodHeader(text: string): boolean {
  if (!text) return false
  if (text.includes('编号') && text.includes('年号')) return true
  return METHOD_HEADERS.has(text)
}

function isProjectHeader(text: string): boolean {
  return !!text && PROJECT_HEADERS.has(text)
}

export interface AbilityHeader {
  headerRow: number
  projectCol: number
  methodCol: number
  how: string
}

/** 定位表头行与「项目列 / 方法列」；找不到返回 null */
export function locateAbilityHeader(ws: ExcelJS.Worksheet): AbilityHeader | null {
  const maxRow = Math.min(ws.rowCount || 0, MAX_HEADER_SCAN_ROWS)
  const maxCol = Math.max(ws.columnCount || 0, 1)

  for (let r = 1; r <= maxRow; r++) {
    const row = ws.getRow(r)
    const texts: string[] = []
    for (let c = 1; c <= maxCol; c++) texts[c] = norm(cellText(row.getCell(c)))

    let methodCol = -1
    for (let c = 1; c <= maxCol; c++) {
      if (isMethodHeader(texts[c])) {
        methodCol = c
        break
      }
    }
    if (methodCol < 0) continue

    // 1) 单层表头：同行左侧最靠近方法列的显式项目列
    let projectCol = -1
    let how = ''
    for (let c = methodCol - 1; c >= 1; c--) {
      if (isProjectHeader(texts[c])) {
        projectCol = c
        how = `表头第 ${r} 行「${texts[c]}」（第 ${c} 列）`
        break
      }
    }

    // 2) 双层表头：左侧倒数第二个「名称」才是「产品/项目/参数」的名称
    if (projectCol < 0) {
      const nameCols: number[] = []
      for (let c = 1; c < methodCol; c++) if (texts[c] === '名称') nameCols.push(c)
      if (nameCols.length >= 2) {
        projectCol = nameCols[nameCols.length - 2]
        how = `双层表头第 ${r} 行：左侧倒数第二个「名称」（第 ${projectCol} 列）`
      } else if (nameCols.length === 1) {
        projectCol = nameCols[0]
        how = `表头第 ${r} 行「名称」（第 ${projectCol} 列）`
      }
    }

    if (projectCol < 0) continue
    const group = r > 1 ? masterText(ws.getRow(r - 1).getCell(projectCol)) : ''
    if (group) how += `，分组「${group}」`
    return { headerRow: r, projectCol, methodCol, how }
  }
  return null
}

/** 解析工作簿：返回条目明细；结构不符时抛可读错误 */
export async function parseAbilityWorkbook(buf: ArrayBuffer): Promise<ParsedAbilityTable> {
  const wb = new ExcelJS.Workbook()
  try {
    await wb.xlsx.load(buf)
  } catch (e) {
    throw new Error(`Excel 解析失败（仅支持 .xlsx，老版 .xls 请另存为 .xlsx）：${(e as Error).message}`)
  }

  const sheets = wb.worksheets || []
  if (!sheets.length) throw new Error('Excel 中未找到工作表')

  let target: { ws: ExcelJS.Worksheet; header: AbilityHeader } | null = null
  for (const ws of sheets) {
    const header = locateAbilityHeader(ws)
    if (header) {
      target = { ws, header }
      break
    }
  }
  if (!target) {
    throw new Error(
      '未找到「编号（含年号）」表头列：请确认上传的是《检验检测能力表》（需含「产品/项目/参数 - 名称」与「依据的标准（方法） - 编号（含年号）」两类列）',
    )
  }

  const { ws, header } = target
  const items: AbilityPair[] = []
  let skipped = 0
  for (let r = header.headerRow + 1; r <= ws.rowCount; r++) {
    const row = ws.getRow(r)
    const project = cellText(row.getCell(header.projectCol)).trim()
    const method = cellText(row.getCell(header.methodCol)).trim()
    if (!project || !method) {
      skipped++
      continue
    }
    items.push({ project, method })
  }
  if (!items.length) {
    throw new Error(`表头已识别（${header.how}），但「编号（含年号）」列没有数据行，请确认文件内容`)
  }

  return {
    sheetName: ws.name,
    headerRow: header.headerRow,
    projectCol: header.projectCol,
    methodCol: header.methodCol,
    items,
    skipped,
    how: header.how,
  }
}
