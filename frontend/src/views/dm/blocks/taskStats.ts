/**
 * 检测任务统计（原 `queryTaskStats`，行 1834~2032）。
 *
 * 逐字保留：合同号/任务号条件拼装（含 `A-B` 区间写法 → `BETWEEN`）、两段 SQL、
 * `getBigCategory` **第一套**规则（行 1896；注意与月报块行 2117 的第二套规则不同，**不可合并**）、
 * 按「任务名称 + 大类」分组的完成/检出/超标统计（含 `sampleId_projectName` 去重）、
 * `formatSampleProjects` 的「N批次样品中项目1和项目2」表述与 `；` 拼接、
 * 分组排序（任务名称 localeCompare → 农产品/畜产品/水产品/其他）、19 列表头与各项百分比 `toFixed(2)`。
 */
import { createStyledExcel, dmQuery } from '../helpers'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'

export interface TaskStatsParams {
  contractNo: string
  taskNo: string
  setProgress: (html: string) => void
}

interface SampleProjectInfo {
  sampleName: string
  projects: Set<string>
}

interface TaskGroup {
  taskNo: string
  taskName: string
  bigCategory: string
  sampleIds: Set<unknown>
  doneItems: number
  undoneItems: number
  detectItems: number
  exceedItems: number
  detectSampleIds: Set<unknown>
  exceedSampleIds: Set<unknown>
  doneItemSet?: Set<string>
  undoneItemSet?: Set<string>
  detectItemSet?: Set<string>
  exceedItemSet?: Set<string>
  detectSampleProjects?: Record<string, SampleProjectInfo>
  exceedSampleProjects?: Record<string, SampleProjectInfo>
}

/** 大类归类（行 1896 第一套规则，未与月报块的第二套合并） */
function getBigCategory(catName: string): string {
  if (!catName) return '其他'
  if (
    catName.includes('蔬菜') || catName.includes('豇豆') || catName.includes('芹菜') || catName.includes('辣椒') ||
    catName.includes('食用菌') || catName.includes('水果') || catName.includes('茶叶') ||
    catName.includes('谷物') || catName.includes('小麦')
  ) return '农产品'
  if (
    catName.includes('猪肉') || catName.includes('牛肉') || catName.includes('羊肉') || catName.includes('禽肉') ||
    catName.includes('禽蛋') || catName.includes('牛乳') || catName.includes('猪肝') || catName.includes('牛肝') ||
    catName.includes('羊肝') || catName.includes('鸡蛋') || catName.includes('鹌鹑蛋') || catName.includes('生鲜')
  ) return '畜产品'
  if (
    catName.includes('水产') || catName.includes('鲫鱼') || catName.includes('鳊鱼') || catName.includes('鲈鱼') ||
    catName.includes('乌鳢') || catName.includes('泥鳅') || catName.includes('牛蛙') || catName.includes('黄鳝') ||
    catName.includes('虾') || catName.includes('蟹') || catName.includes('鱼类')
  ) return '水产品'
  return catName
}

