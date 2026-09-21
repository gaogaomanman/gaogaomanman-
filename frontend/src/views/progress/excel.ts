/**
 * 抽采样进度统计：Excel 模板 / 导入解析 / 导出（纯函数，无 DOM 依赖，便于自动化回读断言）。
 *
 * 下载动作必须在浏览器里做（`document` / `URL.createObjectURL`），所以这里只负责
 * 「工作簿长什么样」与「单元格怎么读」，最后由组件调用 `downloadBuffer`。
 *
 * 模板列固定为：**合同编号 | 区域 | 产品类型 | 任务量**
 * （合同编号可留空——留空即导入到页面上当前选中的合同，见后端 `/quotas/import`）。
 */
import ExcelJS from 'exceljs'

const THIN = { style: 'thin' as const }
const BORDER = { top: THIN, left: THIN, bottom: THIN, right: THIN }
const HEAD_FILL: ExcelJS.Fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF1E3A8A' } }
const HEAD_FONT: Partial<ExcelJS.Font> = { bold: true, color: { argb: 'FFFFFFFF' }, size: 11 }

export interface QuotaImportRow {
  contract_no: string
  region: string
  product_type: string
  quota: string | number | null
}

/** 单元格取值：兼容富文本 / 公式 / 超链接等 exceljs 包装值。 */
export function cellText(value: ExcelJS.CellValue): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') {
    const v = value as {
      richText?: { text: string }[]
      text?: string
      result?: unknown
      hyperlink?: string
    }
    if (Array.isArray(v.richText)) return v.richText.map((r) => r.text).join('')
    if (v.text !== undefined) return String(v.text)
    if (v.result !== undefined) return String(v.result)
    return ''
  }
  return String(value).trim()
}

/** 表头别名（用户手改过表头也能认出来；顺序即优先级） */
const HEAD_ALIAS: Record<string, string[]> = {
  contract_no: ['合同编号', '合同号', '合同', 'contract'],
  region: ['区域', '区县', '地区', 'region'],
  product_type: ['产品类型', '产品名称', '品种', '样品名称', 'product'],
  quota: ['任务量', '任务数', '计划量', '数量', 'quota'],
}

function findHeaderRow(ws: ExcelJS.Worksheet): { row: number; map: Record<string, number> } | null {
  const maxScan = Math.min(ws.rowCount, 10)
  for (let r = 1; r <= maxScan; r += 1) {
    const map: Record<string, number> = {}
    ws.getRow(r).eachCell((cell, col) => {
      const text = cellText(cell.value).toLowerCase()
      if (!text) return
      Object.entries(HEAD_ALIAS).forEach(([key, aliases]) => {
        if (map[key] === undefined && aliases.some((a) => text.includes(a.toLowerCase()))) map[key] = col
      })
    })
    if (map.region && map.product_type) return { row: r, map }
  }
  return null
}

/** 解析任务量导入文件；找不到表头时**抛可读错误**（而不是静默产出 0 行）。 */
export async function parseQuotaWorkbook(file: File): Promise<QuotaImportRow[]> {
  const wb = new ExcelJS.Workbook()
  await wb.xlsx.load(await file.arrayBuffer())
  const ws = wb.worksheets[0]
  if (!ws) throw new Error('文件中没有工作表')

  const head = findHeaderRow(ws)
  if (!head) {
    throw new Error('未找到表头：请使用模板（至少包含「区域」「产品类型」两列，可选「合同编号」「任务量」）')
  }

  const rows: QuotaImportRow[] = []
  ws.eachRow((row, rowNumber) => {
    if (rowNumber <= head.row) return
    const get = (key: string): ExcelJS.CellValue => (head.map[key] ? row.getCell(head.map[key]).value : null)
    const region = cellText(get('region'))
    const pt = cellText(get('product_type'))
    if (!region && !pt) return // 空行跳过
    rows.push({
      contract_no: head.map.contract_no ? cellText(get('contract_no')) : '',
      region,
      product_type: pt,
      quota: head.map.quota ? cellText(get('quota')) : '',
    })
  })
  if (!rows.length) throw new Error('文件中没有可导入的数据行（只有表头）')
  return rows
}

