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
 */
import { createStyledExcel, dmQuery, NEW_KIND_MAP, buildContractCond, reorderPesticides, type CellValue } from '../helpers'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'
import { formatSigNum, notDetectedText, parseCityCounty } from './provinceCommon'

/** 合并统计组：标准列名 -> { 子项目: 折算系数(=1为直接加和) }（块内第三份，未与农产品块合并） */
const YEARLY_GROUPS: Record<string, Record<string, number>> = {
  '甲拌磷（包括甲拌磷砜和甲拌磷亚砜）': { 甲拌磷: 1, 甲拌磷砜: 260.38 / 292.38, 甲拌磷亚砜: 260.38 / 276.38 },
  '克百威（包括3-羟基克百威）': { 克百威: 1, '3-羟基克百威': 221.25 / 237.25 },
  '涕灭威（包括涕灭威砜和涕灭威亚砜）': { 涕灭威: 1, 涕灭威砜: 190.26 / 222.26, 涕灭威亚砜: 190.26 / 206.26 },
  '氟虫腈（包括氟甲腈氟虫腈硫醚氟虫腈砜）': { 氟虫腈: 1, 氟虫腈砜: 437.15 / 453.15, 氟虫腈亚砜: 437.15 / 421.15, 氟甲腈: 437.15 / 389.08 },
  乙基多杀菌素: { 乙基多杀菌素: 1, 乙基多杀菌素J: 1, 乙基多杀菌素L: 1 },
  多杀霉素: { 多杀霉素: 1, 多杀霉素A: 1, 多杀霉素D: 1 },
  三唑酮: { 三唑酮: 1, 三唑醇: 1 },
  '氟苯尼考（氟苯尼考+氟苯尼考胺）': { 氟苯尼考: 1, 氟苯尼考胺: 1 },
  'β-内酰胺酶': { 'β-内酰胺酶': 1 },
}

/** 项目名归一化：合并异名 / 去括号注释（块内独立实现，未与其它页/块合并） */
function normalizeYearlyItem(name: unknown): string {
  let s = String(name || '').trim()
  if (!s) return ''
  s = s.replace(/^[﹡△*☆★]+/, '').trim()
  const aliasMap: Record<string, string> = { 克伦特罗: '克仑特罗', 盐酸克伦特罗: '克仑特罗' }
  if (aliasMap[s]) return aliasMap[s]
  const m = s.match(/^([\u4e00-\u9fa5A-Za-z0-9，、·β]+?)\s*[（(\[][^（(\[）)\]]*[）)\]]\s*$/)
  if (m && m[1].length >= 2) {
    const inner = m[0].match(/[（(\[][^（(\[）)\]]*[）)\]]/)![0]
    if (/有效态|干土|鲜土|总量|以总量计|水溶性|可滴定|鲜样|干样|总酸|游离态|结合态|全盐量/.test(inner)) return s
    return m[1]
  }
  return s
}

/** 匹配合并组：返回标准列名；非合并组返回 null */
function findYearlyGroupCol(dn: string): string | null {
  if (YEARLY_GROUPS[dn]) return dn
  for (const col of Object.keys(YEARLY_GROUPS)) {
    if (YEARLY_GROUPS[col][dn] !== undefined) return col
  }
  return null
}

interface GroupSubValue {
  subName: string
  factor: number
  v: string
  judge: string
  limitType: string
  limitValue: string
}

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
        'd.TASK_NAME, d.CONTRACTS_NO ' +
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
        'r.REPORT_VAL, sp.SINGLE_JUDGE, sp.DETECTION_LIMIT_TYPE, sp.DETECTION_LIMIT_VALUE ' +
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
      }
    }

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

        // 合并组匹配：子项目归并到标准列，加权求和
        const groupCol = findYearlyGroupCol(dn)
        if (groupCol) {
          const factor = YEARLY_GROUPS[groupCol][dn] !== undefined ? YEARLY_GROUPS[groupCol][dn] : 1
          pesticideSet.add(groupCol)
          pesticideUnit[groupCol] = pesticideUnit[groupCol] || String(r[2] || '')
          if (!resultsBySample[sampleId]) resultsBySample[sampleId] = {}
          if (!resultsBySample[sampleId][groupCol]) {
            resultsBySample[sampleId][groupCol] = { group: true, subValues: [] }
          }
          const cell = resultsBySample[sampleId][groupCol] as { group: true; subValues: GroupSubValue[] }
          cell.subValues.push({ subName: dn, factor, v, judge, limitType, limitValue })
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
      let hasFail = false
      for (const pi of pesticides) {
        const res = results[pi]
        if (!res) continue
        const colKey = pi + (pesticideUnit[pi] || '')
        if (res.group) {
          // 合并组：各子项目加权求和；若同一子项目出现多条有效记录，则分行全部列出并标红
          const subs = res.subValues || []
          // ⚠️ 原实现此处为 bySub[x.dn]，而合并组子项对象只有 subName、没有 dn，
          //    因此分组键恒为 'undefined' —— 等价于"该组只要有 >1 条子项记录就判定为重复"。
          //    为保持与旧版一致的导出结果，此处按原样复现（未修正为 subName）。
          const bySub: Record<string, GroupSubValue[]> = {}
          for (const x of subs) {
            const key = String((x as { dn?: string }).dn)
            ;(bySub[key] = bySub[key] || []).push(x)
          }
          const hasDup = Object.values(bySub).some((arr) => arr.length > 1)
          let sum = 0
          let anyVal = false
          let anyFail = false
          let firstLimitType = ''
          let firstLimitValue = ''
          for (const x of subs) {
            if (!firstLimitType && x.limitType) firstLimitType = x.limitType
            if (!firstLimitValue && x.limitValue) firstLimitValue = x.limitValue
            if (x.v === '' || x.v === '未检出') continue
            const num = parseFloat(x.v)
            if (isNaN(num)) continue
            sum += num * x.factor
            anyVal = true
            if (x.judge && (x.judge.includes('不合格') || x.judge === '不符合')) anyFail = true
          }
          if (hasDup && anyVal) {
            const lines: string[] = []
            for (const x of subs) {
              if (x.v === '' || x.v === '未检出') continue
              const num = parseFloat(x.v)
              if (isNaN(num)) continue
              lines.push(formatSigNum(num * x.factor))
            }
            row[colKey] = lines.join('\n')
            redCells.add(rowIdx + '_' + colIdxMap[colKey])
            if (anyFail) hasFail = true
          } else if (anyVal) {
            row[colKey] = formatSigNum(sum)
            if (anyFail) hasFail = true
          } else {
            row[colKey] = notDetectedText(firstLimitType, firstLimitValue)
          }
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
