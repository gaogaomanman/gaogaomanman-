/**
 * 江苏省追溯平台导出（原 `queryJiangsuTaskNo` 行 1425~1576、`queryJiangsuRoutineTaskNo` 行 1581~1732）。
 *
 * ⚠️ 两个块的实现经逐行比对**完全一致**，仅 3 处差异（已在下方 DIFF 标注，并保留为变参）：
 *    1. 监测类别：监督抽查 / 例行监测
 *    2. 抽样单位：苏州市农业农村局 / 苏州市农产品质量安全监测中心
 *    3. 文件名后缀：监督抽查 / 例行监测
 *   （检测单位两者均为「苏州市农产品质量安全监测中心」，不是差异点。）
 *
 * 另注：本块内的 `parseCityCounty` 是原文件第 1476 行与第 1632 行的那份副本，
 * 与其它 3 份同名函数（行 688 / 1773 / 2374 / 2612）**未做合并**——按迁移方案要求逐一核对，避免误合并导致口径漂移。
 *
 * 新增「按合同号导出」：传入 `contractNo` 时，筛选条件由 `d.TASK_NO = 'x'` 改为
 * `buildContractCond()`（`d.CONTRACTS_NO LIKE`，支持逗号/分号分隔多值），其余取数、
 * 列定义、合并规则、判定口径与文件名规则完全不变。
 * 新增「多任务编号输入」：任务编号走 `resolveTaskCond`（多个/免输 RW 前缀，IN 匹配）。
 */
import {
  buildContractCond,
  createStyledExcel,
  dmQuery,
  PESTICIDE_NAME_MAP,
  reorderPesticides,
  type CellValue,
} from '../helpers'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'
import { resolveTaskCond, taskFileStamp, taskListLabel } from './taskResolve'

export type JiangsuVariant = '监督抽查' | '例行监测'

/** 原块内 parseCityCounty（1476/1632 行副本），未改动 */
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

const DIFF: Record<JiangsuVariant, { category: string; samplingOrg: string }> = {
  监督抽查: { category: '监督抽查', samplingOrg: '苏州市农业农村局' },
  例行监测: { category: '例行监测', samplingOrg: '苏州市农产品质量安全监测中心' },
}

export interface JiangsuParams {
  variant: JiangsuVariant
  exactTaskNo?: string
  inputValue: string
  /** 合同号（多个用逗号/分号分隔，模糊匹配）；填写后按合同号导出，忽略任务编号 */
  contractNo?: string
  setProgress: (html: string) => void
}

