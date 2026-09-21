/**
 * 省例行畜产品数据汇总（畜禽产品例行监测，原 `queryProvinceRoutineLivestock`，行 2561~2775）。
 *
 * 逐字保留：
 * - det SQL **多取一列** `d.PRODUCTION_COMPANY_NAME`（产地），列序与农产品块不同；
 * - **38 个固定兽药列**（`PROVINCE_ROUTINE_LIVESTOCK_DRUGS`）+ 9 个属性列 + 1 个结果判定列；
 * - `mapMonitorLink`：监测环节归为 养殖/屠宰/市场/运输车/奶站 五种，
 *   其中「生产基地/企业」按**样品名**区分 —— 猪牛羊等 → 屠宰环节，其余 → 养殖环节；
 * - 规范化优先 `PROVINCE_ROUTINE_LIVESTOCK_MAP`（含覆盖 PESTICIDE_NAME_MAP 的「碱类物质」），其次 `PESTICIDE_NAME_MAP`；
 * - 匹配三级规则，但第 1 级额外支持**去括号基础名**相等（`x.replace(/（.*）/g,'')` / `\(.*\)`）；
 * - 单元格取值**与农产品块不同**：重复记录分行列出**原始值**（不做有效数字格式化）、
 *   氟苯尼考列做**直接加和**（不带折算系数、不格式化）、其余取「首个非空值」，
 *   无值时一律写 `未检出`（不是 `未检出(类型:值)`）；
 * - 空值导出填 `-`；样品按抽样编号 `localeCompare(..., 'zh-CN', {numeric:true})` 排序。
 *
 * 新增「按合同号导出」：传入 `contractNo` 时，筛选条件由 `d.TASK_NO = 'x'` 改为
 * `buildContractCond()`（`d.CONTRACTS_NO LIKE`，支持逗号/分号分隔多值），其余取数、
 * 列定义、加和规则、判定口径与文件名规则完全不变。
 * 新增「多任务编号输入」：任务编号走 `resolveTaskCond`（多个/免输 RW 前缀，IN 匹配）。
 */
import { buildContractCond, createStyledExcel, dmQuery, PESTICIDE_NAME_MAP, type CellValue } from '../helpers'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'
import { resolveTaskCond, taskFileStamp, taskListLabel } from './taskResolve'
import { parseCityCounty } from './provinceCommon'

const PROVINCE_ROUTINE_LIVESTOCK_DRUGS = [
  '三聚氰胺（mg/kg)', '达氟沙星', '磺胺二甲氧嘧啶', '磺胺间甲氧嘧啶', '非诺特罗', '甲砜霉素', '沙丁胺醇', '特布他林', '克仑特罗', '倍他米松',
  '氯丙那林', '喷布特罗', '妥布特罗', '地塞米松', '西马特罗', '氯霉素', '多西环素', '金霉素', '磺胺二甲嘧啶', '磺胺喹噁啉',
  '四环素', '培氟沙星', '诺氟沙星', '磺胺甲噁唑', '氟苯尼考（氟苯尼考+氟苯尼考胺）', '甲氧苄啶', '金刚烷胺', '土霉素', '氧氟沙星', '环丙沙星',
  '莱克多巴胺', 'β-内酰胺酶(单位：U/ml)', '恩诺沙星', '洛美沙星', '沙拉沙星', '碱类物质', '甲硝唑', '地美硝唑',
]

/** 合并列映射：模板列名 -> 数据库多个实际检测项目名 */
const PROVINCE_ROUTINE_LIVESTOCK_GROUPS: Record<string, string[]> = {
  '氟苯尼考（氟苯尼考+氟苯尼考胺）': ['氟苯尼考', '氟苯尼考胺'],
  'β-内酰胺酶(单位：U/ml)': ['β-内酰胺酶'],
}

/** 兽药名规范化映射：数据库项目名 -> 模板列名（含异名/带括号说明） */
const PROVINCE_ROUTINE_LIVESTOCK_MAP: Record<string, string> = {
  强力霉素: '多西环素',
  磺胺间二甲氧嘧啶: '磺胺二甲氧嘧啶',
  '磺胺二甲氧嘧啶（磺胺间二甲氧嘧啶、磺胺二甲氧哒嗪）': '磺胺二甲氧嘧啶',
  '磺胺间甲氧嘧啶（磺胺-6-甲氧嘧啶）': '磺胺间甲氧嘧啶',
  '磺胺甲基异噁唑（磺胺甲噁唑）': '磺胺甲噁唑',
  克伦特罗: '克仑特罗',
  '呋喃唑酮代谢物[AOZ]': '呋喃唑酮代谢物',
  二甲硝咪唑: '地美硝唑',
  '二甲硝咪唑（地美硝唑）': '地美硝唑',
  '羟基二甲硝咪唑（羟基地美硝唑）': '羟基地美硝唑',
  // 覆盖 PESTICIDE_NAME_MAP 中"碱类物质→碱性物质"的错误映射，保证模板列"碱类物质"能匹配
  碱类物质: '碱类物质',
}

