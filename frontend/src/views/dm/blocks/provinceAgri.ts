/**
 * 省例行农产品数据汇总（种植业产品例行监测，原 `queryProvinceRoutineAgri`，行 2323~2527）。
 *
 * 逐字保留：
 * - 两段 SQL（与江苏块同款）；
 * - **73 个固定检测项目列**（`PROVINCE_ROUTINE_AGRI_DRUGS`）+ 8 个属性列 + 1 个结果判定列；
 * - 项目匹配三级规则：单项目精确匹配 → **列名包含项目名**（不做反向包含）→ 合并组子项目精确匹配；
 * - 合并组**分子量折算系数** `PROVINCE_ROUTINE_AGRI_FACTORS`（主项目分子量/子项目分子量）加权求和；
 * - 同一项目多条有效记录时**分行列出并标红**（`hasDup && anyVal`）；
 * - `formatSigNum` 有效数字规则、`notDetectedText`（未检出(类型:值)）；
 * - 样品按样品编号 `localeCompare(..., 'zh-CN', {numeric:true})` 排序；
 * - 导出时空值填 `-`（与其它块不同，其它块填空字符串）。
 *
 * 注：原文件另有 `PROVINCE_ROUTINE_AGRI_TITLE` 常量（行 2267）在块内**未被引用**，故未迁移。
 * 另：`res.val` / `res.limitType` 在 `subValues` 恒有值的情况下为走不到的兜底分支，已按原样保留。
 *
 * 新增「按合同号导出」：传入 `contractNo` 时，筛选条件由 `d.TASK_NO = 'x'` 改为
 * `buildContractCond()`（`d.CONTRACTS_NO LIKE`，支持逗号/分号分隔多值），其余取数、
 * 列定义、合并折算、判定口径与文件名规则完全不变。
 * 新增「多任务编号输入」：任务编号走 `resolveTaskCond`（多个/免输 RW 前缀，IN 匹配）。
 */
import { buildContractCond, createStyledExcel, dmQuery, PESTICIDE_NAME_MAP, type CellValue } from '../helpers'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'
import { resolveTaskCond, taskFileStamp, taskListLabel } from './taskResolve'
import { formatSigNum, notDetectedText, parseCityCounty } from './provinceCommon'

const PROVINCE_ROUTINE_AGRI_DRUGS = [
  '腐霉利', '异菌脲', '吡唑醚菌酯', '百菌清', '噻虫嗪', '苯醚甲环唑', '氯虫苯甲酰胺', '除虫脲', '噻虫胺', '霜霉威',
  '嘧菌酯', '联苯菊酯', '阿维菌素', '哒螨灵', '啶虫脒', '毒死蜱', '多菌灵', '多效唑', '二甲戊灵', '氟氯氰菊酯',
  '甲氨基阿维菌素苯甲酸盐', '甲拌磷（包括甲拌磷砜和甲拌磷亚砜）', '克百威（包括3-羟基克百威）', '乐果', '氯氟氰菊酯', '氯氰菊酯', '咪鲜胺', '戊唑醇', '烯酰吗啉', '氧乐果',
  '甲胺磷', '对硫磷', '三氯杀螨醇', '水胺硫磷', '甲基异柳磷', '涕灭威（包括涕灭威砜和涕灭威亚砜）', '灭多威', '灭线磷', '氟虫腈（包括氟甲腈氟虫腈硫醚氟虫腈砜）', '三唑磷',
  '乙酰甲胺磷', '硫环磷', '氯唑磷', '丁硫克百威', '敌敌畏', '丙溴磷', '倍硫磷', '辛硫磷', '氰戊菊酯', '溴氰菊酯',
  '甲氰菊酯', '氯吡脲', '灭幼脲', '吡虫啉', '虫螨腈', '氟啶脲', '五氯硝基苯', '三唑酮', '嘧霉胺', '甲霜灵',
  '乙基多杀菌素', '氯菊酯', '醚菊酯', '虫酰肼', '抑霉唑', '敌百虫', '丙环唑', '灭蝇胺', '多杀霉素', '噻苯隆',
  '甲萘威', '茚虫威', '噻嗪酮', '氟氰戊菊酯', '啶氧菌酯',
]

/** 合并列映射：模板列名 -> 数据库中的多个实际检测项目名 */
const PROVINCE_ROUTINE_AGRI_GROUPS: Record<string, string[]> = {
  '甲拌磷（包括甲拌磷砜和甲拌磷亚砜）': ['甲拌磷', '甲拌磷砜', '甲拌磷亚砜'],
  '克百威（包括3-羟基克百威）': ['克百威', '3-羟基克百威'],
  '涕灭威（包括涕灭威砜和涕灭威亚砜）': ['涕灭威', '涕灭威砜', '涕灭威亚砜'],
  '氟虫腈（包括氟甲腈氟虫腈硫醚氟虫腈砜）': ['氟虫腈', '氟甲腈', '氟虫腈硫醚', '氟虫腈砜', '氟虫腈亚砜'],
  乙基多杀菌素: ['乙基多杀菌素', '乙基多杀菌素J', '乙基多杀菌素L'],
  多杀霉素: ['多杀霉素', '多杀霉素A', '多杀霉素D'],
  三唑酮: ['三唑酮', '三唑醇'],
}

