/**
 * 报检编号+小号 → 检测项目及方法（原 `queryByDetectionNo`，行 1150~1268）。
 *
 * 逐字保留：编号解析、精确校验（组合键模式同时接受不带小号的报检编号）、
 * 全未命中时的模糊回退与「另一种编号类型」二次回退、明细/去重两种 SQL 与排序、
 * 表头与配色、未命中编号的提示拼接（含最多 30 个的截断规则）。
 * 迁移差异：候选编号列表改为结构化返回（由 Vue 渲染可点击芯片，替代内联 onclick）。
 *
 * 新增「按合同号导出」（`queryByContractNo`）：直接按 `d.CONTRACTS_NO LIKE`（支持逗号/分号多值）
 * 取该合同下全部样品的检测项目及方法；列定义、配色、明细/去重两种模式与文件名规则与编号模式一致
 * （编号模式下的列头为「报检编号+小号」，合同模式取同一列）。
 */
import {
  buildContractCond,
  collectByChunks,
  createStyledExcel,
  dmQuery,
  fuzzySearchNos,
  keySelect,
  noTypeLabel,
  parseNoList,
  resolveNoType,
  sqlInList,
  whereIn,
  type CellValue,
} from '../helpers'
import { danger, esc, ok, warn, type DmOutcome } from '../outcome'

export interface DetectionNoParams {
  /** 点击候选编号芯片时传入，直接按该编号导出 */
  forcedNo?: string
  forcedCol?: string
  /** 文本域内容 */
  inputText: string
  /** 编号类型下拉值：auto / DETECTION_KEY / ORIGINAL_NO */
  noTypeSel: string
  /** 明细 detail / 去重 distinct */
  mode: string
  setProgress: (html: string) => void
}

export interface DetectionContractParams {
  /** 合同号（多个用逗号/分号分隔，模糊匹配） */
  contractNo: string
  /** 明细 detail / 去重 distinct */
  mode: string
  setProgress: (html: string) => void
}

/** 把一段说明插入到已有结果之前（对应原 `resultEl.innerHTML = 说明 + resultEl.innerHTML`） */
function prepend(outcome: DmOutcome, html: string): DmOutcome {
  if (outcome.type === 'html') return { type: 'html', html: html + outcome.html }
  return { ...outcome, intro: html + outcome.intro }
}

/** 表头配色（编号模式与合同号模式共用） */
const HEADER_COLORS: Record<string, string> = {
  基质: 'E8EAF6',
  报检编号: 'E8EAF6',
  '报检编号+小号': 'E8EAF6',
  原始编号: 'E8EAF6',
  检测项目: 'FFF3E0',
  检测方法: 'E8F5E9',
}

/**
 * 导出查询结果（原代码中「表头 + excelRows + 文件名 + createStyledExcel + 提示」这一段）。
 * `sampleTip` 为明细模式下「共 X 个<编号类型>」的前半句，`tail` 追加在提示末尾（未命中编号提示）。
 */
async function exportRows(
  rows: unknown[][],
  mode: string,
  lbl: string,
  stamp: string,
  sampleTip: string,
  tail = '',
): Promise<DmOutcome> {
  const headers =
    mode === 'distinct'
      ? ['基质', '检测项目', '检测方法']
      : [lbl, '报检编号', '样品名称', '样品小号', '检测项目', '标准号', '检测方法', '子方法', '引用标准']
  const projIdx = mode === 'distinct' ? 1 : 4
  const excelRows: CellValue[][] = rows.map((r) => r.map((v) => (v == null ? '' : String(v))))
  const fileName = `${lbl}项目及方法_${String(stamp).replace(/[\\/:*?"<>|]/g, '_').slice(0, 80)}.xlsx`
  // 原实现是 fire-and-forget，导出失败会被静默吞掉；迁移后改为可感知失败
  try {
    await createStyledExcel(headers, excelRows, '编号项目及方法', fileName, HEADER_COLORS)
  } catch (e) {
    return danger(`❌ 导出失败：${esc((e as Error).message)}`)
  }
  const projCount = new Set(rows.map((r) => String(r[projIdx]))).size
  const tips =
    mode === 'distinct'
      ? `✅ 导出完成！共 ${new Set(rows.map((r) => String(r[0] == null ? '' : r[0]))).size} 个基质、${projCount} 个检测项目、${rows.length} 条记录。<br>📁 ${fileName}`
      : `✅ 导出完成！共 ${sampleTip}、${projCount} 个检测项目、${rows.length} 条记录。<br>📁 ${fileName}`
  return ok(tips + tail)
}