/** 监测环节归类：仅显示 养殖/屠宰/市场/运输车/奶站 五种 */
function mapMonitorLink(val: unknown, sampleName: unknown): string {
  if (!val) return ''
  const v = String(val)
  if (v.indexOf('屠宰') >= 0) return '屠宰环节'
  if (v.indexOf('运输') >= 0) return '运输车'
  if (v.indexOf('奶站') >= 0 || v.indexOf('生鲜乳') >= 0) return '奶站'
  if (v.indexOf('市场') >= 0 || v.indexOf('超市') >= 0) return '市场环节'
  // "生产基地/企业"：按样品名区分畜/禽
  if (v.indexOf('基地') >= 0 || v.indexOf('企业') >= 0) {
    const n = String(sampleName || '')
    const isLivestock = /猪|牛|羊|生鲜牛乳|牛奶/.test(n) // 畜产品
    if (isLivestock) return '屠宰环节'
    return '养殖环节'
  }
  if (
    v.indexOf('养殖') >= 0 || v.indexOf('场') >= 0 ||
    v.indexOf('塘') >= 0 || v.indexOf('家庭农场') >= 0 || v.indexOf('散户') >= 0 ||
    v.indexOf('合作社') >= 0 || v.indexOf('农户') >= 0 || v.indexOf('田块') >= 0 || v.indexOf('鱼塘') >= 0
  ) return '养殖环节'
  return v // 无法归类的保留原值
}

interface LsSubValue {
  dn: string
  v: string
  judge: string
}

export interface ProvinceLivestockParams {
  exactTaskNo?: string
  inputValue: string
  /** 合同号（多个用逗号/分号分隔，模糊匹配）；填写后按合同号导出，忽略任务编号 */
  contractNo?: string
  setProgress: (html: string) => void
}

