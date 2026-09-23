/**
 * 年度数据快速统计（原 `queryNewTemplate`，行 666~963）。
 *
 * 逐字保留：
 * - 年份按 `EXTRACT(YEAR FROM d.ACCEPT_TIME)` 过滤、合同编号走 `buildContractCond`（模糊多值）、
 *   产品类别展开为 `SAMPLE_CATEGORY_NAME IN (...)`；
 * - `YEARLY_GROUPS` **第三份**折算系数表（在农产品 7 组基础上多了畜产品「氟苯尼考」与「β-内酰胺酶」，共 9 组）
 *   —— 与 `provinceAgri.ts` 里那份**不同**，未合并；
 * - `normalizeYearlyItem`（块内独立实现）：去 `﹡△*☆★` 前缀、克伦特罗→克仑特罗异名映射、
 *   去尾部括号注释（括号内为形态/限量修饰词时保留）。其字符类含 `β`、敏感词表与 SQLServer 页的 `normalizeItem` **不同**，未合并；
 * - 合并组按 `factor` 加权求和 + `formatSigNum`；重复子项分行标红；
 * - 独立列：多条有效记录分行标红，空值以 `notDetectedText` 呈现；
 * - 表头为「11 个属性列 + 动态项目列（带单位后缀）」、导出用 `colorMap` 给项目列表头上色、
 *   空值填 `''`（注意：**不是** `-`，与省例行三块不同）；文件名固定为 `样品检测结果汇总_{年}年.xlsx`。
 *
 * ⚠️ 一处**疑似原实现缺陷，已按原行为复现、未做修正**（详见 `anyDupInGroup` 注释）：
 *   合并组按 `x.dn` 分组判断重复，但子项对象字段名是 `subName`（无 `dn`），
 *   导致分组键恒为 `undefined`——等价于"只要该组有多条子项记录即判定为重复"，
 *   因而走"分行列出"而非"加权求和"。如需修正会改变导出结果，故留待确认。
 *
 * 判定口径修正（2026-09-23）：「判定结果」列原先**只**由动态项目列的单项目判定推出，
 * 样品 SQL **一个结论字段都没查**。现补取 `s.EVALUATE_RESULT` / `d.EVALUATE_RESULT`
 * （追加在 SQL 末尾，不移动既有列下标），判定改为
 * `isFailText(样本级) || isFailText(单据级)` **优先**（谓词见 `provinceCommon.isFailText`），
 * 单项目判定保留作交叉验证；该列只输出 `合格` / `不合格`。
 * 实测合同 PS2026004：21 个不合格样品里有 9 个的单项目判定完全没填 → 旧实现必判「合格」。
 */
import { createStyledExcel, dmQuery, NEW_KIND_MAP, buildContractCond, reorderPesticides, type CellValue } from '../helpers'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'
import { formatSigNum, isFailText, notDetectedText, parseCityCounty } from './provinceCommon'
import { ensureRules, factorOf, findGroupByMember, findGroupByTarget, getRule, resolveName } from '../rules'
import { resolveCell, type SubValue } from '../merge'

/**
 * 合并统计组已改为**可配置规则**（`/api/lims-rules`，页面 `/limsrules`）。
 * 内置默认值 = 改造前的 `YEARLY_GROUPS`（= 农产品 7 组 + 畜产品「氟苯尼考」「β-内酰胺酶」）。
 * ⚠️ 本模板默认的判重方式是「整组」——复刻旧版疑似缺陷（旧代码分组键恒为 'undefined'），
 *    因此组内多条记录时走"分行列出 + 标红"；如需改成与农产品一致（按项目名判重、加权求和），
 *    在规则页把该组的「判重方式」改成"按项目名"即可。
 */
const TEMPLATE_ID = 'yearlyStats'

/** 项目名归一化：合并异名 / 去括号注释（块内独立实现，未与其它页/块合并） */
function normalizeYearlyItem(name: unknown): string {
  let s = String(name || '').trim()
  if (!s) return ''
  s = s.replace(/^[﹡△*☆★]+/, '').trim()
  // 别名来自规则层；本模板不继承全局别名表（与旧实现一致，见 rules.TEMPLATE_MATCH）
  const alias = resolveName(TEMPLATE_ID, s)
  if (alias !== s) return alias
  const m = s.match(/^([\u4e00-\u9fa5A-Za-z0-9，、·β]+?)\s*[（(\[][^（(\[）)\]]*[）)\]]\s*$/)
  if (m && m[1].length >= 2) {
    const inner = m[0].match(/[（(\[][^（(\[）)\]]*[）)\]]/)![0]
    if (/有效态|干土|鲜土|总量|以总量计|水溶性|可滴定|鲜样|干样|总酸|游离态|结合态|全盐量/.test(inner)) return s
    return m[1]
  }
  return s
}