export async function queryByDetectionNo(p: DetectionNoParams): Promise<DmOutcome> {
  const setProgress = p.setProgress
  const mode = p.mode

  let nos: string[] = []
  if (p.forcedNo) {
    nos = [p.forcedNo]
  } else {
    nos = parseNoList(p.inputText)
    if (!nos.length) {
      return danger('⚠️ 请先粘贴或输入编号')
    }
  }
  const col = p.forcedCol || resolveNoType(nos, p.noTypeSel)
  const lbl = noTypeLabel(col)

  setProgress(`⏳ 正在校验 ${nos.length} 个${lbl}...`)

  // 1) 精确校验：哪些编号在库中真实存在
  //    组合键模式返回两列（组合键、报检编号），因此不带小号的报检编号也算命中
  const chk = await collectByChunks(col, nos, (c, part) =>
    c === 'ORIGINAL_NO'
      ? 'SELECT DISTINCT ORIGINAL_NO FROM DETECTION.DT_SAMPLE ' +
        'WHERE IS_DELETED = 0 AND ORIGINAL_NO IN (' + sqlInList(part) + ')'
      : 'SELECT DISTINCT DETECTION_NO || SMALL_NO, DETECTION_NO FROM DETECTION.DT_SAMPLE ' +
        'WHERE IS_DELETED = 0 AND (DETECTION_NO || SMALL_NO IN (' + sqlInList(part) + ') ' +
        'OR DETECTION_NO IN (' + sqlInList(part) + '))',
  )
  if (chk.error) {
    return danger(`❌ 查询失败：${esc(chk.error)}<br>请确认数据库已连接后再试`)
  }
  const hitSet = new Set<string>()
  const keyCols = col === 'ORIGINAL_NO' ? 1 : 2
  ;(chk.rows || []).forEach((r) => {
    for (let i = 0; i < keyCols; i += 1) if (r[i] != null) hitSet.add(String(r[i]))
  })
  const hit = nos.filter((n) => hitSet.has(n))
  const miss = nos.filter((n) => !hitSet.has(n))

  if (hit.length === 0) {
    if (nos.length > 1) {
      return warn(`⚠️ ${nos.length} 个编号全部未匹配。<br>请确认编号类型选对了（当前按「${lbl}」查询）。`)
    }
    setProgress('⏳ 未精确匹配，正在模糊搜索...')
    const other = col === 'ORIGINAL_NO' ? 'DETECTION_KEY' : 'ORIGINAL_NO'
    const sr = await fuzzySearchNos(col, nos[0])
    if (sr.error) {
      return danger(`❌ 搜索失败：${esc(sr.error)}`)
    }
    const cand = sr.nos || []
    if (cand.length === 1) return queryByDetectionNo({ ...p, forcedNo: cand[0], forcedCol: col })
    if (cand.length > 1) {
      return { type: 'noList', intro: `🔍 找到 ${cand.length} 个匹配的${lbl}，点击编号导出：`, col, nos: cand }
    }
    const alt = await fuzzySearchNos(other, nos[0])
    const altNos = alt.nos || []
    if (!alt.error && altNos.length) {
      if (altNos.length === 1) {
        // 另一种编号类型下唯一命中：直接导出，并在结果上方说明来源
        const inner = await queryByDetectionNo({ ...p, forcedNo: altNos[0], forcedCol: other })
        return prepend(
          inner,
          `<div style="color:var(--warning);margin-bottom:6px;">按「${lbl}」没找到，已自动按「${noTypeLabel(other)}」匹配到 <b>${esc(altNos[0])}</b> 并导出：</div>`,
        )
      }
      const intro = `<div style="color:var(--warning);margin-bottom:6px;">按「${lbl}」没找到，下面是按「${noTypeLabel(other)}」匹配到的结果：</div>`
      return prepend(
        { type: 'noList', intro: `🔍 找到 ${altNos.length} 个匹配的${noTypeLabel(other)}，点击编号导出：`, col: other, nos: altNos },
        intro,
      )
    }
    return warn(`⚠️ 未找到包含「${esc(nos[0])}」的编号（报检编号与原始编号都已试过）`)
  }

  setProgress(`⏳ 正在查询 ${hit.length} 个${lbl}的项目及方法...`)

  const isKey = col !== 'ORIGINAL_NO'
  const kSel = keySelect(col)
  const sel =
    mode === 'distinct'
      ? 's.NAME, p.DECIDE_PROJECT_NAME, p.DECIDE_PROJECT_METHOD_NAME'
      : kSel +
        ', s.DETECTION_NO, s.NAME, s.SMALL_NO, p.DECIDE_PROJECT_NAME, p.STANDARD_NO, p.DECIDE_PROJECT_METHOD_NAME, p.SUB_METHOD_NAME, p.QUOTED_STANDARD'
  // DISTINCT 下 ORDER BY 用列序号；明细按 报检编号/小号/项目序号 排序
  const orderBy =
    mode === 'distinct'
      ? '1, 2, 3'
      : isKey
        ? 's.DETECTION_NO, s.SMALL_NO, p.SAMPLE_SMALL_NO, p.SEQ_NO'
        : 's.ORIGINAL_NO, p.SAMPLE_SMALL_NO, p.SEQ_NO'

  const d = await collectByChunks(col, hit, (c, part) =>
    'SELECT ' + (mode === 'distinct' ? 'DISTINCT ' : '') + sel + ' ' +
    'FROM DETECTION.DT_SAMPLE s JOIN DETECTION.DT_SAMPLE_PROJECT p ON p.SAMPLE_ID = s.ID ' +
    'WHERE s.IS_DELETED = 0 AND p.IS_DELETED = 0 AND ' + whereIn(c, part) + ' ' +
    'ORDER BY ' + orderBy,
  )
  if (d.error) {
    return danger(`❌ 查询失败：${esc(d.error)}`)
  }
  const rows = d.rows || []
  if (rows.length === 0) {
    return warn(`⚠️ 这些${lbl}下没有检测项目记录`)
  }

  const stamp = hit.length === 1 ? hit[0] : `${hit.length}个编号`
  let tail = ''
  if (miss.length) {
    const show = miss.slice(0, 30).map((m) => esc(m)).join('、') + (miss.length > 30 ? ` …等共 ${miss.length} 个` : '')
    tail = `<br><span style="color:var(--warning);">⚠️ ${miss.length} 个编号未在库中找到，已跳过：${show}</span>`
  }
  return exportRows(rows, mode, lbl, stamp, `${hit.length} 个${lbl}`, tail)
}

