/**
 * 检测结果月报（原 `queryMonthlyReport`，行 2037~2262）。
 *
 * 逐字保留：合同号/任务号多值拆分（兼容中文逗号、任务号自动去 `RW` 前缀）、
 * 受理日期区间的 `TO_DATE(...)` 与 `< ... + 1` 写法、两段 SQL、
 * `getBigCategory` **第二套**规则（行 2117；与任务统计块行 1896 第一套规则不同，**不可合并**）、
 * 按「任务编号 + 大类」分组统计、「样品+项目」去重的检出计数、
 * `formatDetectDrugs` 的「项目N次」按次数降序、`formatSampleProjects`（与模板5同款）、
 * 排序（任务编号 localeCompare → 大类顺序）、10 列表头、以及末尾**合计行**。
 */
import { createStyledExcel, dmQuery } from '../helpers'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'

export interface MonthlyReportParams {
  contractNo: string
  taskNo: string
  dateFrom: string
  dateTo: string
  setProgress: (html: string) => void
}

interface MonthlyGroup {
  taskNo: string
  taskName: string
  bigCategory: string
  sampleIds: Set<unknown>
  detectSampleIds: Set<unknown>
  exceedSampleIds: Set<unknown>
  detectDrugs: Record<string, number>
  exceedSampleProjects: Record<string, { sampleName: string; projects: Set<string> }>
  detectDrugSet?: Set<string>
}

/** 大类归类（行 2117 第二套规则，未与任务统计块的第一套合并） */
function getBigCategory(catName: string): string {
  if (!catName) return '其他'
  const c = catName
  if (c === '农产品' || c === '畜产品' || c === '水产品') return c
  if (c.includes('农产品')) return '农产品'
  if (c.includes('畜产品')) return '畜产品'
  if (c.includes('水产品')) return '水产品'
  if (
    c.includes('蔬菜') || c.includes('水果') || c.includes('茶叶') || c.includes('谷物') ||
    c.includes('食用菌') || c.includes('小麦') || c.includes('稻谷') || c.includes('蔬果')
  ) return '农产品'
  if (
    c.includes('豇豆') || c.includes('芹菜') || c.includes('辣椒') || c.includes('甜瓜') ||
    c.includes('普通白菜') || c.includes('草莓') || c.includes('蕹菜')
  ) return '农产品'
  if (
    c.includes('猪肉') || c.includes('牛肉') || c.includes('羊肉') || c.includes('牛羊肉') ||
    c.includes('禽肉') || c.includes('禽蛋') || c.includes('生鲜') || c.includes('牛乳') ||
    c.includes('猪肝') || c.includes('牛肝') || c.includes('羊肝') || c.includes('鸡蛋') ||
    c.includes('鹌鹑蛋') || c.includes('尿液') || c.includes('肉类')
  ) return '畜产品'
  if (
    c.includes('水产') || c.includes('鱼类') || c.includes('虾蟹') || c.includes('虾') ||
    c.includes('蟹') || c.includes('鲫鱼') || c.includes('鳊鱼') || c.includes('鲈鱼') ||
    c.includes('乌鳢') || c.includes('泥鳅') || c.includes('牛蛙') || c.includes('黄鳝') || c.includes('鳙鱼')
  ) return '水产品'
  return '其他'
}

/** 格式化超标项次名称（与模板5一致） */
function formatSampleProjects(
  sampleProjects?: Record<string, { sampleName: string; projects: Set<string> }>,
): string {
  if (!sampleProjects) return ''
  const countMap: Record<string, number> = {}
  for (const info of Object.values(sampleProjects)) {
    const sname = info.sampleName || ''
    const projectList = Array.from(info.projects).sort()
    if (projectList.length === 0) continue
    const projectStr = projectList.join('和')
    const key = sname + '||' + projectStr
    countMap[key] = (countMap[key] || 0) + 1
  }
  const items: string[] = []
  for (const [key, count] of Object.entries(countMap)) {
    const [sname, pstr] = key.split('||')
    items.push(count + '批次' + sname + '中' + pstr)
  }
  return items.join('；')
}

/** 格式化检出项目及数量（多到少） */
function formatDetectDrugs(detectDrugs: Record<string, number>): string {
  const sorted = Object.entries(detectDrugs).sort((a, b) => b[1] - a[1])
  return sorted.map(([d, c]) => d + c + '次').join('、')
}

