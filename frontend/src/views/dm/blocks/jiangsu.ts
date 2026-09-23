/**
 * 江苏省追溯平台导出（**单一模板**；原「监督抽查」「例行监测」两个变体已于 2026-09-23 合并）。
 *
 * 为什么合并：两个变体除「监测类别」「抽样单位」「文件名后缀」外**逐行完全一致**，
 * 而这两列本应来自数据、不该由模板决定——库里 `d.BUSINESS_CATEGORY_NAME`（业务类别）是
 * 干净枚举（例行监测 / 监督抽检 / 委托送样检验 / 委托抽样检验，6704 张单据无空值），
 * 旧实现却把它查出来丢掉、改用模板常量，于是"数据类别 ≠ 所选模板"时标签就贴错了
 * （实测合同 `PS2026005-阳澄湖水质专项` 混有 例行监测 41 张 + 委托送样检验 20 张，
 *  两个旧模板各硬编码一种类别，怎么导都有一半行是错的；"委托送样检验"更是两者都不覆盖）。
 *
 * 现在的口径（**类别取自数据**）：
 *   - 监测类别 = `d.BUSINESS_CATEGORY_NAME`，其中库里的「监督抽检」上报时写作「监督抽查」；
 *   - 抽样单位 = 监督抽检/监督抽查 → 苏州市农业农村局；其余类别一律 → 苏州市农产品质量安全监测中心；
 *   - 检测单位 = 苏州市农产品质量安全监测中心（不分类别）。
 *
 * 另注：本块内的 `parseCityCounty` 是原文件第 1476 行与第 1632 行的那份副本，
 * 与其它 3 份同名函数（行 688 / 1773 / 2374 / 2612）**未做合并**——按迁移方案要求逐一核对，避免误合并导致口径漂移。
 *
 * 按任务号导出：任务编号走 `resolveTaskCond`（多个/免输 RW 前缀，IN 匹配）；
 * 按合同号导出：传 `contractNo` 时筛选改为 `buildContractCond()`（`d.CONTRACTS_NO LIKE`，支持多值）。
 *
 * 产品类别筛选（2026-09-23 新增，参考「年度数据快速统计」）：`kinds` 走 `NEW_KIND_MAP`
 * 展开为 `s.SAMPLE_CATEGORY_NAME IN (...)`，两条查询同时生效。**全选等于不筛选**，
 * 见函数内 `kindCond` 的注释（库里存在 19 种未纳入归类表的样品类别，一律展开会丢数据）。
 */
import {
  buildContractCond,
  createStyledExcel,
  dmQuery,
  NEW_KIND_MAP,
  NEW_KIND_NAMES,
  PESTICIDE_NAME_MAP,
  reorderPesticides,
  type CellValue,
} from '../helpers'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'
import { isFailText } from './provinceCommon'
import { resolveTaskCond, taskFileStamp, taskListLabel } from './taskResolve'

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

/** 库里「业务类别」用词 → 上报用词：监督抽检 → 监督抽查（其余原样输出） */
const CATEGORY_LABEL: Record<string, string> = { 监督抽检: '监督抽查' }

/** 监督类业务类别（库里写"抽检"、上报写"抽查"，两者都认）——只有它们用农业农村局作为抽样单位 */
const SUPERVISE_CATEGORIES = new Set(['监督抽检', '监督抽查'])
const SUPERVISE_SAMPLING_ORG = '苏州市农业农村局'
const ROUTINE_SAMPLING_ORG = '苏州市农产品质量安全监测中心'

/** 上报用的「监测类别」文案 */
function categoryLabel(raw: string): string {
  return CATEGORY_LABEL[raw] || raw
}

/** 该业务类别对应的「抽样单位」 */
function samplingOrgOf(raw: string): string {
  return SUPERVISE_CATEGORIES.has(raw) ? SUPERVISE_SAMPLING_ORG : ROUTINE_SAMPLING_ORG
}



export interface JiangsuParams {
  exactTaskNo?: string
  inputValue: string
  /** 合同号（多个用逗号/分号分隔，模糊匹配）；填写后按合同号导出，忽略任务编号 */
  contractNo?: string
  /**
   * 产品类别筛选（与「年度数据快速统计」同一套归类，见 `helpers.NEW_KIND_MAP`）。
   * 全部类别都勾选时**不加条件**（等于不筛选），理由见下方 `kindCond` 注释。
   */
  kinds?: string[]
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

