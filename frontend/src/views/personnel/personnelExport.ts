/**
 * 卡1「人员能力表」的展示与导出逻辑（纯函数，无 DOM 依赖）。
 *
 * 抽出来的原因：下载动作必须在浏览器里做（`document` / `URL.createObjectURL`），
 * 但"工作簿长什么样"（标题、表头、列宽、红字）是可以被自动化验证的部分——
 * 抽成纯函数后，既让 `PersonnelView.vue` 只剩"点一下、下个文件"，
 * 也让导出内容能在测试里真实生成并回读断言（而不是只靠代码审查）。
 */
import ExcelJS from 'exceljs'

import type { PersonnelByPersonRow } from '../../api/client'

const THIN = { style: 'thin' as const }
const BORDER = { top: THIN, left: THIN, bottom: THIN, right: THIN }
const ALIGN = { horizontal: 'center' as const, vertical: 'middle' as const, wrapText: true }

/** 比对列文案：在 / 在（年号不同）/ 在（项目名别名）/ 在（别名与年号均不同）/ 不在 */
export function abilityCellText(row: PersonnelByPersonRow): string {
  if (!row.inAbility) return '—'
  return row.inAbilityNote ? `${row.inAbility}（${row.inAbilityNote}）` : row.inAbility
}

/** 比对列配色：不在=红、宽松命中=琥珀、在=绿 */
export function abilityCellCls(row: PersonnelByPersonRow): string {
  if (row.inAbility === '不在') return 'font-semibold text-red-600'
  if (row.inAbility === '在') return row.inAbilityNote ? 'text-amber-600' : 'text-green-600'
  return 'text-gray-400'
}

/** 是否需要比对列：已上传能力表且该行确实带回判定 */
export function hasAbilityColumn(rows: PersonnelByPersonRow[], abilityLoaded: boolean): boolean {
  return abilityLoaded && rows.some((row) => !!row.inAbility)
}

/**
 * 生成卡1 的《人员能力表》工作簿。
 *
 * 与原实现**逐字等价**：标题 `人员能力表：{姓名}`（合并 A1:C1 / 上传能力表后 A1:D1）、
 * 表头填充 `FFDBEAFE`、四边框、居中换行、`Times New Roman 11`、列宽 8/28/38(/20)、
 * 「不在」单元格红字加粗。文件名仍由调用方按 `人员能力表_{姓名}.xlsx` 下载。
 */
export async function buildPersonnelWorkbook(
  name: string,
  rows: PersonnelByPersonRow[],
  withAbility: boolean,
): Promise<ArrayBuffer> {
  const wb = new ExcelJS.Workbook()
  wb.creator = '人员能力表梳理'
  const ws = wb.addWorksheet('人员能力表')

  ws.mergeCells(withAbility ? 'A1:D1' : 'A1:C1')
  const titleCell = ws.getCell('A1')
  titleCell.value = `人员能力表：${name}`
  titleCell.font = { name: 'Times New Roman', size: 13, bold: true }
  titleCell.alignment = { horizontal: 'center', vertical: 'middle' }
  ws.getRow(1).height = 24

  const hRow = ws.addRow(
    withAbility ? ['序号', '检测项目', '检测标准', '是否在能力表'] : ['序号', '检测项目', '检测标准'],
  )
  hRow.eachCell((c) => {
    c.font = { name: 'Times New Roman', size: 11, bold: true }
    c.alignment = ALIGN
    c.border = BORDER
    c.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFDBEAFE' } }
  })

  rows.forEach((row, i) => {
    const cells: (string | number)[] = withAbility
      ? [i + 1, row.project, row.method, abilityCellText(row)]
      : [i + 1, row.project, row.method]
    const r = ws.addRow(cells)
    r.eachCell((c, colNumber) => {
      c.font = { name: 'Times New Roman', size: 11 }
      if (withAbility && colNumber === 4 && row.inAbility === '不在') {
        c.font = { name: 'Times New Roman', size: 11, bold: true, color: { argb: 'FFFF0000' } }
      }
      c.alignment = ALIGN
      c.border = BORDER
    })
  })

  ws.getColumn(1).width = 8
  ws.getColumn(2).width = 28
  ws.getColumn(3).width = 38
  if (withAbility) ws.getColumn(4).width = 20

  return (await wb.xlsx.writeBuffer()) as ArrayBuffer
}
