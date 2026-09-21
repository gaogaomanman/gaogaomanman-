/**
 * 任务编号解析（原 `resolveTaskNo` + `renderTaskList`）。
 *
 * 原逻辑：无精确编号时按关键词模糊搜索；0 条→提示，1 条→直接使用，多条→渲染可点击编号列表。
 * 迁移后：多条的情况返回结构化 taskList，由 Vue 模板渲染芯片。
 */
import { dmQuery, fuzzySearchTasks } from '../helpers'
import { danger, esc, warn, type DmOutcome } from '../outcome'

export interface ResolveTaskResult {
  /** 解析出的唯一任务编号；为空表示未解析成功 */
  taskNo?: string
  /** 需要展示给用户的提示 / 候选列表 */
  outcome?: DmOutcome
}

export async function resolveTaskNo(
  exactTaskNo: string | undefined,
  inputValue: string,
  setProgress: (html: string) => void,
): Promise<ResolveTaskResult> {
  if (exactTaskNo) return { taskNo: exactTaskNo }
  const kw = inputValue.trim()
  if (!kw) {
    return { outcome: danger('⚠️ 请输入任务编号') }
  }
  setProgress('⏳ 正在搜索匹配的任务...')
  const sr = await fuzzySearchTasks(kw)
  if (sr.error) {
    return { outcome: danger(`❌ 搜索失败：${esc(sr.error)}<br>请确认数据库已连接后再试`) }
  }
  const tasks = sr.tasks || []
  if (tasks.length === 0) {
    return { outcome: warn(`⚠️ 未找到包含「${esc(kw)}」的任务编号`) }
  }
  if (tasks.length === 1) return { taskNo: tasks[0] }
  return { outcome: { type: 'taskList', intro: `🔍 找到 ${tasks.length} 个匹配任务，点击编号导出：`, tasks } }
}

export interface ResolveTasksResult {
  /** 命中的任务编号（按库中真实值返回，已去重排序） */
  tasks: string[]
  /** SQL 条件片段（含前导 AND，已转义），如 " AND d.TASK_NO IN ('RW2026049',...)" */
  cond: string
  /** 需要直接展示给用户的结果（输入为空 / 未找到 / 匹配过多 / 单编号多候选芯片） */
  outcome?: DmOutcome
}

/** 最多一次导出的任务数（超过视为误输入，提示补全编号） */
const MAX_TASKS = 100

/**
 * 任务编号解析（多编号 + 免输 RW 前缀版本，供支持多个任务的功能块使用）。
 *
 * - 多个编号用逗号/分号/空白分隔；
 * - 纯数字（或小写 rw 前缀）自动补全为 RW 前缀："2026049" / "rw2026049" → "RW2026049"；
 * - 每个编号按 LIKE 模糊匹配库中真实任务号，命中结果合并去重；
 * - 单个编号匹配到多个任务时返回 taskList 芯片（与原单编号行为一致，点击后走精确编号）；
 * - 命中超过 100 个视为关键词过短，提示补全。
 */
export async function resolveTaskCond(
  exactTaskNo: string | undefined,
  inputValue: string,
  setProgress: (html: string) => void,
): Promise<ResolveTasksResult> {
  if (exactTaskNo) {
    return { tasks: [exactTaskNo], cond: " AND d.TASK_NO = '" + exactTaskNo.replace(/'/g, "''") + "'" }
  }
  const tokens = String(inputValue || '')
    .split(/[,，;；\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (!tokens.length) {
    return { tasks: [], cond: '', outcome: danger('⚠️ 请输入任务编号') }
  }
  // 归一化：去掉 RW 前缀后统一补 RW（大小写均可，兼容只输数字）
  const kws = Array.from(new Set(tokens.map((t) => 'RW' + t.replace(/^RW/i, ''))))
  setProgress(`⏳ 正在搜索 ${kws.length} 个任务编号...`)
  const where = kws.map((k) => "TASK_NO LIKE '%" + k.replace(/'/g, "''") + "%'").join(' OR ')
  const d = await dmQuery(
    'SELECT DISTINCT TASK_NO FROM DETECTION.DT_DETECTION ' +
      'WHERE IS_DELETED = 0 AND TASK_NO IS NOT NULL AND (' + where + ') ORDER BY TASK_NO',
  )
  if (!d.success) {
    return { tasks: [], cond: '', outcome: danger(`❌ 搜索失败：${esc(d.error)}<br>请确认数据库已连接后再试`) }
  }
  const tasks = (d.rows || []).map((r) => String(r[0])).filter(Boolean)
  if (!tasks.length) {
    return { tasks: [], cond: '', outcome: warn(`⚠️ 未找到包含「${esc(kws.join('、'))}」的任务编号`) }
  }
  if (tasks.length > MAX_TASKS) {
    return {
      tasks: [],
      cond: '',
      outcome: warn(`⚠️ 关键词匹配到 ${tasks.length} 个任务，数量过多，请输入更完整的任务编号`),
    }
  }
  if (kws.length === 1 && tasks.length > 1) {
    return {
      tasks: [],
      cond: '',
      outcome: { type: 'taskList', intro: `🔍 找到 ${tasks.length} 个匹配任务，点击编号导出：`, tasks },
    }
  }
  const cond = ' AND d.TASK_NO IN (' + tasks.map((t) => "'" + t.replace(/'/g, "''") + "'").join(',') + ')'
  return { tasks, cond }
}

/** 多任务时的展示名（提示消息里用，最多列 10 个） */
export function taskListLabel(tasks: string[]): string {
  return tasks.length <= 10 ? tasks.join('、') : tasks.slice(0, 10).join('、') + ` 等${tasks.length}个`
}

/** 多任务时的文件名戳（最多列 3 个） */
export function taskFileStamp(tasks: string[]): string {
  return tasks.length === 1 ? tasks[0] : tasks.slice(0, 3).join('_') + (tasks.length > 3 ? `等${tasks.length}个` : '')
}
