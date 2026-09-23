/**
 * 省例行水产品数据汇总（原 `queryProvinceRoutineAquatic`，行 2880~3035）。
 *
 * 逐字保留：
 * - det SQL 取 `d.PRODUCTION_COMPANY_ADDRESS`（用于「溯源省市县」）与 `d.EVALUATE_RESULT`，列序与农/畜块不同；
 * - **33 个固定兽药列**（`PROVINCE_ROUTINE_AQUATIC_DRUGS`）+ 15 个属性列 + 「检测单位」「备注」两列（注意**在项目列之后**）；
 * - `parseAddressLevels` 分别解析「抽样地址」与「溯源地址」得到 省/市/县 六列；
 * - `mapAquaLoop`（抽样环节 5 类）与 `mapAquaPlace`（抽样地点，按企业名称判散户/家庭农场/合作社，其余按采样位置归 16 类）；
 * - 规范化优先 `PROVINCE_ROUTINE_AQUATIC_MAP`，其次 `PESTICIDE_NAME_MAP`；
 * - 匹配规则只有两级（精确/去括号基础名 → 列名包含），**没有合并组步骤**（该块各兽药为独立列）；
 * - 单元格取值：重复记录分行列出**原始值**并标红，否则取首个非空值，无值写 `未检出`；
 * - **「判定结果」只输出 `合格` / `不合格`**（2026-09-23 变更）：原先**逐字照抄单据级散文**
 *   （如「该批（次）产品经检验检测，依据规定，判为不合格品。」）、为空时兜底 `合格`；
 *   现与农/畜块统一 —— `isFailText(样本级结论) || isFailText(单据级结论)` 优先，
 *   本项目列的单项目判定作交叉验证（谓词见 `provinceCommon.isFailText`）；
 * - 空值导出填 `-`；样品按编号 `localeCompare(..., 'zh-CN', {numeric:true})` 排序。
 *
 * 新增「多任务编号输入」：任务编号支持多个（逗号/分号/空白分隔），纯数字或小写 rw 自动补全为
 * RW 前缀（`resolveTaskCond`），命中合并去重后按 `d.TASK_NO IN (...)` 导出；其余取数、列定义、
 * 判定口径与文件名规则完全不变。
 * 新增「按合同号导出」：传入 `contractNo` 时，筛选条件改用 `buildContractCond()`
 * （`d.CONTRACTS_NO LIKE`，支持逗号/分号分隔多值），其余取数、列定义、判定口径与文件名规则完全不变。
 */
import { buildContractCond, createStyledExcel, dmQuery, type CellValue } from '../helpers'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'
import { mapAquaLoop, mapAquaPlace, parseAddressLevels } from '../helpers'
import { isFailText } from './provinceCommon'
import { resolveTaskCond, taskFileStamp, taskListLabel } from './taskResolve'
import { ensureRules, factorOf, findGroupByTarget, getRule, resolveColumn, resolveName } from '../rules'
import { resolveCell, type SubValue } from '../merge'

const PROVINCE_ROUTINE_AQUATIC_DRUGS = [
  '氯霉素', '甲砜霉素', '氟苯尼考', '氟苯尼考胺', '呋喃唑酮代谢物', '呋喃西林代谢物', '呋喃妥因代谢物', '呋喃它酮代谢物', '孔雀石绿', '地西泮',
  '磺胺噻唑', '磺胺嘧啶', '磺胺甲基嘧啶', '磺胺二甲基嘧啶', '磺胺甲基异噁唑', '磺胺多辛', '磺胺异噁唑', '磺胺喹噁啉', '磺胺间甲氧嘧啶', '磺胺间二甲氧嘧啶',
  '磺胺氯哒嗪', '磺胺甲噻二唑', '恩诺沙星', '环丙沙星', '诺氟沙星', '氧氟沙星', '培氟沙星', '洛美沙星', '四环素', '土霉素',
  '金霉素', '多西环素', '甲氧苄啶',
]

/**
 * 「兽药名规范化映射」已改为**可配置规则**（`/api/lims-rules`，页面 `/limsrules`）。
 * 内置默认值 = 改造前的 `PROVINCE_ROUTINE_AQUATIC_MAP`；本模板没有合并列（各兽药为独立列）。
 */
const TEMPLATE_ID = 'provinceAquatic'

export interface ProvinceAquaticParams {
  exactTaskNo?: string
  inputValue: string
  /** 合同号（多个用逗号/分号分隔，模糊匹配）；填写后按合同号导出，忽略任务编号 */
  contractNo?: string
  setProgress: (html: string) => void
}