/** 合并列加权系数：子项目 -> 折算系数（主项目分子量/子项目分子量，=1 为直接加和） */
const PROVINCE_ROUTINE_AGRI_FACTORS: Record<string, Record<string, number>> = {
  '甲拌磷（包括甲拌磷砜和甲拌磷亚砜）': { 甲拌磷: 1, 甲拌磷砜: 260.38 / 292.38, 甲拌磷亚砜: 260.38 / 276.38 },
  '克百威（包括3-羟基克百威）': { 克百威: 1, '3-羟基克百威': 221.25 / 237.25 },
  '涕灭威（包括涕灭威砜和涕灭威亚砜）': { 涕灭威: 1, 涕灭威砜: 190.26 / 222.26, 涕灭威亚砜: 190.26 / 206.26 },
  '氟虫腈（包括氟甲腈氟虫腈硫醚氟虫腈砜）': { 氟虫腈: 1, 氟虫腈砜: 437.15 / 453.15, 氟虫腈亚砜: 437.15 / 421.15, 氟甲腈: 437.15 / 389.08 },
  乙基多杀菌素: { 乙基多杀菌素: 1, 乙基多杀菌素J: 1, 乙基多杀菌素L: 1 },
  多杀霉素: { 多杀霉素: 1, 多杀霉素A: 1, 多杀霉素D: 1 },
  三唑酮: { 三唑酮: 1, 三唑醇: 1 },
}

interface SubValue {
  name: string
  dn: string
  v: string
  judge: string
  limitType: string
  limitValue: string
}

export interface ProvinceAgriParams {
  exactTaskNo?: string
  inputValue: string
  /** 合同号（多个用逗号/分号分隔，模糊匹配）；填写后按合同号导出，忽略任务编号 */
  contractNo?: string
  setProgress: (html: string) => void
}

