/**
 * 通用模板：分类通用数据汇总单列（原 `queryByTaskNo`，行 1273~1420）。
 *
 * 逐字保留：两段 SQL、样品字段取值顺序、检测项目横向展开、`PESTICIDE_NAME_MAP` 映射、
 * `reorderPesticides` 分组、多值单元格换行并标红、综合判定兜底、表头配色与文件名。
 *
 * 判定口径修正（2026-09-23）：「综合判定」列原先**优先照抄**单据级散文（`d.EVALUATE_RESULT`），
 * 只有它为空时才退回单项目判定，且**从不看**样本级结论。现与省例行四块统一 ——
 * `isFailText(样本级) || isFailText(单据级)` 优先（谓词见 `provinceCommon.isFailText`），
 * 单项目判定保留作交叉验证；该列**只输出 `合格` / `不合格`**，不再出现散文。
 * 实测（本块取数按任务号，取该合同下的两个任务号）：
 *   - `RW2026042`（12 个样品 / 9 个不合格）：旧实现这 9 行**全是散文**（显示「不合格」的 0 行）；
 *   - `RW2026034`（92 个样品 / 2 个不合格）：旧实现 1 行散文（`JSLX032026010106`），
 *     另 1 行 `JSLX032026010107` 虽单据级为空，但其单项目判定已填「不合格」→ 退回分支侥幸判对。
 *   改后两个任务分别 9 / 2 行「不合格」，与 LIMS 基准**完全一致（漏 0 / 多 0）**。
 */
import { createStyledExcel, dmQuery, PESTICIDE_NAME_MAP, reorderPesticides, type CellValue } from '../helpers'
import { isFailText } from './provinceCommon'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'
import { resolveTaskNo } from './taskResolve'

export interface TaskNoParams {
  exactTaskNo?: string
  inputValue: string
  setProgress: (html: string) => void
}