export async function queryProvinceRoutineAquatic(p: ProvinceAquaticParams): Promise<DmOutcome> {
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
    // 规则（别名）一次拉取，其后走缓存；后端不可用时退回内置默认
    await ensureRules()
    const detData = await dmQuery(
      'SELECT s.ID, s.SMALL_NO, s.NAME, s.SAMPLING_POSITION, ' +
        'd.NO, d.BUSINESS_CATEGORY_NAME, d.DETECTED_COMPANY_NAME, d.DETECTED_COMPANY_ADDRESS, ' +
        'd.PRODUCTION_COMPANY_NAME, d.PRODUCTION_COMPANY_ADDRESS, d.EVALUATE_RESULT, s.ORIGINAL_NO, ' +
        's.EVALUATE_RESULT ' +
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
        'r.REPORT_VAL, sp.SINGLE_JUDGE, sp.DETECTION_LIMIT_TYPE, sp.DETECTION_LIMIT_VALUE, ' +
        'sp.ID AS SP_ID, sp.CREATE_DATETIME AS SP_CREATED ' +
        'FROM DETECTION.DT_DETECTION d ' +
        'LEFT JOIN DETECTION.DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 ' +
        "LEFT JOIN DETECTION.DT_SAMPLE_PROJECT sp ON sp.SAMPLE_ID = s.ID AND sp.IS_DELETED = 0 AND NVL(sp.IS_LOGOUT, 'NO') <> 'YES' " +
        'LEFT JOIN DETECTION.DT_RESULT_CHECK_IN r ON r.SAMPLE_PROJECT_ID = sp.ID AND r.IS_DELETED = 0 ' +
        'WHERE d.IS_DELETED = 0' + whereCond + ' ' +
        'ORDER BY s.ID, sp.SEQ_NO',
    )

    interface AqSampleInfo {
      sampleNo: string
      sampleName: string
      monitorLink: string
      samplePlace: string
      sampleAddr: string
      spProv: string
      spCity: string
      spCounty: string
      enterprise: string
      opProv: string
      opCity: string
      opCounty: string
      origin: string
      evaluate: string
      /** 样本级结论 s.EVALUATE_RESULT（规范枚举：符合 / 不合格 / 不符合 / 空），首选判定依据 */
      sampleEvaluate: string
    }

    const sampleInfo: Record<string, AqSampleInfo> = {}
    for (const s of samples) {
      const sampleAddr = String(s[7] || '') // d.DETECTED_COMPANY_ADDRESS（抽样地址）
      const originAddr = String(s[9] || '') // d.PRODUCTION_COMPANY_ADDRESS（溯源省市县）
      const sp = parseAddressLevels(sampleAddr)
      const op = parseAddressLevels(originAddr)
      sampleInfo[String(s[0])] = {
        sampleNo: String(s[11] || s[1] || ''), // 原始编号 s.ORIGINAL_NO
        sampleName: String(s[2] || ''),
        monitorLink: mapAquaLoop(s[3]), // 抽样环节（归类为5类）
        samplePlace: mapAquaPlace(s[6], s[3]), // 抽样地点
        sampleAddr,
        spProv: sp.prov,
        spCity: sp.city,
        spCounty: sp.county,
        enterprise: String(s[6] || ''), // 企业名称 = 受检单位
        opProv: op.prov,
        opCity: op.city,
        opCounty: op.county,
        origin: String(s[8] || ''), // 溯源产地 = 生产单位
        evaluate: String(s[10] || ''), // 单据级结论 d.EVALUATE_RESULT（散文，仅作兜底）
        // s.EVALUATE_RESULT —— 追加在 SQL 末尾，不移动既有列的下标
        sampleEvaluate: String(s[12] || ''),
      }
    }

    // 汇总检测结果（氟苯尼考与氟苯尼考胺单独计算）
    // 本模板生效规则（后端版本优先，取不到则内置默认）
    const rule = getRule(TEMPLATE_ID)
    const groups = rule.groups
    const resultsBySample: Record<string, Record<string, { subValues: SubValue[] }>> = {}
    if (projData.success && projData.rows) {
      for (const r of projData.rows) {
        const sid = String(r[0])
        const pn = r[1] as string
        if (!pn) continue
        // 兽药名归一化：模板别名优先，其次全局别名
        const dn = resolveName(TEMPLATE_ID, pn)
        if (!resultsBySample[sid]) resultsBySample[sid] = {}

        // 匹配规则（两级）：列名精确（含去括号基础名）→ 列名包含；本块没有合并组
        const hit = resolveColumn(TEMPLATE_ID, dn, PROVINCE_ROUTINE_AQUATIC_DRUGS, groups)
        if (!hit) continue
        const colName = hit.column

        if (!resultsBySample[sid][colName]) resultsBySample[sid][colName] = { subValues: [] }
        const v = r[3] !== null && r[3] !== undefined && String(r[3]).trim() !== '' ? String(r[3]) : ''
        resultsBySample[sid][colName].subValues.push({
          dn,
          v,
          judge: String(r[4] || ''),
          limitType: String(r[5] || ''),
          limitValue: r[6] !== null && r[6] !== undefined ? String(r[6]) : '',
          factor: factorOf(hit.group, dn),
          spId: r[7] !== null && r[7] !== undefined ? String(r[7]) : '',
          createdAt: r[8] !== null && r[8] !== undefined ? String(r[8]) : '',
        })
      }
    }

    const headers = [
      '序号', '样品编号', '样品名称', '抽样环节', '抽样地点', '抽样省', '抽样市', '抽样县', '抽样地址',
      '企业名称', '溯源省', '溯源市', '溯源县', '溯源产地', '判定结果',
    ]
    for (const item of PROVINCE_ROUTINE_AQUATIC_DRUGS) headers.push(item)
    headers.push('检测单位', '备注')

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
      row['样品编号'] = info.sampleNo
      row['样品名称'] = info.sampleName
      row['抽样环节'] = info.monitorLink
      row['抽样地点'] = info.samplePlace
      row['抽样省'] = info.spProv
      row['抽样市'] = info.spCity
      row['抽样县'] = info.spCounty
      row['抽样地址'] = info.sampleAddr
      row['企业名称'] = info.enterprise
      row['溯源省'] = info.opProv
      row['溯源市'] = info.opCity
      row['溯源县'] = info.opCounty
      row['溯源产地'] = info.origin
      row['检测单位'] = '苏州市农产品质量安全监测中心'

      const rowIdx = rows.length
      const results = resultsBySample[sid] || {}
      // 判定依据（任一命中即「不合格」）：
      //   1) 样本级结论 s.EVALUATE_RESULT（规范枚举：符合 / 不合格 / 不符合 / 空，首选）；
      //   2) 单据级结论 d.EVALUATE_RESULT（散文，兜底）；
      //   3) 本项目列的单项目判定（交叉验证）。
      //
      // ⚠️ 2026-09-23 变更：原先本列**逐字照抄单据级散文**（「该批（次）产品经检验检测，……判为
      //    不合格品。」）、单据级为空时兜底「合格」——既不好用，也会把「单据级为空但样本级已判
      //    不符合」的样品导成「合格」（实测合同 PS2026004 的 JSLX032026010107 即此情形）。
      //    现统一为只输出 `合格` / `不合格`，口径与农/畜块一致。
      let hasFail = isFailText(info.sampleEvaluate) || isFailText(info.evaluate)
      for (const item of PROVINCE_ROUTINE_AQUATIC_DRUGS) {
        const res = results[item]
        if (!res || !res.subValues || res.subValues.length === 0) continue
        // 单项目判定作交叉验证（存在「两级结论都为空、但项目级判定已写不合格」的样品）
        for (const sv of res.subValues) {
          if (isFailText(sv.judge)) hasFail = true
        }
        // 本块各兽药为独立列：取首个检出值；同一项目多条时分行列出并标红
        const plan = resolveCell(res.subValues, {
          combine: rule.defaultCombine,
          // 判重一律按项目名（「整组判重」已于 2026-09-22 取消）
          dupKey: 'dn',
          dupPolicy: 'keepLines',
          failSource: 'none',
          linesRequireValue: false,
          valueFilter: 'nonEmpty',
        })
        if (plan.mode === 'lines') {
          row[item] = plan.rawLines.length ? plan.rawLines.join('\n') : '未检出'
          redCells.add(rowIdx + '_' + colIdxMap[item])
        } else if (plan.mode === 'single') {
          row[item] = plan.singleRaw || '未检出'
        } else {
          row[item] = '未检出'
        }
      }
      row['判定结果'] = hasFail ? '不合格' : '合格'
      rows.push(row)
    }

    const excelRows = rows.map((row) =>
      headers.map((h) => (row[h] === '' || row[h] === null || row[h] === undefined ? '-' : row[h])),
    )
    const fileName = `省例行水产品_${stamp}.xlsx`
    try {
      await createStyledExcel(headers, excelRows, '省例行水产品', fileName, {}, redCells)
    } catch (e) {
      return danger(`❌ 导出失败：${esc((e as Error).message)}`)
    }

    return ok(
      `✅ 导出完成！共 ${rows.length} 个样品，${PROVINCE_ROUTINE_AQUATIC_DRUGS.length} 种检测项目。<br>📁 文件：${fileName}`,
    )
  } catch (e) {
    return danger(`❌ 失败：${esc((e as Error).message)}`)
  }
}