export async function queryJiangsuTaskNo(p: JiangsuParams): Promise<DmOutcome> {
  const contractNo = (p.contractNo || '').trim()
  let whereCond = ''
  let stamp = ''
  let desc = ''
  if (contractNo) {
    whereCond = buildContractCond(contractNo)
    stamp = `合同${contractNo}`
    desc = `合同 ${contractNo}`
    p.setProgress(`⏳ 正在查询合同 ${contractNo} 的数据...`)
  } else {
    const r = await resolveTaskCond(p.exactTaskNo, p.inputValue, p.setProgress)
    if (r.outcome) return r.outcome
    const tasks = r.tasks
    whereCond = r.cond
    stamp = taskFileStamp(tasks)
    desc = `任务编号 ${taskListLabel(tasks)}`
    p.setProgress(`⏳ 正在查询任务 ${taskListLabel(tasks)} 的数据...`)
  }
  const diff = DIFF[p.variant]

  try {
    const detData = await dmQuery(
      'SELECT s.ID, s.SMALL_NO, s.NAME, s.SAMPLING_POSITION, ' +
        'd.NO, d.BUSINESS_CATEGORY_NAME, d.DETECTED_COMPANY_NAME, d.DETECTED_COMPANY_ADDRESS, ' +
        'd.SAMPLING_DATE, d.ACCEPT_ORG_NAME, d.EVALUATE_RESULT, s.ORIGINAL_NO ' +
        'FROM DETECTION.DT_DETECTION d ' +
        'LEFT JOIN DETECTION.DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 ' +
        'WHERE d.IS_DELETED = 0' + whereCond + ' ' +
        'ORDER BY d.NO, s.SMALL_NO',
    )

    if (!detData.success) {
      return danger(`❌ 查询失败：${detData.error || '未知错误'}`)
    }
    if (!detData.rows || detData.rows.length === 0) {
      return warn(`⚠️ 未找到${desc}的数据`)
    }

    const samples = detData.rows

    const projData = await dmQuery(
      'SELECT s.ID, sp.DECIDE_PROJECT_NAME, sp.METERING_UNIT_NAME, ' +
        'r.REPORT_VAL, sp.SINGLE_JUDGE, sp.DETECTION_LIMIT_TYPE, sp.DETECTION_LIMIT_VALUE ' +
        'FROM DETECTION.DT_DETECTION d ' +
        'LEFT JOIN DETECTION.DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 ' +
        "LEFT JOIN DETECTION.DT_SAMPLE_PROJECT sp ON sp.SAMPLE_ID = s.ID AND sp.IS_DELETED = 0 AND NVL(sp.IS_LOGOUT, 'NO') <> 'YES' " +
        'LEFT JOIN DETECTION.DT_RESULT_CHECK_IN r ON r.SAMPLE_PROJECT_ID = sp.ID AND r.IS_DELETED = 0 ' +
        'WHERE d.IS_DELETED = 0' + whereCond + ' ' +
        'ORDER BY s.ID, sp.SEQ_NO',
    )

    interface JsSampleInfo {
      sampleNo: string
      sampleName: string
      samplingPosition: string
      detectedCompany: string
      address: string
      samplingDate: unknown
      evaluateResult: string
    }

    const sampleInfo: Record<string, JsSampleInfo> = {}
    for (const s of samples) {
      sampleInfo[String(s[0])] = {
        sampleNo: s[4] ? String(s[4]) + String(s[1] || '') : String(s[1] || ''),
        sampleName: String(s[2] || ''),
        samplingPosition: String(s[3] || ''),
        detectedCompany: String(s[6] || ''),
        address: String(s[7] || ''),
        samplingDate: s[8] || '',
        evaluateResult: String(s[10] || ''),
      }
    }

    const pesticideSet = new Set<string>()
    const pesticideUnit: Record<string, string> = {}
    const resultsBySample: Record<string, Record<string, { vals: unknown[]; judge: string }>> = {}
    if (projData.success && projData.rows) {
      for (const r of projData.rows) {
        const sid = String(r[0])
        const pn = r[1] as string
        if (!pn) continue
        const dn = PESTICIDE_NAME_MAP[pn] || pn
        pesticideSet.add(dn)
        pesticideUnit[dn] = pesticideUnit[dn] || String(r[2] || '')
        if (!resultsBySample[sid]) resultsBySample[sid] = {}
        if (!resultsBySample[sid][dn]) resultsBySample[sid][dn] = { vals: [], judge: '' }
        const cell = resultsBySample[sid][dn]
        cell.vals.push(r[3] || '')
        if (r[4]) cell.judge = String(r[4])
      }
    }

    const { reordered: pesticides, colorMap } = reorderPesticides(Array.from(pesticideSet).sort(), pesticideUnit)
    const headers = [
      '序号', '监测类别', '抽样单位', '检测单位', '被检市县',
      '样品编号', '抽样日期', '样品名称', '受检单位名称', '受检单位注册号',
      '监测环节', '受检单位所在地',
    ]
    for (const pi of pesticides) headers.push(pi)
    headers.push('结果判定(合格或不合格)')

    const rows: Record<string, CellValue>[] = []
    const redCells = new Set<string>()
    const colIdxMap: Record<string, number> = {}
    headers.forEach((h, i) => {
      colIdxMap[h] = i
    })
    let seq = 1
    for (const [sid, info] of Object.entries(sampleInfo)) {
      const row: Record<string, CellValue> = {}
      headers.forEach((h) => {
        row[h] = ''
      })
      row['序号'] = seq++
      row['监测类别'] = diff.category
      row['抽样单位'] = diff.samplingOrg
      row['检测单位'] = '苏州市农产品质量安全监测中心'
      row['被检市县'] = parseCityCounty(info.address)
      row['样品编号'] = info.sampleNo
      row['抽样日期'] = info.samplingDate ? String(info.samplingDate).split('T')[0] : ''
      row['样品名称'] = info.sampleName
      row['受检单位名称'] = info.detectedCompany
      row['受检单位注册号'] = ''
      row['监测环节'] = info.samplingPosition
      row['受检单位所在地'] = info.address

      const rowIdx = rows.length
      const results = resultsBySample[sid] || {}
      let hasFail = false
      for (const pi of pesticides) {
        const res = results[pi]
        if (res) {
          const vals = res.vals || []
          if (vals.length > 1) {
            row[pi] = vals.map((v) => String(v)).join('\n')
            redCells.add(rowIdx + '_' + colIdxMap[pi])
          } else {
            row[pi] = String(vals[0] || '')
          }
          if (res.judge && (res.judge.includes('不合格') || res.judge === '不符合')) hasFail = true
        }
      }
      row['结果判定(合格或不合格)'] = hasFail ? '不合格' : '合格'
      rows.push(row)
    }

    const excelRows = rows.map((row) => headers.map((h) => row[h] || ''))
    const fileName = `江苏省追溯平台_${p.variant}_${stamp}.xlsx`
    try {
      await createStyledExcel(headers, excelRows, '江苏省追溯平台', fileName, colorMap, redCells)
    } catch (e) {
      return danger(`❌ 导出失败：${esc((e as Error).message)}`)
    }

    return ok(`✅ 导出完成！共 ${rows.length} 个样品，${pesticides.length} 种检测项目。<br>📁 文件：${fileName}`)
  } catch (e) {
    return danger(`❌ 失败：${esc((e as Error).message)}`)
  }
}