export async function queryByTaskNo(p: TaskNoParams): Promise<DmOutcome> {
  const resolved = await resolveTaskNo(p.exactTaskNo, p.inputValue, p.setProgress)
  if (resolved.outcome) return resolved.outcome
  const taskNo = resolved.taskNo as string

  p.setProgress(`⏳ 正在查询任务 ${taskNo} 的数据...`)

  try {
    const detData = await dmQuery(
      'SELECT s.ID, s.SMALL_NO, s.NAME, s.ORIGINAL_NO, s.SAMPLING_POSITION, s.SAMPLE_NUM, s.SAMPLE_NUM_UNIT_NAME, ' +
        'd.NO, d.BUSINESS_CATEGORY_NAME, d.DETECTED_COMPANY_NAME, d.DETECTED_COMPANY_ADDRESS, ' +
        'd.PRODUCTION_COMPANY_NAME, d.SAMPLING_COMPANY, d.SAMPLING_USER_NAME, d.SAMPLING_DATE, ' +
        'd.ACCEPT_ORG_NAME, d.EVALUATE_RESULT, s.EVALUATE_RESULT ' +
        'FROM DETECTION.DT_DETECTION d ' +
        'LEFT JOIN DETECTION.DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 ' +
        "WHERE d.TASK_NO = '" + taskNo.replace(/'/g, "''") + "' AND d.IS_DELETED = 0 " +
        'ORDER BY d.NO, s.SMALL_NO',
    )

    if (!detData.success) {
      return danger(`❌ 查询失败：${detData.error || '未知错误'}<br>请确认数据库已连接后再试`)
    }
    if (!detData.rows || detData.rows.length === 0) {
      return warn(`⚠️ 未找到任务编号 ${taskNo} 的数据，请检查编号是否正确`)
    }

    const samples = detData.rows

    const projData = await dmQuery(
      'SELECT s.ID, sp.DECIDE_PROJECT_NAME, sp.METERING_UNIT_NAME, ' +
        'r.REPORT_VAL, sp.SINGLE_JUDGE, sp.DETECTION_LIMIT_TYPE, sp.DETECTION_LIMIT_VALUE ' +
        'FROM DETECTION.DT_DETECTION d ' +
        'LEFT JOIN DETECTION.DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 ' +
        "LEFT JOIN DETECTION.DT_SAMPLE_PROJECT sp ON sp.SAMPLE_ID = s.ID AND sp.IS_DELETED = 0 AND NVL(sp.IS_LOGOUT, 'NO') <> 'YES' " +
        'LEFT JOIN DETECTION.DT_RESULT_CHECK_IN r ON r.SAMPLE_PROJECT_ID = sp.ID AND r.IS_DELETED = 0 ' +
        "WHERE d.TASK_NO = '" + taskNo.replace(/'/g, "''") + "' AND d.IS_DELETED = 0 " +
        'ORDER BY s.ID, sp.SEQ_NO',
    )

    interface SampleInfo {
      sampleNo: string
      sampleName: string
      originalNo: string
      samplingPosition: string
      sampleNum: unknown
      sampleNumUnit: unknown
      category: string
      detectedCompany: string
      address: string
      productionCompany: string
      samplingCompany: string
      samplingUser: string
      samplingDate: unknown
      acceptOrg: string
      /** 单据级结论 d.EVALUATE_RESULT（散文，仅作兜底） */
      evaluateResult: string
      /** 样本级结论 s.EVALUATE_RESULT（规范枚举：符合 / 不合格 / 不符合 / 空），首选判定依据 */
      sampleEvaluate: string
    }

    const sampleInfo: Record<string, SampleInfo> = {}
    for (const s of samples) {
      sampleInfo[String(s[0])] = {
        sampleNo: s[7] ? String(s[7]) + String(s[1] || '') : String(s[1] || ''),
        sampleName: String(s[2] || ''),
        originalNo: String(s[3] || ''),
        samplingPosition: String(s[4] || ''),
        sampleNum: s[5] || '',
        sampleNumUnit: s[6] || '',
        category: String(s[8] || ''),
        detectedCompany: String(s[9] || ''),
        address: String(s[10] || ''),
        productionCompany: String(s[11] || ''),
        samplingCompany: String(s[12] || ''),
        samplingUser: String(s[13] || ''),
        samplingDate: s[14] || '',
        acceptOrg: String(s[15] || ''),
        evaluateResult: String(s[16] || ''), // d.EVALUATE_RESULT（散文，仅作兜底）
        // s.EVALUATE_RESULT —— 追加在 SQL 末尾，不移动既有列的下标
        sampleEvaluate: String(s[17] || ''),
      }
    }

    const pesticideSet = new Set<string>()
    const pesticideUnit: Record<string, string> = {}
    const resultsBySample: Record<string, Record<string, { vals: unknown[]; judge: string }>> = {}
    if (projData.success && projData.rows) {
      for (const r of projData.rows) {
        const sampleId = String(r[0])
        const projectName = r[1] as string
        if (!projectName) continue
        const displayName = PESTICIDE_NAME_MAP[projectName] || projectName
        pesticideSet.add(displayName)
        pesticideUnit[displayName] = pesticideUnit[displayName] || String(r[2] || '')
        if (!resultsBySample[sampleId]) resultsBySample[sampleId] = {}
        if (!resultsBySample[sampleId][displayName]) {
          resultsBySample[sampleId][displayName] = { vals: [], judge: '' }
        }
        const cell = resultsBySample[sampleId][displayName]
        cell.vals.push(r[3] || '')
        if (r[4]) cell.judge = String(r[4])
      }
    }

    const { reordered: pesticides, colorMap } = reorderPesticides(Array.from(pesticideSet).sort(), pesticideUnit)
    const headers = [
      '样品编号', '产品名称', '原始编号', '抽样地点', '受检单位',
      '受检单位地址', '生产单位', '抽样人', '抽样数量', '抽样时间', '综合判定', '检测类别',
    ]
    for (const pi of pesticides) headers.push(pi + (pesticideUnit[pi] || ''))

    const rows: Record<string, CellValue>[] = []
    const redCells = new Set<string>()
    const colIdxMap: Record<string, number> = {}
    headers.forEach((h, i) => {
      colIdxMap[h] = i
    })
    for (const [sampleId, info] of Object.entries(sampleInfo)) {
      const row: Record<string, CellValue> = {}
      headers.forEach((h) => {
        row[h] = ''
      })
      row['样品编号'] = info.sampleNo
      row['产品名称'] = info.sampleName
      row['原始编号'] = info.originalNo
      row['抽样地点'] = info.samplingPosition
      row['受检单位'] = info.detectedCompany
      row['受检单位地址'] = info.address
      row['生产单位'] = info.productionCompany
      row['抽样人'] = info.samplingUser
      row['抽样数量'] = String(info.sampleNum || '') + (info.sampleNumUnit ? ' ' + String(info.sampleNumUnit) : '')
      row['抽样时间'] = info.samplingDate ? String(info.samplingDate).split('T')[0] : ''
      row['检测类别'] = info.category

      const rowIdx = rows.length
      const results = resultsBySample[sampleId] || {}
      let hasFail = false
      for (const pi of pesticides) {
        const res = results[pi]
        if (res) {
          const colKey = pi + (pesticideUnit[pi] || '')
          const vals = res.vals || []
          if (vals.length > 1) {
            row[colKey] = vals.map((v) => String(v)).join('\n')
            redCells.add(rowIdx + '_' + colIdxMap[colKey])
          } else {
            row[colKey] = String(vals[0] || '')
          }
          if (res.judge && (res.judge.includes('不合格') || res.judge === '不符合')) {
            hasFail = true
          }
        }
      }
      // 判定依据（任一命中即「不合格」）：与省例行四块统一 ——
      //   1) 样本级结论 s.EVALUATE_RESULT（规范枚举，首选）；
      //   2) 单据级结论 d.EVALUATE_RESULT（散文，兜底）；
      //   3) 检测项目列的单项目判定（原有逻辑，保留作交叉验证）。
      // 该列**只输出 `合格` / `不合格`**（2026-09-23 变更，不再照抄单据级散文）。
      row['综合判定'] =
        isFailText(info.sampleEvaluate) || isFailText(info.evaluateResult) || hasFail ? '不合格' : '合格'
      rows.push(row)
    }

    const excelRows = rows.map((row) => headers.map((h) => row[h] || ''))
    const fileName = `通用数据汇总_${taskNo}.xlsx`
    // 原实现是 fire-and-forget，导出失败会被静默吞掉；迁移后改为可感知失败
    try {
      await createStyledExcel(headers, excelRows, '通用数据汇总', fileName, colorMap, redCells)
    } catch (e) {
      return danger(`❌ 导出失败：${esc((e as Error).message)}`)
    }

    return ok(
      `✅ 导出完成！共 ${rows.length} 个样品，${pesticides.length} 种检测项目。<br>📁 文件：${fileName}`,
    )
  } catch (e) {
    return danger(`❌ 失败：${esc((e as Error).message)}`)
  }
}