  // 产品类别筛选（参考「年度数据快速统计」，同一套归类 NEW_KIND_MAP）。
  // ⚠️ **全部类别都勾选时不加任何条件**——实测库里有 19 种样品类别不在 NEW_KIND_MAP 里
  //    （263 个样品，含「产品-农产品-水果」「产品-水产品-鱼类」「环境-水质」等新命名）。
  //    若一律展开成 IN(...)，默认"全选"就会静默丢掉这些样品，等于改变了现有导出结果。
  //    所以：全选 = 不筛选（与改造前完全一致）；只有勾选子集时才真正过滤。
  const kinds = p.kinds || []
  if (!kinds.length) return danger('❌ 请至少选择一个产品类别')
  let kindCond = ''
  if (kinds.length < NEW_KIND_NAMES.length) {
    const vals: string[] = []
    for (const k of kinds) (NEW_KIND_MAP[k] || []).forEach((v) => vals.push(v))
    const uniq = Array.from(new Set(vals))
    if (!uniq.length) return danger('❌ 产品类别无效')
    kindCond = ' AND s.SAMPLE_CATEGORY_NAME IN (' +
      uniq.map((v) => "'" + v.replace(/'/g, "''") + "'").join(',') + ')'
  }

  try {
    const detData = await dmQuery(
      'SELECT s.ID, s.SMALL_NO, s.NAME, s.SAMPLING_POSITION, ' +
        'd.NO, d.BUSINESS_CATEGORY_NAME, d.DETECTED_COMPANY_NAME, d.DETECTED_COMPANY_ADDRESS, ' +
        'd.SAMPLING_DATE, d.ACCEPT_ORG_NAME, d.EVALUATE_RESULT, s.ORIGINAL_NO, ' +
        // 样本级判定结论（s.EVALUATE_RESULT 是规范枚举：符合 / 不合格 / 不符合 / 空），
        // 比单据级的散文结论更适合做判定依据，见下方"结果判定"注释
        's.EVALUATE_RESULT AS SAMPLE_EVALUATE_RESULT ' +
        'FROM DETECTION.DT_DETECTION d ' +
        'LEFT JOIN DETECTION.DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 ' +
        'WHERE d.IS_DELETED = 0' + whereCond + kindCond + ' ' +
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
        'WHERE d.IS_DELETED = 0' + whereCond + kindCond + ' ' +
        'ORDER BY s.ID, sp.SEQ_NO',
    )

    interface JsSampleInfo {
      sampleNo: string
      sampleName: string
      samplingPosition: string
      detectedCompany: string
      address: string
      samplingDate: unknown
      /** 单据级结论（散文，如「…判为不合格品。」），仅作兜底 */
      evaluateResult: string
      /** 样本级结论（规范枚举：符合 / 不合格 / 不符合 / 空），首选判定依据 */
      sampleEvaluate: string
      /** d.BUSINESS_CATEGORY_NAME（业务类别）——「监测类别」「抽样单位」两列的依据 */
      bizCategory: string
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
        sampleEvaluate: String(s[12] || ''),
        bizCategory: String(s[5] || ''),
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
      // 监测类别 / 抽样单位都取自数据（d.BUSINESS_CATEGORY_NAME），不再由模板决定
      row['监测类别'] = categoryLabel(info.bizCategory)
      row['抽样单位'] = samplingOrgOf(info.bizCategory)
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
      // 判定依据（任一命中即"不合格"）：
      //   1) 样本级结论 s.EVALUATE_RESULT —— 规范枚举（符合 / 不合格 / 不符合 / 空），首选；
      //   2) 单据级结论 d.EVALUATE_RESULT —— 散文兜底；
      //   3) 单项目判定 sp.SINGLE_JUDGE（原有逻辑，保留作交叉验证）。
      //
      // ⚠️ 事故背景（2026-09-23）：任务 RW2026042 共 12 张检测单，其中 9 张的 LIMS 样本级
      //    结论已明写"不合格"，但只有 2 张的单项目判定填了"不合格"。另有 7 张的涉事项目
      //    （如恩诺沙星 140、环丙沙星 3.34，远超残留限量）sp.SINGLE_JUDGE 却是 '/'（未判定）。
      //    旧逻辑只读 SINGLE_JUDGE，把这 7 张导出成"合格"——判定列是报告的最终结论，
      //    必须以 LIMS 的结论为准，不能因为单项目判定漏填就改判为合格。
      let hasFail = isFailText(info.sampleEvaluate) || isFailText(info.evaluateResult)
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
          if (isFailText(res.judge)) hasFail = true
        }
      }
      row['结果判定(合格或不合格)'] = hasFail ? '不合格' : '合格'
      rows.push(row)
    }

    const excelRows = rows.map((row) => headers.map((h) => row[h] || ''))
    const fileName = `江苏省追溯平台_${stamp}.xlsx`
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