export async function queryProvinceRoutineLivestock(p: ProvinceLivestockParams): Promise<DmOutcome> {
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
        'd.PRODUCTION_COMPANY_NAME, d.SAMPLING_DATE, d.ACCEPT_ORG_NAME, s.ORIGINAL_NO ' +
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

    interface LsSampleInfo {
      sampleNo: string
      sampleName: string
      monitorLink: string
      detectedCompany: string
      address: string
      origin: string
    }

    const sampleInfo: Record<string, LsSampleInfo> = {}
    for (const s of samples) {
      sampleInfo[String(s[0])] = {
        sampleNo: String(s[11] || s[1] || ''), // 原始编号 s.ORIGINAL_NO（抽样编号）
        sampleName: String(s[2] || ''),
        monitorLink: mapMonitorLink(s[3], s[2]), // s.SAMPLING_POSITION（监测环节）
        detectedCompany: String(s[6] || ''),
        address: String(s[7] || ''), // 受检单位所在地
        origin: String(s[8] || ''), // 生产单位 d.PRODUCTION_COMPANY_NAME（产地）
      }
    }

    // 汇总每个样品的检测结果（含合并列加和）
    const resultsBySample: Record<string, Record<string, { subValues: LsSubValue[] }>> = {}
    if (projData.success && projData.rows) {
      for (const r of projData.rows) {
        const sid = String(r[0])
        const pn = r[1] as string
        if (!pn) continue
        let dn = pn
        // 优先用兽药名规范化映射（异名/带括号说明 → 模板列名）
        if (PROVINCE_ROUTINE_LIVESTOCK_MAP[pn]) {
          dn = PROVINCE_ROUTINE_LIVESTOCK_MAP[pn]
        } else if (PESTICIDE_NAME_MAP[pn]) {
          dn = PESTICIDE_NAME_MAP[pn]
        }
        if (!resultsBySample[sid]) resultsBySample[sid] = {}

        // 匹配规则：仅"标题包含项目名"才构成包含关系（不做反向包含匹配）
        // 1) 单项目精确匹配（含去括号基础名）
        let colName = PROVINCE_ROUTINE_LIVESTOCK_DRUGS.find((x) => {
          const base = x.replace(/（.*）/g, '').replace(/\(.*\)/g, '')
          return x === dn || base === dn
        })
        // 2) 列名包含项目名（标题含项目名）
        if (!colName) {
          colName = PROVINCE_ROUTINE_LIVESTOCK_DRUGS.find((x) => x.indexOf(dn) >= 0)
        }
        // 3) 合并组：子项目精确匹配
        if (!colName) {
          for (const [col, subs] of Object.entries(PROVINCE_ROUTINE_LIVESTOCK_GROUPS)) {
            if (subs.includes(dn)) {
              colName = col
              break
            }
          }
        }
        if (!colName) continue

        if (!resultsBySample[sid][colName]) resultsBySample[sid][colName] = { subValues: [] }
        const v = r[3] !== null && r[3] !== undefined && String(r[3]).trim() !== '' ? String(r[3]) : ''
        resultsBySample[sid][colName].subValues.push({ dn, v, judge: String(r[4] || '') })
      }
    }

    const headers = ['序号', '检测单位', '被检市县', '抽样编号', '样品名称', '受检单位名称', '监测环节', '受检单位所在地', '样品标示来源地(产地)']
    for (const item of PROVINCE_ROUTINE_LIVESTOCK_DRUGS) headers.push(item)
    headers.push('结果判定(合格或不合格)')

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
      row['抽样编号'] = info.sampleNo
      row['样品名称'] = info.sampleName
      row['受检单位名称'] = info.detectedCompany
      row['监测环节'] = info.monitorLink
      row['受检单位所在地'] = info.address
      row['样品标示来源地(产地)'] = info.origin

      const rowIdx = rows.length
      const results = resultsBySample[sid] || {}
      let hasFail = false
      for (const item of PROVINCE_ROUTINE_LIVESTOCK_DRUGS) {
        const res = results[item]
        if (!res) continue
        const subs = res.subValues || []
        if (subs.length === 0) continue
        const bySub: Record<string, LsSubValue[]> = {}
        for (const x of subs) {
          const k = x.dn || ''
          ;(bySub[k] = bySub[k] || []).push(x)
        }
        const hasDup = Object.values(bySub).some((arr) => arr.length > 1)
        if (hasDup) {
          // 同一项目出现多条有效记录：分行全部列出并标红
          const lines: string[] = []
          for (const x of subs) {
            if (x.v === '' || x.v === '未检出') continue
            lines.push(String(x.v))
            if (x.judge && (x.judge.includes('不合格') || x.judge === '不符合')) hasFail = true
          }
          row[item] = lines.length ? lines.join('\n') : '未检出'
          redCells.add(rowIdx + '_' + colIdxMap[item])
        } else if (item.indexOf('氟苯尼考') >= 0) {
          // 氟苯尼考：氟苯尼考 + 氟苯尼考胺 加和
          let sum = 0
          let any = false
          let fail = false
          for (const x of subs) {
            if (x.v === '' || x.v === '未检出') continue
            const num = parseFloat(x.v)
            if (!isNaN(num)) {
              sum += num
              any = true
            }
            if (x.judge && (x.judge.includes('不合格') || x.judge === '不符合')) fail = true
          }
          if (any) {
            row[item] = sum
          } else {
            row[item] = '未检出'
          }
          if (fail) hasFail = true
        } else {
          // 其它：填检出的值
          const detected = subs.find((x) => x.v !== '')
          row[item] = detected ? detected.v : '未检出'
          if (detected && detected.judge && (detected.judge.includes('不合格') || detected.judge === '不符合')) {
            hasFail = true
          }
        }
      }
      row['结果判定(合格或不合格)'] = hasFail ? '不合格' : '合格'
      rows.push(row)
    }

    const excelRows = rows.map((row) =>
      headers.map((h) => (row[h] === '' || row[h] === null || row[h] === undefined ? '-' : row[h])),
    )
    const fileName = `省例行畜产品_${stamp}.xlsx`
    try {
      await createStyledExcel(headers, excelRows, '省例行畜产品', fileName, {}, redCells)
    } catch (e) {
      return danger(`❌ 导出失败：${esc((e as Error).message)}`)
    }

    return ok(
      `✅ 导出完成！共 ${rows.length} 个样品，${PROVINCE_ROUTINE_LIVESTOCK_DRUGS.length} 种检测项目。<br>📁 文件：${fileName}`,
    )
  } catch (e) {
    return danger(`❌ 失败：${esc((e as Error).message)}`)
  }
}
