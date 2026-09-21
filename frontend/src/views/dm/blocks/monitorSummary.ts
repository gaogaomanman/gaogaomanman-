/**
 * 附件2 监测信息汇总表（原 `queryMonitorSummaryTable`，行 1737~1829）。
 *
 * 平坦表格式（无检测项目列）。逐字保留：单段 SQL、17 列表头、
 * 「例行监测→风险监测」类别映射、抽样单位按类别取值、产品类别路径 `产品-x-y` → `x>>y` 的转换、
 * 监听类别为空时抽样单位取空字符串的行为。
 *
 * 本块内的 `parseCityCounty` 是原文件第 1773 行副本，与 1476 / 1632 行两份逐字一致；
 * 按迁移方案要求**暂不跨块合并**（另有 688 / 2374 / 2612 三份待逐份核对）。
 */
import { createStyledExcel, dmQuery, type CellValue } from '../helpers'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'
import { resolveTaskNo } from './taskResolve'

function parseCityCounty(addr: string): string {
  if (!addr) return ''
  let s = addr
  const m = s.match(/(省|自治区)/)
  if (m) s = s.substring(m.index + m[0].length)
  const m2 = s.match(/(.+?[市])([^路街道\d]+?(?:区|县|市|园区|新区|开发区))/) || s.match(/(.+?[市])(.+?[区县])/)
  if (m2) return m2[1] + m2[2]
  const m3 = s.match(/(.+?[市])/)
  if (m3) return m3[1]
  const m4 = s.match(/^([^路街道\d]+?(?:园区|新区|开发区))/)
  if (m4) return m4[1]
  return s
}

export interface MonitorSummaryParams {
  exactTaskNo?: string
  inputValue: string
  setProgress: (html: string) => void
}

export async function queryMonitorSummaryTable(p: MonitorSummaryParams): Promise<DmOutcome> {
  const resolved = await resolveTaskNo(p.exactTaskNo, p.inputValue, p.setProgress)
  if (resolved.outcome) return resolved.outcome
  const taskNo = resolved.taskNo as string

  p.setProgress(`⏳ 正在查询任务 ${taskNo} 的数据...`)

  try {
    const detData = await dmQuery(
      'SELECT s.ID, s.SMALL_NO, s.NAME, s.ORIGINAL_NO, s.SAMPLING_POSITION, s.SAMPLE_CATEGORY_NAME, c.FULL_VALUE_PATH, ' +
        'd.NO, d.BUSINESS_CATEGORY_NAME, d.DETECTED_COMPANY_NAME, d.DETECTED_COMPANY_ADDRESS, ' +
        'd.SAMPLING_DATE, d.EVALUATE_RESULT, d.TASK_NO ' +
        'FROM DETECTION.DT_DETECTION d ' +
        'LEFT JOIN DETECTION.DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 ' +
        'LEFT JOIN DETECTION.DT_SAMPLE_CATEGORY c ON c.ID = s.SAMPLE_CATEGORY_ID AND c.IS_DELETED = 0 ' +
        "WHERE d.TASK_NO = '" + taskNo.replace(/'/g, "''") + "' AND d.IS_DELETED = 0 " +
        'ORDER BY d.NO, s.SMALL_NO',
    )

    if (!detData.success) {
      return danger(`❌ 查询失败：${detData.error || '未知错误'}`)
    }
    if (!detData.rows || detData.rows.length === 0) {
      return warn(`⚠️ 未找到任务编号 ${taskNo} 的数据`)
    }

    const samples = detData.rows

    const headers = [
      '序号', '监测类别', '抽样单位', '检测单位', '县级市（区）',
      '样品编号', '抽样日期', '样品名称', '受检单位名称', '抽样环节',
      '受检单位所在地', '溯源信息', '结果判定', '备注', '样品原编号', '产品类别', '任务单号',
    ]

    const rows: Record<string, CellValue>[] = []
    let seq = 1
    for (const s of samples) {
      const row: Record<string, CellValue> = {}
      headers.forEach((h) => {
        row[h] = ''
      })
      const rawCategory = String(s[8] || '')
      const category = rawCategory === '例行监测' ? '风险监测' : rawCategory
      const samplingUnit =
        category === '监督抽查' ? '苏州市农业农村局' : category === '风险监测' ? '苏州市农产品质量安全监测中心' : ''

      row['序号'] = seq++
      row['监测类别'] = category
      row['抽样单位'] = samplingUnit
      row['检测单位'] = '苏州市农产品质量安全监测中心'
      row['县级市（区）'] = parseCityCounty(String(s[10] || ''))
      row['样品编号'] = s[7] ? String(s[7]) + String(s[1] || '') : String(s[1] || '')
      row['抽样日期'] = s[11] ? String(s[11]).split('T')[0] : ''
      row['样品名称'] = String(s[2] || '')
      row['受检单位名称'] = String(s[9] || '')
      row['抽样环节'] = String(s[4] || '')
      row['受检单位所在地'] = String(s[10] || '')
      row['溯源信息'] = ''
      row['结果判定'] = String(s[12] || '')
      row['备注'] = ''
      row['样品原编号'] = String(s[3] || '')
      const fullPath = String(s[6] || '')
      row['产品类别'] = fullPath ? fullPath.replace(/^产品-/, '').replace(/-/g, '>>') : String(s[5] || '')
      row['任务单号'] = taskNo
      rows.push(row)
    }

    const excelRows = rows.map((row) => headers.map((h) => row[h] || ''))
    const fileName = `监测信息汇总表_${taskNo}.xlsx`
    try {
      await createStyledExcel(headers, excelRows, '监测信息汇总表', fileName, {})
    } catch (e) {
      return danger(`❌ 导出失败：${esc((e as Error).message)}`)
    }

    return ok(`✅ 导出完成！共 ${rows.length} 个样品。<br>📁 文件：${fileName}`)
  } catch (e) {
    return danger(`❌ 失败：${esc((e as Error).message)}`)
  }
}