/** 合并组子项 = 规则层统一类型（原块内的 `GroupSubValue`，字段由 subName 改为 dn） */
type GroupSubValue = SubValue

type YearlyCell =
  | { group: true; subValues: GroupSubValue[] }
  | { group: false; vals: string[]; judge: string; limitType: string; limitValue: string }

export interface YearlyStatsParams {
  year: string | number
  contract: string
  kinds: string[]
  isConnected: boolean
  setProgress: (html: string) => void
}

export async function queryNewTemplate(p: YearlyStatsParams): Promise<DmOutcome> {
  const year = String(p.year ?? '')
  if (!year) return danger('❌ 请选择检测年份')
  if (p.kinds.length === 0) return danger('❌ 请至少选择一个产品类别')
  if (!p.isConnected) return danger('❌ 请先连接数据库')

  p.setProgress('⏳ 正在查询样品数据...')

  try {
    // 规则（别名 + 合并组）一次拉取，其后走缓存；后端不可用时退回内置默认
    await ensureRules()
    // 产品类别展开为 SAMPLE_CATEGORY_NAME 列表
    const catValues: string[] = []
    for (const k of p.kinds) {
      ;(NEW_KIND_MAP[k] || []).forEach((v) => catValues.push(v))
    }
    const catIn = Array.from(new Set(catValues))
      .map((v) => `'${v.replace(/'/g, "''")}'`)
      .join(',')
    const contractCond = buildContractCond(p.contract)
    const yearCond = 'EXTRACT(YEAR FROM d.ACCEPT_TIME) = ' + parseInt(year, 10)

    // 1. 样品信息
    const detData = await dmQuery(
      'SELECT s.ID, s.SMALL_NO, s.NAME, s.SAMPLING_POSITION, s.SAMPLE_CATEGORY_NAME, ' +
        'd.NO, d.DETECTED_COMPANY_NAME, d.DETECTED_COMPANY_ADDRESS, d.ACCEPT_TIME, ' +
        'd.TASK_NAME, d.CONTRACTS_NO, s.EVALUATE_RESULT, d.EVALUATE_RESULT ' +
        'FROM DETECTION.DT_DETECTION d ' +
        'LEFT JOIN DETECTION.DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 ' +
        'WHERE d.IS_DELETED = 0 AND ' + yearCond + contractCond + ' ' +
        'AND s.SAMPLE_CATEGORY_NAME IN (' + catIn + ') ' +
        'ORDER BY d.NO, s.SMALL_NO',
    )
    if (!detData.success) return danger(`❌ 查询失败：${detData.error || '未知错误'}`)
    const samples = detData.rows || []
    if (samples.length === 0) return warn('⚠️ 该条件下没有样品数据')

    p.setProgress(`⏳ 已找到 ${samples.length} 个样品，正在查询检测项目...`)

    // 2. 检测项目
    const projData = await dmQuery(
      'SELECT s.ID, sp.DECIDE_PROJECT_NAME, sp.METERING_UNIT_NAME, ' +
        'r.REPORT_VAL, sp.SINGLE_JUDGE, sp.DETECTION_LIMIT_TYPE, sp.DETECTION_LIMIT_VALUE, ' +
        'sp.ID AS SP_ID, sp.CREATE_DATETIME AS SP_CREATED ' +
        'FROM DETECTION.DT_DETECTION d ' +
        'LEFT JOIN DETECTION.DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 ' +
        "LEFT JOIN DETECTION.DT_SAMPLE_PROJECT sp ON sp.SAMPLE_ID = s.ID AND sp.IS_DELETED = 0 AND NVL(sp.IS_LOGOUT, 'NO') <> 'YES' " +
        'LEFT JOIN DETECTION.DT_RESULT_CHECK_IN r ON r.SAMPLE_PROJECT_ID = sp.ID AND r.IS_DELETED = 0 ' +
        'WHERE d.IS_DELETED = 0 AND ' + yearCond + contractCond + ' ' +
        'AND s.SAMPLE_CATEGORY_NAME IN (' + catIn + ') ' +
        'ORDER BY s.ID, sp.SEQ_NO',
    )

    interface YearSampleInfo {
      sampleNo: string
      sampleName: string
      samplingPosition: string
      categoryName: string
      detectedCompany: string
      address: string
      acceptTime: unknown
      taskName: string
      contractsNo: string
      /** 样本级结论 s.EVALUATE_RESULT（规范枚举：符合 / 不合格 / 不符合 / 空），首选判定依据 */
      sampleEvaluate: string
      /** 单据级结论 d.EVALUATE_RESULT（散文，如「…判为不合格品。」），仅作兜底 */
      evaluateResult: string
    }

    const sampleInfo: Record<string, YearSampleInfo> = {}
    for (const s of samples) {
      sampleInfo[String(s[0])] = {
        sampleNo: s[5] ? String(s[5]) + String(s[1] || '') : String(s[1] || ''),
        sampleName: String(s[2] || ''),
        samplingPosition: String(s[3] || ''),
        categoryName: String(s[4] || ''),
        detectedCompany: String(s[6] || ''),
        address: String(s[7] || ''),
        acceptTime: s[8] || '',
        taskName: String(s[9] || ''),
        contractsNo: String(s[10] || ''),
        // 两列结论追加在 SQL 末尾，不移动既有列的下标
        sampleEvaluate: String(s[11] || ''), // s.EVALUATE_RESULT
        evaluateResult: String(s[12] || ''), // d.EVALUATE_RESULT
      }
    }

    // 本模板生效规则（后端版本优先，取不到则内置默认）
    const rule = getRule(TEMPLATE_ID)
    const groups = rule.groups
    // 检测项目（含合并统计与加和计算）
    const pesticideSet = new Set<string>()
    const pesticideUnit: Record<string, string> = {}
    const resultsBySample: Record<string, Record<string, YearlyCell>> = {}
    if (projData.success && projData.rows) {
      for (const r of projData.rows) {
        const sampleId = String(r[0])
        const projectName = r[1] as string
        if (!projectName) continue
        // 项目名归一化（合并异名/去括号）
        const dn = normalizeYearlyItem(projectName)
        if (!dn) continue
        const v = r[3] !== null && r[3] !== undefined && String(r[3]).trim() !== '' ? String(r[3]) : ''
        const judge = String(r[4] || '')
        const limitType = String(r[5] || '')
        const limitValue = r[6] !== null && r[6] !== undefined ? String(r[6]) : ''

        // 合并组匹配：子项目归并到标准列，按规则系数折算
        const gp = findGroupByMember(groups, dn)
        if (gp) {
          const groupCol = gp.target
          pesticideSet.add(groupCol)
          pesticideUnit[groupCol] = pesticideUnit[groupCol] || String(r[2] || '')
          if (!resultsBySample[sampleId]) resultsBySample[sampleId] = {}
          if (!resultsBySample[sampleId][groupCol]) {
            resultsBySample[sampleId][groupCol] = { group: true, subValues: [] }
          }
          const cell = resultsBySample[sampleId][groupCol] as { group: true; subValues: GroupSubValue[] }
          cell.subValues.push({
            dn,
            v,
            judge,
            limitType,
            limitValue,
            factor: factorOf(gp, dn),
            spId: r[7] !== null && r[7] !== undefined ? String(r[7]) : '',
            createdAt: r[8] !== null && r[8] !== undefined ? String(r[8]) : '',
          })
        } else {
          // 独立列
          pesticideSet.add(dn)
          pesticideUnit[dn] = pesticideUnit[dn] || String(r[2] || '')
          if (!resultsBySample[sampleId]) resultsBySample[sampleId] = {}
          if (!resultsBySample[sampleId][dn]) {
            resultsBySample[sampleId][dn] = { group: false, vals: [], judge: '', limitType, limitValue }
          }
          const oc = resultsBySample[sampleId][dn] as {
            group: false
            vals: string[]
            judge: string
            limitType: string
            limitValue: string
          }
          oc.vals.push(v)
          if (judge) oc.judge = judge
          if (!oc.limitValue && limitValue) oc.limitValue = limitValue
          if (!oc.limitType && limitType) oc.limitType = limitType
        }
      }
    }

    const { reordered: pesticides, colorMap } = reorderPesticides(Array.from(pesticideSet).sort(), pesticideUnit)
    const headers = [
      '样品编号', '样品名称', '类别', '抽样环节', '区域', '抽样地址', '主体名称',
      '日期', '判定结果', '任务名称', '合同编号',
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
      row['样品名称'] = info.sampleName
      row['类别'] = info.categoryName
      row['抽样环节'] = info.samplingPosition
      row['区域'] = parseCityCounty(info.address)
      row['抽样地址'] = info.address
      row['主体名称'] = info.detectedCompany
      row['日期'] = info.acceptTime ? String(info.acceptTime).split('T')[0] : ''
      row['任务名称'] = info.taskName
      row['合同编号'] = info.contractsNo

      const rowIdx = rows.length
      const results = resultsBySample[sampleId] || {}
      // 判定依据（任一命中即「不合格」）：与省例行四块统一 ——
      //   1) 样本级结论 s.EVALUATE_RESULT（规范枚举，首选）；
      //   2) 单据级结论 d.EVALUATE_RESULT（散文，兜底）；
      //   3) 动态项目列的单项目判定（原有逻辑，保留作交叉验证）。
      //
      // ⚠️ 事故背景（2026-09-23）：旧实现的样品 SQL **一个结论字段都没查**，判定只由单项目判定推出。
      //    实测合同 PS2026004 的 21 个不合格样品中，有 **9 个**的单项目判定完全没填，
      //    旧实现必然把它们导出成「合格」。注意本块的项目列是**动态生成**的，
      //    不存在「涉事项目不在固定列清单」那一类漏判（那类只影响省例行农/畜产品块）。
      let hasFail = isFailText(info.sampleEvaluate) || isFailText(info.evaluateResult)
      for (const pi of pesticides) {
        const res = results[pi]
        if (!res) continue
        const colKey = pi + (pesticideUnit[pi] || '')
        if (res.group) {
          // 合并组：合并方式 / 判重方式 / 重复策略取自规则
          // （默认 dupKey='group' 复刻旧版"整组判重"行为，与农产品块的口径差异见文件头说明）
          const subs = res.subValues || []
          const gp = findGroupByTarget(groups, pi)
          const plan = resolveCell(subs, {
            combine: gp?.combine || rule.defaultCombine,
            // 【2026-09-22 口径变更】判重按项目名（原先为复刻旧缺陷用「整组判重」，
            // 导致组内多条只分行标红、不做合并求和；现已取消，与农产品块一致）
            dupKey: 'dn',
            dupPolicy: gp?.dupPolicy || 'keepLines',
            failSource: 'valued',
            linesRequireValue: true,
            // 年度块合并组一律按数值口径（旧实现统一 parseFloat）
            valueFilter: 'numeric',
          })
          if (plan.mode === 'lines') {
            row[colKey] = plan.numericLines.map((n) => formatSigNum(n)).join('\n')
            redCells.add(rowIdx + '_' + colIdxMap[colKey])
          } else if (plan.mode === 'single') {
            row[colKey] = plan.single === null ? plan.singleRaw : formatSigNum(plan.single)
          } else {
            row[colKey] = notDetectedText(plan.limitType, plan.limitValue)
          }
          if (plan.anyFail) hasFail = true
        } else {
          // 独立列：多条有效记录则分行全部列出并标红
          const vals = res.vals || []
          if (vals.length > 1) {
            row[colKey] = vals
              .map((v2) =>
                v2 === '' || v2 === null || v2 === undefined
                  ? notDetectedText(res.limitType, res.limitValue)
                  : formatSigNum(v2),
              )
              .join('\n')
            redCells.add(rowIdx + '_' + colIdxMap[colKey])
          } else if (vals.length === 1 && vals[0] !== '' && vals[0] !== null && vals[0] !== undefined) {
            row[colKey] = formatSigNum(vals[0])
          } else {
            row[colKey] = notDetectedText(res.limitType, res.limitValue)
          }
          if (res.judge && (res.judge.includes('不合格') || res.judge === '不符合')) hasFail = true
        }
      }
      row['判定结果'] = hasFail ? '不合格' : '合格'
      rows.push(row)
    }

    const excelRows = rows.map((row) => headers.map((h) => row[h] || ''))
    const fileName = `样品检测结果汇总_${year}年.xlsx`
    try {
      await createStyledExcel(headers, excelRows, '样品检测结果汇总', fileName, colorMap, redCells)
    } catch (e) {
      return danger(`❌ 导出失败：${esc((e as Error).message)}`)
    }
    return ok(
      `✅ 导出完成！共 ${samples.length} 个样品，${pesticides.length} 个检测项目。<br>📁 ${fileName}`,
    )
  } catch (e) {
    return danger(`❌ 失败：${esc((e as Error).message)}`)
  }
}