export async function queryProvinceRoutineAgri(p: ProvinceAgriParams): Promise<DmOutcome> {
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
    if (!detData.success) return danger(`❌ 查询失败：${detData.error || '未知错误'}`)
    if (!detData.rows || detData.rows.length === 0) return warn(`⚠️ 未找到${desc}的数据`)

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

    interface AgriSampleInfo {
      sampleNo: string
      sampleName: string
      samplingPosition: string
      detectedCompany: string
      address: string
    }

    const sampleInfo: Record<string, AgriSampleInfo> = {}
    for (const s of samples) {
      sampleInfo[String(s[0])] = {
        sampleNo: String(s[11] || s[1] || ''), // 原始编号 s.ORIGINAL_NO
        sampleName: String(s[2] || ''),
        samplingPosition: String(s[3] || ''), // s.SAMPLING_POSITION（抽样环节）
        detectedCompany: String(s[6] || ''),
        address: String(s[7] || ''), // 受检单位所在地（抽样地点）
      }
    }

    // 汇总每个样品的检测结果（按固定检测项目列，合并组按"填检出的值"处理）
    const resultsBySample: Record<
      string,
      Record<string, { val: string; judge: string; limitType?: string; limitValue?: string; subValues: SubValue[] }>
    > = {}
    if (projData.success && projData.rows) {
      for (const r of projData.rows) {
        const sid = String(r[0])
        const pn = r[1] as string
        if (!pn) continue
        // 检测项目名归一化（映射括号说明等）
        let dn = pn
        if (PESTICIDE_NAME_MAP[pn]) dn = PESTICIDE_NAME_MAP[pn]
        if (!resultsBySample[sid]) resultsBySample[sid] = {}

        // 匹配规则：仅"标题包含项目名"才构成包含关系（不做反向包含匹配）
        // 1) 单项目精确匹配
        let colName = PROVINCE_ROUTINE_AGRI_DRUGS.find((x) => x === dn)
        // 2) 列名包含项目名（标题含项目名，如"甲拌磷（包括...）"包含"甲拌磷"）
        if (!colName) {
          colName = PROVINCE_ROUTINE_AGRI_DRUGS.find((x) => x.indexOf(dn) >= 0)
        }
        // 3) 合并组：子项目精确匹配
        if (!colName) {
          for (const [col, subs] of Object.entries(PROVINCE_ROUTINE_AGRI_GROUPS)) {
            if (subs.includes(dn)) {
              colName = col
              break
            }
          }
        }
        if (!colName) continue

        // 收集该列对应本样品的所有子项目检出值（填检出的值：优先取有数值的）
        if (!resultsBySample[sid][colName]) {
          resultsBySample[sid][colName] = { val: '', judge: '', subValues: [] }
        }
        const v = r[3] !== null && r[3] !== undefined && String(r[3]).trim() !== '' ? String(r[3]) : ''
        // pn 是原始数据库项目名（用于加权系数匹配），dn 是规范化后的模板子项目名
        // r[5]=检出限类型，r[6]=检出限值
        const limitType = String(r[5] || '')
        const limitValue = r[6] !== null && r[6] !== undefined ? String(r[6]) : ''
        resultsBySample[sid][colName].subValues.push({
          name: pn,
          dn,
          v,
          judge: String(r[4] || ''),
          limitType,
          limitValue,
        })
      }
    }

    const headers = ['序号', '检测单位', '被检市县', '样品编号', '样品名称', '受检单位名称', '抽样环节', '抽样地点']
    for (const item of PROVINCE_ROUTINE_AGRI_DRUGS) headers.push(item)
    headers.push('结果判定（合格或不合格）')

    const rows: Record<string, CellValue>[] = []
    const redCells = new Set<string>()
    const colIdxMap: Record<string, number> = {}
    headers.forEach((h, i) => {
      colIdxMap[h] = i
    })
    let seq = 1
    const sampleEntries = Object.entries(sampleInfo).sort((a, b) =>
      String(a[1].sampleNo).localeCompare(String(b[1].sampleNo), 'zh-CN', { numeric: true }),
    )
    for (const [sid, info] of sampleEntries) {
      const row: Record<string, CellValue> = {}
      headers.forEach((h) => {
        row[h] = ''
      })
      row['序号'] = seq++
      row['检测单位'] = '苏州市农产品质量安全监测中心'
      row['被检市县'] = parseCityCounty(info.address)
      row['样品编号'] = info.sampleNo
      row['样品名称'] = info.sampleName
      row['受检单位名称'] = info.detectedCompany
      row['抽样环节'] = info.samplingPosition
      row['抽样地点'] = info.address

      const rowIdx = rows.length
      const results = resultsBySample[sid] || {}
      let hasFail = false
      for (const item of PROVINCE_ROUTINE_AGRI_DRUGS) {
        const res = results[item]
        if (!res) continue
        const subs = res.subValues || []
        const factors = PROVINCE_ROUTINE_AGRI_FACTORS[item] || {}
        const bySub: Record<string, SubValue[]> = {}
        for (const x of subs) {
          ;(bySub[x.dn] = bySub[x.dn] || []).push(x)
        }
        const hasDup = Object.values(bySub).some((arr) => arr.length > 1)
        if (subs.length > 0) {
          // 合并组：各子项目加权求和（按 PROVINCE_ROUTINE_AGRI_FACTORS 折算系数）
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
            const factor = x.dn !== undefined && factors[x.dn] !== undefined ? factors[x.dn] : 1
            sum += num * factor
            anyVal = true
            if (x.judge && (x.judge.includes('不合格') || x.judge === '不符合')) anyFail = true
          }
          if (hasDup && anyVal) {
            // 同一项目出现多条有效记录：分行全部列出并标红
            const lines: string[] = []
            for (const x of subs) {
              if (x.v === '' || x.v === '未检出') continue
              const num = parseFloat(x.v)
              if (isNaN(num)) continue
              const factor = x.dn !== undefined && factors[x.dn] !== undefined ? factors[x.dn] : 1
              lines.push(formatSigNum(num * factor))
            }
            row[item] = lines.join('\n')
            redCells.add(rowIdx + '_' + colIdxMap[item])
            if (anyFail) hasFail = true
          } else if (anyVal) {
            row[item] = formatSigNum(sum) // <1 保留 2 位有效数字，>=1 保留 3 位
            if (anyFail) hasFail = true
          } else {
            row[item] = notDetectedText(firstLimitType, firstLimitValue)
          }
        } else {
          // 单项目（原实现中 subValues 恒有值，此分支为兜底，按原样保留）
          if (res.val !== '' && res.val !== null && res.val !== undefined) {
            row[item] = formatSigNum(res.val)
          } else {
            row[item] = notDetectedText(res.limitType, res.limitValue)
          }
          if (res.judge && (res.judge.includes('不合格') || res.judge === '不符合')) hasFail = true
        }
      }
      row['结果判定（合格或不合格）'] = hasFail ? '不合格' : '合格'
      rows.push(row)
    }

    const excelRows = rows.map((row) =>
      headers.map((h) => (row[h] === '' || row[h] === null || row[h] === undefined ? '-' : row[h])),
    )
    const fileName = `省例行农产品_${stamp}.xlsx`
    try {
      await createStyledExcel(headers, excelRows, '省例行农产品', fileName, {}, redCells)
    } catch (e) {
      return danger(`❌ 导出失败：${esc((e as Error).message)}`)
    }

    return ok(
      `✅ 导出完成！共 ${rows.length} 个样品，${PROVINCE_ROUTINE_AGRI_DRUGS.length} 种检测项目。<br>📁 文件：${fileName}`,
    )
  } catch (e) {
    return danger(`❌ 失败：${esc((e as Error).message)}`)
  }
}