export async function queryMonthlyReport(p: MonthlyReportParams): Promise<DmOutcome> {
  const contractNo = p.contractNo.trim()
  const taskNo = p.taskNo.trim()
  const dateFrom = p.dateFrom
  const dateTo = p.dateTo

  if (!contractNo && !taskNo && !dateFrom && !dateTo) {
    return danger('⚠️ 请至少输入合同号、任务编号或选择受理日期区间')
  }

  let whereClause = 'd.IS_DELETED = 0'
  if (contractNo) {
    const contracts = contractNo
      .replace(/，/g, ',')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
    if (contracts.length === 1) {
      whereClause += " AND d.CONTRACTS_NO LIKE '%" + contracts[0].replace(/'/g, "''") + "%'"
    } else {
      const quoted = contracts
        .map((c) => "d.CONTRACTS_NO LIKE '%" + c.replace(/'/g, "''") + "%'")
        .join(' OR ')
      whereClause += ' AND (' + quoted + ')'
    }
  }
  if (taskNo) {
    const tasks = taskNo
      .replace(/，/g, ',')
      .split(',')
      .map((s) => s.trim().replace(/^RW/i, ''))
      .filter(Boolean)
    if (tasks.length === 1) {
      whereClause += " AND d.TASK_NO LIKE '%RW" + tasks[0].replace(/'/g, "''") + "%'"
    } else {
      const quoted = tasks.map((t) => "d.TASK_NO LIKE '%RW" + t.replace(/'/g, "''") + "%'").join(' OR ')
      whereClause += ' AND (' + quoted + ')'
    }
  }
  if (dateFrom) {
    whereClause += " AND d.SAMPLING_DATE >= TO_DATE('" + dateFrom + "', 'YYYY-MM-DD')"
  }
  if (dateTo) {
    whereClause += " AND d.SAMPLING_DATE < TO_DATE('" + dateTo + "', 'YYYY-MM-DD') + 1"
  }

  p.setProgress('⏳ 正在查询数据...')
  try {
    const detData = await dmQuery(
      'SELECT d.TASK_NO, d.TASK_NAME, d.CONTRACTS_NO, d.SAMPLING_DATE, d.EVALUATE_RESULT, ' +
        's.ID AS SAMPLE_ID, s.NAME AS SAMPLE_NAME, s.SMALL_NO, ' +
        'sc.NAME AS CATEGORY_NAME, NVL(sc.FULL_VALUE_PATH, sc.NAME) AS CATEGORY_PATH ' +
        'FROM DETECTION.DT_DETECTION d ' +
        'LEFT JOIN DETECTION.DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 ' +
        'LEFT JOIN DETECTION.DT_SAMPLE_CATEGORY sc ON sc.ID = s.SAMPLE_CATEGORY_ID AND sc.IS_DELETED = 0 ' +
        'WHERE ' + whereClause + ' ' +
        'ORDER BY d.TASK_NO, s.SMALL_NO',
    )
    if (!detData.success) return danger(`❌ 查询失败：${detData.error || ''}`)
    if (!detData.rows || detData.rows.length === 0) return warn('⚠️ 未找到数据')

    const rows = detData.rows
    const sampleIds = rows
      .map((r) => r[5])
      .filter(Boolean)
      .join(',')

    let projRows: unknown[][] = []
    let projOk = true
    if (sampleIds) {
      const projRes = await dmQuery(
        'SELECT sp.SAMPLE_ID, sp.DECIDE_PROJECT_NAME, ' +
          'r.REPORT_VAL, sp.SINGLE_JUDGE, s.NAME AS SAMPLE_NAME ' +
          'FROM DETECTION.DT_SAMPLE_PROJECT sp ' +
          'LEFT JOIN DETECTION.DT_SAMPLE s ON s.ID = sp.SAMPLE_ID AND s.IS_DELETED = 0 ' +
          'LEFT JOIN DETECTION.DT_RESULT_CHECK_IN r ON r.SAMPLE_PROJECT_ID = sp.ID AND r.IS_DELETED = 0 ' +
          'WHERE sp.SAMPLE_ID IN (' + sampleIds + ") AND sp.IS_DELETED = 0 AND NVL(sp.IS_LOGOUT, 'NO') <> 'YES' " +
          'ORDER BY sp.SAMPLE_ID, sp.SEQ_NO',
      )
      projOk = !!projRes.success
      projRows = projRes.rows || []
    }

    const groupMap: Record<string, MonthlyGroup> = {}
    for (const r of rows) {
      const taskNoValue = r[0] as string
      const taskName = String(r[1] || '')
      const sampleId = r[5]
      if (!sampleId) continue
      // 优先使用 CATEGORY_PATH（含"产品-农产品"路径），其次 NAME
      const bigCategory = getBigCategory(String(r[9] || r[8] || ''))
      const key = String(taskNoValue) + '||' + bigCategory
      if (!groupMap[key]) {
        groupMap[key] = {
          taskNo: taskNoValue,
          taskName,
          bigCategory,
          sampleIds: new Set(),
          detectSampleIds: new Set(),
          exceedSampleIds: new Set(),
          detectDrugs: {},
          exceedSampleProjects: {},
        }
      }
      groupMap[key].sampleIds.add(sampleId)
    }

    if (projOk && projRows.length) {
      for (const pr of projRows) {
        const sampleId = pr[0]
        const projectName = String(pr[1] || '')
        const reportVal = pr[2]
        const judge = String(pr[3] || '')
        const sampleName = String(pr[4] || '')
        const hasResult = reportVal !== null && reportVal !== undefined && reportVal !== ''
        const isDetected =
          hasResult && String(reportVal).indexOf('未检出') === -1 && String(reportVal).indexOf('阴性') === -1
        const isExceed = !!judge && (judge.includes('不合格') || judge === '不符合')
        for (const grp of Object.values(groupMap)) {
          if (grp.sampleIds.has(sampleId)) {
            if (isDetected) {
              grp.detectSampleIds.add(sampleId)
              if (projectName) {
                // 同一"样品+项目"只计一次（重复记录不重复计数）
                const detectKey = String(sampleId) + '_' + projectName
                if (!grp.detectDrugSet) grp.detectDrugSet = new Set()
                if (!grp.detectDrugSet.has(detectKey)) {
                  grp.detectDrugSet.add(detectKey)
                  grp.detectDrugs[projectName] = (grp.detectDrugs[projectName] || 0) + 1
                }
              }
            }
            if (isExceed) {
              grp.exceedSampleIds.add(sampleId)
              if (projectName) {
                if (!grp.exceedSampleProjects[String(sampleId)]) {
                  grp.exceedSampleProjects[String(sampleId)] = { sampleName, projects: new Set() }
                }
                grp.exceedSampleProjects[String(sampleId)].projects.add(projectName)
              }
            }
            break
          }
        }
      }
    }

    const bigCatOrder = ['农产品', '畜产品', '水产品', '其他']
    const groupList = Object.values(groupMap).sort((a, b) => {
      if (a.taskNo !== b.taskNo) return (a.taskNo || '').localeCompare(b.taskNo || '')
      return bigCatOrder.indexOf(a.bigCategory) - bigCatOrder.indexOf(b.bigCategory)
    })

    const headers = ['序号', '任务编号', '任务名称', '产品类别', '检测数量', '检出数量', '检出率', '检出项目及数量', '超标项次名称', '合格率']

    const excelRows: (string | number)[][] = []
    let seq = 1
    for (const grp of groupList) {
      const total = grp.sampleIds.size
      const detectCount = grp.detectSampleIds.size
      const exceedCount = grp.exceedSampleIds.size
      const detectRate = total > 0 ? ((detectCount / total) * 100).toFixed(1) : '0.0'
      const qualifiedRate = total > 0 ? (((total - exceedCount) / total) * 100).toFixed(1) : '100.0'
      const exceedStr = formatSampleProjects(grp.exceedSampleProjects)
      excelRows.push([
        seq++,
        grp.taskNo,
        grp.taskName,
        grp.bigCategory,
        total,
        detectCount,
        detectRate + '%',
        formatDetectDrugs(grp.detectDrugs),
        exceedStr,
        qualifiedRate + '%',
      ])
    }

    // 合计行
    const totalDetect = groupList.reduce((s, g) => s + g.sampleIds.size, 0)
    const totalDetected = groupList.reduce((s, g) => s + g.detectSampleIds.size, 0)
    const totalExceed = groupList.reduce((s, g) => s + g.exceedSampleIds.size, 0)
    const totalDetectRate = totalDetect > 0 ? ((totalDetected / totalDetect) * 100).toFixed(1) + '%' : '0.0%'
    const totalQualifiedRate =
      totalDetect > 0 ? (((totalDetect - totalExceed) / totalDetect) * 100).toFixed(1) + '%' : '100.0%'
    excelRows.push(['', '合计', '', '', totalDetect, totalDetected, totalDetectRate, '', '', totalQualifiedRate])

    const fileName = `检测结果月报_${contractNo || taskNo || dateFrom || '月报'}.xlsx`
    try {
      await createStyledExcel(headers, excelRows, '检测结果月报', fileName, {})
    } catch (e) {
      return danger(`❌ 导出失败：${esc((e as Error).message)}`)
    }
    return ok(`✅ 导出完成！共 ${excelRows.length} 行。<br>📁 文件：${fileName}`)
  } catch (e) {
    return danger(`❌ 失败：${esc((e as Error).message)}`)
  }
}