function formatSampleProjects(sampleProjects?: Record<string, SampleProjectInfo>): string {
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

export async function queryTaskStats(p: TaskStatsParams): Promise<DmOutcome> {
  const contractNo = p.contractNo.trim()
  const taskNo = p.taskNo.trim()
  if (!contractNo && !taskNo) {
    return danger('⚠️ 请输入合同号或任务编号')
  }

  let whereClause = 'd.IS_DELETED = 0'
  if (contractNo) whereClause += " AND d.CONTRACTS_NO LIKE '%" + contractNo.replace(/'/g, "''") + "%'"
  if (taskNo) {
    if (taskNo.includes('-')) {
      const parts = taskNo.split('-')
      const t1 = parts[0].trim().replace(/'/g, "''")
      const t2 = parts[1].trim().replace(/'/g, "''")
      whereClause += " AND d.TASK_NO BETWEEN '" + t1 + "' AND '" + t2 + "'"
    } else {
      whereClause += " AND d.TASK_NO LIKE '%" + taskNo.replace(/'/g, "''") + "%'"
    }
  }

  p.setProgress('⏳ 正在查询数据...')
  try {
    const detData = await dmQuery(
      'SELECT d.TASK_NO, d.TASK_NAME, d.CONTRACTS_NO, ' +
        's.ID AS SAMPLE_ID, s.NAME AS SAMPLE_NAME, s.SMALL_NO, ' +
        's.SAMPLE_NUM, s.SAMPLE_NUM_UNIT_NAME, ' +
        'sc.NAME AS CATEGORY_NAME ' +
        'FROM DETECTION.DT_DETECTION d ' +
        'LEFT JOIN DETECTION.DT_SAMPLE s ON s.DETECTION_NO = d.NO AND s.IS_DELETED = 0 ' +
        'LEFT JOIN DETECTION.DT_SAMPLE_CATEGORY sc ON sc.ID = s.SAMPLE_CATEGORY_ID AND sc.IS_DELETED = 0 ' +
        'WHERE ' + whereClause + ' ' +
        'ORDER BY d.NO, s.SMALL_NO',
    )
    if (!detData.success) return danger(`❌ 查询失败：${detData.error || ''}`)
    if (!detData.rows || detData.rows.length === 0) return warn('⚠️ 未找到数据')

    const rows = detData.rows
    const sampleIds = rows
      .map((r) => r[3])
      .filter(Boolean)
      .join(',')

    let projRows: unknown[][] = []
    let projOk = true
    if (sampleIds) {
      const projRes = await dmQuery(
        'SELECT sp.SAMPLE_ID, sp.ID AS PROJECT_ID, sp.DECIDE_PROJECT_NAME, ' +
          'sp.SINGLE_JUDGE, r.REPORT_VAL, r.STANDARD_VAL, ' +
          's.NAME AS SAMPLE_NAME, s.SMALL_NO ' +
          'FROM DETECTION.DT_SAMPLE_PROJECT sp ' +
          'LEFT JOIN DETECTION.DT_SAMPLE s ON s.ID = sp.SAMPLE_ID AND s.IS_DELETED = 0 ' +
          'LEFT JOIN DETECTION.DT_RESULT_CHECK_IN r ON r.SAMPLE_PROJECT_ID = sp.ID AND r.IS_DELETED = 0 ' +
          'WHERE sp.SAMPLE_ID IN (' + sampleIds + ") AND sp.IS_DELETED = 0 AND NVL(sp.IS_LOGOUT, 'NO') <> 'YES' " +
          'ORDER BY sp.SAMPLE_ID, sp.SEQ_NO',
      )
      projOk = !!projRes.success
      projRows = projRes.rows || []
    }

    const groupMap: Record<string, TaskGroup> = {}
    for (const r of rows) {
      const taskNoValue = r[0] as string
      const taskName = String(r[1] || '')
      const sampleId = r[3]
      if (!sampleId) continue
      const bigCategory = getBigCategory(String(r[8] || ''))
      const key = taskName + '||' + bigCategory
      if (!groupMap[key]) {
        groupMap[key] = {
          taskNo: taskNoValue,
          taskName,
          bigCategory,
          sampleIds: new Set(),
          doneItems: 0,
          undoneItems: 0,
          detectItems: 0,
          exceedItems: 0,
          detectSampleIds: new Set(),
          exceedSampleIds: new Set(),
        }
      }
      groupMap[key].sampleIds.add(sampleId)
    }

    if (projOk && projRows.length) {
      for (const pr of projRows) {
        const sampleId = pr[0]
        const projectName = String(pr[2] || '')
        const judge = String(pr[3] || '')
        const reportVal = pr[4]
        const sampleName = String(pr[6] || '')
        const hasResult = reportVal !== null && reportVal !== undefined && reportVal !== ''
        const isDetected =
          hasResult && String(reportVal).indexOf('未检出') === -1 && String(reportVal).indexOf('阴性') === -1
        const isExceed = !!judge && (judge.includes('不合格') || judge === '不符合')
        for (const grp of Object.values(groupMap)) {
          if (grp.sampleIds.has(sampleId)) {
            if (hasResult) {
              const doneKey = String(sampleId) + '_' + projectName
              if (!grp.doneItemSet) grp.doneItemSet = new Set()
              if (!grp.doneItemSet.has(doneKey)) {
                grp.doneItemSet.add(doneKey)
                grp.doneItems += 1
              }
            } else {
              const undoneKey = String(sampleId) + '_' + projectName
              if (!grp.undoneItemSet) grp.undoneItemSet = new Set()
              if (!grp.undoneItemSet.has(undoneKey)) {
                grp.undoneItemSet.add(undoneKey)
                grp.undoneItems += 1
              }
            }
            if (isDetected) {
              const detectKey = String(sampleId) + '_' + projectName
              if (!grp.detectItemSet) grp.detectItemSet = new Set()
              if (!grp.detectItemSet.has(detectKey)) {
                grp.detectItemSet.add(detectKey)
                grp.detectItems += 1
              }
              grp.detectSampleIds.add(sampleId)
              if (!grp.detectSampleProjects) grp.detectSampleProjects = {}
              if (!grp.detectSampleProjects[String(sampleId)]) {
                grp.detectSampleProjects[String(sampleId)] = { sampleName, projects: new Set() }
              }
              if (projectName) grp.detectSampleProjects[String(sampleId)].projects.add(projectName)
            }
            if (isExceed) {
              const exceedKey = String(sampleId) + '_' + projectName
              if (!grp.exceedItemSet) grp.exceedItemSet = new Set()
              if (!grp.exceedItemSet.has(exceedKey)) {
                grp.exceedItemSet.add(exceedKey)
                grp.exceedItems += 1
              }
              grp.exceedSampleIds.add(sampleId)
              if (!grp.exceedSampleProjects) grp.exceedSampleProjects = {}
              if (!grp.exceedSampleProjects[String(sampleId)]) {
                grp.exceedSampleProjects[String(sampleId)] = { sampleName, projects: new Set() }
              }
              if (projectName) grp.exceedSampleProjects[String(sampleId)].projects.add(projectName)
            }
            break
          }
        }
      }
    }

    const bigCatOrder = ['农产品', '畜产品', '水产品', '其他']
    const groupList = Object.values(groupMap).sort((a, b) => {
      if (a.taskName !== b.taskName) return a.taskName.localeCompare(b.taskName)
      return bigCatOrder.indexOf(a.bigCategory) - bigCatOrder.indexOf(b.bigCategory)
    })

    const headers = [
      '序号', '任务编号', '任务名称', '产品类别', '接收样品量',
      '已完成-数量', '已完成-项次', '未完成-数量', '未完成-项次',
      '检出-数量', '检出-项次', '检出项次名称', '样品检出率', '项次检出率',
      '超标-数量', '超标-项次', '超标项次名称', '样品超标率%', '项次超标率%',
    ]

    const excelRows: (string | number)[][] = []
    let seq = 1
    for (const grp of groupList) {
      const totalItems = grp.doneItems + grp.undoneItems
      const totalSafe = totalItems || 1
      const totalSamples = grp.sampleIds.size || 1
      const dCnt = grp.detectSampleIds.size
      const eCnt = grp.exceedSampleIds.size
      const dItems = grp.detectItems || 0
      const eItems = grp.exceedItems || 0
      excelRows.push([
        seq++, grp.taskNo, grp.taskName, grp.bigCategory,
        grp.sampleIds.size, grp.sampleIds.size, grp.doneItems, 0, grp.undoneItems,
        dCnt, dItems, formatSampleProjects(grp.detectSampleProjects),
        ((dCnt / totalSamples) * 100).toFixed(2) + '%',
        ((dItems / totalSafe) * 100).toFixed(2) + '%',
        eCnt, eItems, formatSampleProjects(grp.exceedSampleProjects),
        ((eCnt / totalSamples) * 100).toFixed(2) + '%',
        ((eItems / totalSafe) * 100).toFixed(2) + '%',
      ])
    }

    const fileName = `检测任务统计_${contractNo || taskNo || ''}.xlsx`
    try {
      await createStyledExcel(headers, excelRows, '检测任务统计', fileName, {})
    } catch (e) {
      return danger(`❌ 导出失败：${esc((e as Error).message)}`)
    }
    return ok(`✅ 导出完成！共 ${excelRows.length} 行。<br>📁 文件：${fileName}`)
  } catch (e) {
    return danger(`❌ 失败：${esc((e as Error).message)}`)
  }
}