/**
 * 按合同号导出检测项目及方法。
 *
 * 与编号模式的差异只在取数入口：直接按 `d.CONTRACTS_NO LIKE`（buildContractCond，
 * 支持逗号/分号分隔多值模糊匹配）联 DT_DETECTION 取该合同下全部样品；
 * 列定义、明细/去重模式、排序、配色与文件名规则与编号模式完全一致
 * （明细模式首列取「报检编号+小号」组合键）。
 */
export async function queryByContractNo(p: DetectionContractParams): Promise<DmOutcome> {
  const setProgress = p.setProgress
  const mode = p.mode
  const contractNo = (p.contractNo || '').trim()
  if (!contractNo) {
    return danger('⚠️ 请先输入合同号')
  }
  const cond = buildContractCond(contractNo)
  if (!cond) {
    return danger('⚠️ 请先输入合同号')
  }
  const lbl = noTypeLabel('DETECTION_KEY')

  setProgress(`⏳ 正在查询合同 ${contractNo} 的检测项目及方法...`)

  // 明细/去重两种模式的取数列与排序（与编号模式一致；去重模式 DISTINCT + 列序号排序）
  const sel =
    mode === 'distinct'
      ? 's.NAME, p.DECIDE_PROJECT_NAME, p.DECIDE_PROJECT_METHOD_NAME'
      : keySelect('DETECTION_KEY') +
        ', s.DETECTION_NO, s.NAME, s.SMALL_NO, p.DECIDE_PROJECT_NAME, p.STANDARD_NO, p.DECIDE_PROJECT_METHOD_NAME, p.SUB_METHOD_NAME, p.QUOTED_STANDARD'
  const orderBy =
    mode === 'distinct'
      ? '1, 2, 3'
      : 's.DETECTION_NO, s.SMALL_NO, p.SAMPLE_SMALL_NO, p.SEQ_NO'

  const d = await dmQuery(
    'SELECT ' + (mode === 'distinct' ? 'DISTINCT ' : '') + sel + ' ' +
    'FROM DETECTION.DT_SAMPLE s ' +
    'JOIN DETECTION.DT_DETECTION d ON d.NO = s.DETECTION_NO AND d.IS_DELETED = 0 ' +
    'JOIN DETECTION.DT_SAMPLE_PROJECT p ON p.SAMPLE_ID = s.ID ' +
    'WHERE s.IS_DELETED = 0 AND p.IS_DELETED = 0' + cond + ' ' +
    'ORDER BY ' + orderBy,
  )
  if (d.error) {
    return danger(`❌ 查询失败：${esc(d.error)}`)
  }
  const rows = d.rows || []
  if (rows.length === 0) {
    return warn(`⚠️ 合同 ${esc(contractNo)} 下没有检测项目记录`)
  }

  // 明细模式下「共 X 个报检编号+小号」：按首列组合键去重计数
  const sampleTip = `${new Set(rows.map((r) => String(r[0] == null ? '' : r[0]))).size} 个${lbl}`
  return exportRows(rows, mode, lbl, `合同${contractNo}`, sampleTip)
}