/** 生成导入模板：表头 + 当前区域 × 产品类型的空行（用户直接在表里填数字）。 */
export async function buildQuotaTemplate(
  regions: string[],
  productTypes: string[],
  contractNo: string,
  year: number,
): Promise<ArrayBuffer> {
  const wb = new ExcelJS.Workbook()
  wb.creator = 'AI工具合集 · 抽采样进度统计'
  const ws = wb.addWorksheet(`${year}年任务量`)

  const head = ['合同编号', '区域', '产品类型', '任务量']
  ws.addRow([`${year} 年抽采样任务量导入模板（合同编号可留空：留空即导入到当前所选合同）`])
  ws.mergeCells(1, 1, 1, head.length)
  ws.getCell('A1').font = { bold: true, size: 12 }
  ws.addRow(head)
  ws.getRow(2).eachCell((c) => {
    c.fill = HEAD_FILL
    c.font = HEAD_FONT
    c.border = BORDER
    c.alignment = { horizontal: 'center', vertical: 'middle' }
  })

  const r0 = regions.length ? regions : ['']
  const p0 = productTypes.length ? productTypes : ['']
  r0.forEach((region) => {
    p0.forEach((pt) => {
      const row = ws.addRow([contractNo, region, pt, null])
      row.eachCell((c) => {
        c.border = BORDER
      })
    })
  })

  ws.getColumn(1).width = 26
  ws.getColumn(2).width = 16
  ws.getColumn(3).width = 16
  ws.getColumn(4).width = 12
  ws.views = [{ state: 'frozen', ySplit: 2 }]

  return (await wb.xlsx.writeBuffer()) as ArrayBuffer
}

/** 导出统计矩阵：区域 × 产品类型，单元格为「完成量/任务量（完成率）」。 */
export async function buildMatrixWorkbook(
  year: number,
  title: string,
  columns: { id: number; name: string }[],
  rows: { region: string; cells: { product_type_id: number; done: number; quota: number; rate: number | null }[]; done: number; quota: number }[],
): Promise<ArrayBuffer> {
  const wb = new ExcelJS.Workbook()
  const ws = wb.addWorksheet('进度统计')
  const head = ['区域', ...columns.map((c) => c.name), '合计完成量', '合计任务量', '完成率']
  ws.addRow([`${year} 年抽采样进度统计 · ${title}`])
  ws.mergeCells(1, 1, 1, head.length)
  ws.getCell('A1').font = { bold: true, size: 12 }
  ws.addRow(head)
  ws.getRow(2).eachCell((c) => {
    c.fill = HEAD_FILL
    c.font = HEAD_FONT
    c.border = BORDER
    c.alignment = { horizontal: 'center', vertical: 'middle', wrapText: true }
  })

  rows.forEach((row) => {
    const line: (string | number)[] = [row.region]
    columns.forEach((col) => {
      const cell = row.cells.find((c) => c.product_type_id === col.id)
      if (!cell || (cell.done === 0 && cell.quota === 0)) {
        line.push('—')
      } else if (cell.quota <= 0) {
        line.push(`${cell.done}/—`)
      } else {
        line.push(`${cell.done}/${cell.quota}（${(cell.rate ?? 0).toFixed(0)}%）`)
      }
    })
    const rate = row.quota > 0 ? `${((row.done / row.quota) * 100).toFixed(1)}%` : '—'
    line.push(row.done, row.quota, rate)
    const r = ws.addRow(line)
    r.eachCell((c) => {
      c.border = BORDER
      c.alignment = { horizontal: 'center', vertical: 'middle' }
    })
  })

  ws.getColumn(1).width = 16
  for (let i = 0; i < columns.length; i += 1) ws.getColumn(i + 2).width = 16
  ws.getColumn(columns.length + 2).width = 12
  ws.getColumn(columns.length + 3).width = 12
  ws.getColumn(columns.length + 4).width = 12
  ws.views = [{ state: 'frozen', xSplit: 1, ySplit: 2 }]

  return (await wb.xlsx.writeBuffer()) as ArrayBuffer
}

/** 触发浏览器下载。 */
export function downloadBuffer(buf: ArrayBuffer, fileName: string): void {
  const url = URL.createObjectURL(
    new Blob([buf], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }),
  )
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  a.click()
  URL.revokeObjectURL(url)
}
