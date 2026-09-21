<script setup lang="ts">
/**
 * SQL Server 查询工具（移植自 sqlserver-query-tool/sql-query.html，原 :3003）。
 *
 * UI 保真：DOM 结构、class、文案与原文一致；样式声明原样保留（加 `.v-sql` 前缀隔离）。
 * JS 逻辑完整改写为 Vue：命令式 DOM 操作 → 响应式状态；接口路径 /api/query → /api/sqlserver/query。
 *
 * 迁移中修正的两点（均为旧实现缺陷，已在文档中记录）：
 * 1. 旧页按属性名取数（`s.Submission_ID`），但后端返回的是**位置数组**（`rows: [[...]]`），
 *    导致「样品编号/样品名称/类别/抽样环节/区域/抽样地址/主体名称/日期/任务来源」等属性列全部为空。
 *    新版按 columns 映射成对象后再取数，输出为预期内容。
 * 2. 旧页点击样品类型标签会因 `<label>` 包裹 checkbox 而产生「默认激活 + 手动取反」双重切换，
 *    新版用单一事件源（`@click.prevent` + v-model），点击标签即选中一次。
 * 其余算法（parseArea / inferKind / normalizeItem / 分批 800 / 判定规则 / 导出样式与文件名）逐字保留。
 */
import { onMounted, ref } from 'vue'
import ExcelJS from 'exceljs'
import { navApi, sqlserverApi, type SqlQueryResult } from '../api/client'

/* 数据来源提示：镜像模式下把「数据截止时间」显示出来，避免被误认为实时数据 */
const sourceText = ref('')
const sourceTitle = ref('')

async function loadSource(): Promise<void> {
  try {
    const s = await navApi.status()
    const m = s.mirror
    if (!m) return
    if (m.mode === 'mirror') {
      const t = (m.data_deadline || '').replace('T', ' ')
      sourceText.value = `🧊 镜像数据${t ? ` · 截止 ${t}` : '（尚未同步）'}`
      sourceTitle.value = '查询走本地 SQLite 镜像：毫秒级返回，源库暂时不可达时页面仍可用'
    } else {
      sourceText.value = '🛰 直连源库（实时）'
      sourceTitle.value = m.fallback || '查询直接走源库'
    }
  } catch {
    /* 状态获取失败不影响主流程 */
  }
}

void loadSource()

/* ==================== 连接状态 ==================== */
const isConnected = ref(false)
const statusText = ref('未连接')
const statusType = ref('')
const connDesc = ref('未连接数据库')

async function autoConnect(): Promise<void> {
  statusText.value = '连接中...'
  statusType.value = ''
  try {
    const r = await sqlserverApi.autoConnect()
    if (r.success) {
      isConnected.value = true
      statusText.value = '✓ 已连接'
      statusType.value = 'connected'
      connDesc.value = r.mirror
        ? `SQLite 镜像 · 数据截止 ${(r.dataDeadline || '').replace('T', ' ')}`
        : `${r.server}:${r.port}/${r.database}`
    } else {
      statusText.value = '连接失败'
      statusType.value = 'error'
      connDesc.value = `连接失败: ${r.error || ''}`
    }
  } catch (e) {
    statusText.value = '异常'
    statusType.value = 'error'
    connDesc.value = `异常: ${(e as Error).message}`
  }
}

async function disconnect(): Promise<void> {
  await sqlserverApi.disconnect()
  isConnected.value = false
  statusText.value = '已断开'
  statusType.value = ''
  connDesc.value = '未连接数据库'
}

/* ==================== 样品类型 ==================== */
const TEMPLATE_KIND_I_MAP: Record<string, string[]> = {
  '农产品（蔬菜、水果、食用菌、稻谷等）': ['农产品', '蔬菜', '水果', '食用菌', '稻谷', '小麦'],
  畜产品: ['畜产品', '生鲜乳'],
  水产品: ['水产品'],
  肥料: ['肥料'],
  环境水质: ['环境水质'],
  环境土壤: ['环境土壤'],
}

interface KindItem {
  key: string
  checked: boolean
  active: boolean
}

const kinds = ref<KindItem[]>(
  Object.keys(TEMPLATE_KIND_I_MAP).map((k) => ({ key: k, checked: false, active: false })),
)

function toggleKind(k: KindItem): void {
  k.checked = !k.checked
  k.active = k.checked
}

const year = ref<number>(new Date().getFullYear())
const years = ref<number[]>([])
const dateFrom = ref('')
const dateTo = ref('')

/* ==================== 工具函数（逐字移植） ==================== */
function esc(s: unknown): string {
  return String(s == null ? '' : s).replace(/'/g, "''")
}

function parseArea(str: unknown): string {
  if (!str) return ''
  const s = String(str)
  for (let i = s.length - 1; i >= 0; i -= 1) {
    const ch = s[i]
    if (ch === '区' || ch === '县') {
      let start = i - 1
      while (start >= 0 && /[\u4e00-\u9fa5]/.test(s[start]) && i - start <= 3) start -= 1
      let name = s.slice(start + 1, i + 1)
      if (name.length >= 2) {
        name = name.replace(/^市/, '').replace(/^省/, '')
        const at = name.indexOf('市')
        if (at >= 0 && name.length - at >= 3) name = name.slice(at + 1)
        if (name.length >= 2) return name
      }
    }
  }
  for (let i = s.length - 1; i >= 0; i -= 1) {
    if (s[i] === '市') {
      if (s[i + 1] === '场') continue
      let start = i - 1
      while (start >= 0 && /[\u4e00-\u9fa5]/.test(s[start]) && i - start <= 3) start -= 1
      const name = s.slice(start + 1, i + 1)
      if (name.length >= 2) {
        const at = name.indexOf('市')
        if (at >= 0 && name.length - at >= 3) return name.slice(at + 1)
        return name.replace(/^省/, '')
      }
    }
  }
  return ''
}

function inferKind(beChecked: unknown, sampleKindI: unknown): string {
  const s = String(beChecked || '').trim()
  if (!s || s === '——' || s === '-' || s === '/' || s === '、') return ''
  const rules: [string, string][] = [
    ['屠宰场', '屠宰场'], ['屠宰', '屠宰场'],
    ['养殖户', '散户'],
    ['养殖场', '养殖场'],
    ['养殖基地', '养殖基地'],
    ['牧业', '养殖场'],
    ['批发市场', '批发市场'],
    ['农副食品城', '批发市场'],
    ['农贸市场', '农贸市场'],
    ['集贸市场', '农贸市场'],
    ['超市', '超市'],
    ['鱼塘', '鱼塘'],
    ['门店', '门店'],
    ['种植基地', '生产基地'],
    ['种植户', '种植户'],
    ['家庭农场', '生产基地'],
    ['合作社', '生产基地'],
    ['农业园', '生产基地'],
    ['农业科技', '生产基地'],
    ['农业发展', '生产基地'],
    ['农场', '生产基地'],
    ['公司', '企业'],
    ['集团', '企业'],
    ['市场', '农贸市场'],
    ['养殖', '养殖场'],
  ]
  for (const [kw, kind] of rules) {
    if (s.includes(kw)) return kind
  }
  const ki = String(sampleKindI || '')
  if (ki.includes('水产品')) return '养殖基地'
  if (ki.includes('畜产品') || ki.includes('生鲜乳')) return '养殖场'
  if (['农产品', '蔬菜', '水果', '食用菌', '稻谷'].some((x) => ki.includes(x))) return '生产基地'
  return ''
}

function normalizeItem(name: unknown): string {
  let s = String(name || '').trim()
  if (!s) return ''
  s = s.replace(/^[﹡△*☆★]+/, '').trim()
  const aliasMap: Record<string, string> = { 克伦特罗: '克仑特罗', 盐酸克伦特罗: '克仑特罗' }
  if (aliasMap[s]) return aliasMap[s]
  const m = s.match(/^([\u4e00-\u9fa5A-Za-z0-9，、·]+?)\s*[（(\[][^（(\[）)\]]*[）)\]]\s*$/)
  if (m && m[1].length >= 2) {
    const inner = m[0].match(/[（(\[][^（(\[）)\]]*[）)\]]/)![0]
    if (/有效态|干土|鲜土|总量|以总量计|水溶性|可滴定|鲜样|干样|总酸|游离态|结合态|全盐量|总量计/.test(inner)) {
      return s
    }
    return m[1]
  }
  return s
}

/** 把后端的位置数组行按列名转成对象（修正旧页按属性名取数取不到值的问题） */
function toObjects(res: SqlQueryResult): Record<string, unknown>[] {
  const cols = res.columns || []
  return (res.rows || []).map((row) => {
    const obj: Record<string, unknown> = {}
    cols.forEach((c, i) => {
      obj[c] = (row as unknown[])[i]
    })
    return obj
  })
}

/* ==================== Excel 导出（逐字移植，去掉 CDN 依赖） ==================== */
async function createStyledExcel(
  headers: string[],
  dataRows: (string | number)[][],
  sheetName: string,
  fileName: string,
  headerColors: Record<string, string> | null,
): Promise<void> {
  const wb = new ExcelJS.Workbook()
  wb.creator = '数据导出'
  const ws = wb.addWorksheet(sheetName)

  const thin = { style: 'thin' as const }
  const border = { top: thin, left: thin, bottom: thin, right: thin }
  const align = { horizontal: 'center' as const, vertical: 'middle' as const, wrapText: true }
  const fontData = { name: 'Times New Roman', size: 10 }
  const fontHeader = { name: 'Times New Roman', size: 10, bold: true }

  const hRow = ws.addRow(headers)
  hRow.eachCell((c, colIdx) => {
    c.font = fontHeader
    c.alignment = align
    c.border = border
    const hText = headers[colIdx - 1]
    if (headerColors && headerColors[hText]) {
      c.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: `FF${headerColors[hText]}` } }
    }
  })

  for (const row of dataRows) {
    const r = ws.addRow(row)
    r.eachCell((c) => {
      c.font = fontData
      c.alignment = align
      c.border = border
    })
  }

  ws.columns.forEach((col, idx) => {
    let maxLen = 10
    ;[headers, ...dataRows].forEach((row) => {
      const v = row[idx]
      if (v != null) maxLen = Math.max(maxLen, String(v).length)
    })
    col.width = Math.min(maxLen + 3, 40)
  })

  const buf = await wb.xlsx.writeBuffer()
  const url = URL.createObjectURL(new Blob([buf]))
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  a.click()
  URL.revokeObjectURL(url)
}

/* ==================== 主查询（逐字移植） ==================== */
const resultMsg = ref('')
const resultColor = ref('')

const DEFAULT_COLOR = ''
const DANGER = 'var(--danger)'
const WARNING = 'var(--warning)'
const SUCCESS = 'var(--success)'

function setResult(msg: string, color = DEFAULT_COLOR): void {
  resultMsg.value = msg
  resultColor.value = color
}

async function querySampleTemplate(): Promise<void> {
  const y = year.value
  const from = dateFrom.value
  const to = dateTo.value

  if (!y) {
    setResult('❌ 请选择年份', DANGER)
    return
  }
  if (!!from !== !!to) {
    setResult('❌ 抽样开始/结束日期需同时填写，或都留空（全年）', DANGER)
    return
  }
  const selectedKindI = kinds.value.filter((k) => k.checked).map((k) => k.key)
  if (selectedKindI.length === 0) {
    setResult('❌ 请至少选择一个样品类型', DANGER)
    return
  }
  if (!isConnected.value) {
    setResult('❌ 请先连接数据库', DANGER)
    return
  }

  setResult('⏳ 正在查询样品数据...')

  try {
    const kindIValues: string[] = []
    for (const k of selectedKindI) {
      ;(TEMPLATE_KIND_I_MAP[k] || []).forEach((v) => kindIValues.push(v))
    }
    const kindICond = Array.from(new Set(kindIValues))
      .map((v) => `'${esc(v)}'`)
      .join(',')

    let dateCond = `YEAR(v.Cy_Date) = ${parseInt(String(y), 10)}`
    if (from && to) {
      dateCond += ` AND v.Cy_Date >= '${from}' AND v.Cy_Date < DATEADD(day, 1, '${to}')`
    }
    const sampleSQL =
      `SELECT DISTINCT v.Submission_ID, v.Sample_ID, v.Sample_Name, v.Sample_Kind_I,
                    v.Test_Kind, v.Task_Kind, v.Be_Checked, v.Cy_Date,
                    b.Kind, b.Area, b.Address
             FROM VIEW_SubMisSampleResult v
             LEFT JOIN TBe_Checked b ON v.Be_Checked = b.Be_Checked
             WHERE ${dateCond}
               AND v.Sample_Kind_I IN (${kindICond})
             ORDER BY v.Cy_Date, v.Submission_ID`

    const sampleRes = await sqlserverApi.query(sampleSQL)
    if (!sampleRes.success) {
      setResult(`❌ 样品查询失败：${sampleRes.error || ''}`, DANGER)
      return
    }
    const samples = toObjects(sampleRes)
    if (samples.length === 0) {
      setResult('⚠️ 该条件下没有样品数据', WARNING)
      return
    }

    setResult(`⏳ 已找到 ${samples.length} 个样品，正在查询检测项目...`)

    const allResults: Record<string, Record<string, { val: unknown; qualified: unknown }>> = {}
    const itemSet = new Set<string>()
    const BATCH = 800
    for (let i = 0; i < samples.length; i += BATCH) {
      const batch = samples.slice(i, i + BATCH)
      const subs = batch.map((s) => `'${esc(s.Submission_ID)}'`).join(',')
      const itemSQL =
        `SELECT tr.Submission_ID, tr.Item, td.Result_Data, tr.Limit, tr.Unit, tr.Qualified
                 FROM TResult tr
                 LEFT JOIN TResult_Data td ON tr.Result_ID = td.Result_ID
                 WHERE tr.Submission_ID IN (${subs})
                 ORDER BY tr.Submission_ID, tr.Item_Num`
      const itemRes = await sqlserverApi.query(itemSQL)
      if (!itemRes.success) {
        setResult(`❌ 检测项目查询失败：${itemRes.error || ''}`, DANGER)
        return
      }
      for (const r of toObjects(itemRes)) {
        const sid = r.Submission_ID as string
        const raw = r.Item
        if (!raw) continue
        const it = normalizeItem(raw)
        if (!it) continue
        itemSet.add(it)
        if (!allResults[sid]) allResults[sid] = {}
        allResults[sid][it] = {
          val: r.Result_Data != null ? r.Result_Data : '',
          qualified: r.Qualified != null ? r.Qualified : '',
        }
      }
    }

    const pesticides = Array.from(itemSet).sort()

    const headers = [
      '样品编号', '样品名称', '类别', '抽样环节', '区域', '抽样地址', '主体名称', '日期', '任务来源', '判定结果',
    ]
    for (const p of pesticides) headers.push(p)

    const rows: Record<string, unknown>[] = []
    samples.forEach((s) => {
      const row: Record<string, unknown> = {}
      headers.forEach((h) => {
        row[h] = ''
      })
      row['样品编号'] = s.Submission_ID || ''
      row['样品名称'] = s.Sample_Name || ''
      row['类别'] = s.Sample_Kind_I || ''
      row['抽样环节'] = String(s.Kind || '').trim() || inferKind(s.Be_Checked, s.Sample_Kind_I)
      row['区域'] = String(s.Area || '').trim() || parseArea(s.Address) || parseArea(s.Be_Checked) || ''
      row['抽样地址'] = s.Address || ''
      row['主体名称'] = s.Be_Checked || ''
      row['日期'] = s.Cy_Date ? String(s.Cy_Date).split('T')[0] : ''
      row['任务来源'] = s.Task_Kind || ''
      row['判定结果'] = ''

      const results = allResults[s.Submission_ID as string] || {}
      let hasFail = false
      for (const p of pesticides) {
        const res = results[p]
        if (res) {
          row[p] = res.val
          const q = String(res.qualified || '')
          if (q && (q.includes('不符合') || q === '不合格')) hasFail = true
        } else {
          row[p] = '-'
        }
      }
      row['判定结果'] = hasFail ? '不合格' : '符合'
      rows.push(row)
    })

    const excelRows = rows.map((row) => headers.map((h) => row[h] as string | number))
    const fileName = `样品检测结果汇总_${from ? `${from}_${to}` : `${y}年全年`}.xlsx`
    await createStyledExcel(headers, excelRows, '检测结果汇总', fileName, null)

    setResult(
      `✅ 导出完成！共 ${samples.length} 个样品，${pesticides.length} 个检测项目。\n📁 文件：${fileName}`,
      SUCCESS,
    )
  } catch (e) {
    setResult(`❌ 失败：${(e as Error).message}`, DANGER)
  }
}

/* ==================== 生命周期 ==================== */
onMounted(() => {
  const cur = new Date().getFullYear()
  const list: number[] = []
  for (let y = 2016; y <= cur; y += 1) list.push(y)
  years.value = list
  year.value = cur
  autoConnect()
})
</script>

<template>
  <div class="v-sql">
    <div class="header">
      <div>
        <h1>🗄️ SQL Server 查询工具</h1>
        <div class="sub">{{ connDesc }}</div>
        <span
          v-if="sourceText"
          :title="sourceTitle"
          style="display:inline-block;margin-top:6px;font-size:12px;background:#eef2ff;border-radius:20px;padding:3px 10px;"
        >{{ sourceText }}</span>
      </div>
      <div style="display:flex;gap:10px;align-items:center;">
        <div id="status" :class="statusType">{{ statusText }}</div>
        <button class="btn btn-primary" @click="autoConnect">连接</button>
        <button class="btn btn-ghost" @click="disconnect">断开</button>
      </div>
    </div>

    <div class="container">
      <!-- 全新模板：样品检测结果汇总 -->
      <div class="tpl-card">
        <h2>🧪 样品检测结果汇总模板 <span class="hint">9个属性列 + 动态检测项目列</span></h2>
        <div class="tpl-cond">
          <div class="tpl-field">
            <label>年份（抽样开始时间所在年份）</label>
            <select v-model="year" style="padding:8px 10px;border:1px solid var(--border);border-radius:8px;font-size:13px;min-width:120px;">
              <option v-for="y in years" :key="y" :value="y">{{ y }}年</option>
            </select>
          </div>
          <div class="tpl-field">
            <label>抽样开始日期 <span style="color:#94a3b8;font-weight:400;">（可留空=全年）</span></label>
            <input v-model="dateFrom" type="date" />
          </div>
          <div class="tpl-field">
            <label>抽样结束日期 <span style="color:#94a3b8;font-weight:400;">（可留空=全年）</span></label>
            <input v-model="dateTo" type="date" />
          </div>
          <div class="tpl-field">
            <label>样品类型（可多选）</label>
            <div class="tpl-kinds">
              <label
                v-for="k in kinds"
                :key="k.key"
                class="tpl-kind"
                :class="{ active: k.active }"
                @click.prevent="toggleKind(k)"
              >
                <input type="checkbox" v-model="k.checked" />{{ k.key }}
              </label>
            </div>
          </div>
          <button class="btn btn-primary" @click="querySampleTemplate">🔍 查询并导出</button>
        </div>
        <div class="tpl-result">
          <span :style="{ color: resultColor, whiteSpace: 'pre-line' }">{{ resultMsg }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style>
/* 原样式完整保留，仅加 `.v-sql` 前缀做隔离；body 规则改挂根容器 */
.v-sql { --bg:#f5f7fa; --card:#ffffff; --border:#e2e8f0; --primary:#2563eb; --primary-dark:#1d4ed8;
  --text:#1e293b; --text2:#64748b; --success:#16a34a; --danger:#dc2626; --warning:#d97706; }
.v-sql, .v-sql * { margin: 0; padding: 0; box-sizing: border-box; }
.v-sql {
  font-family: "Microsoft YaHei", -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}
.v-sql .header {
  background: linear-gradient(135deg, #2563eb, #1d4ed8);
  color: #fff;
  padding: 16px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.1);
}
.v-sql .header h1 { font-size: 20px; font-weight: 700; }
.v-sql .header .sub { font-size: 13px; opacity: 0.85; margin-top: 2px; }
.v-sql #status {
  padding: 6px 16px; border-radius: 20px; font-size: 13px; font-weight: 600;
  background: rgba(255,255,255,0.15);
}
.v-sql #status.connected { background: rgba(34,197,94,0.3); }
.v-sql #status.error { background: rgba(239,68,68,0.3); }
.v-sql .container { max-width: 1400px; margin: 20px auto; padding: 0 20px; }
.v-sql .card {
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 20px; margin-bottom: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}
.v-sql .card h2 { font-size: 16px; margin-bottom: 14px; color: var(--text); display: flex; align-items: center; gap: 8px; }
.v-sql .card h2 .hint { font-size: 12px; color: var(--text2); font-weight: 400; margin-left: auto; }
.v-sql .row-btns { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; align-items: center; }
.v-sql .btn {
  padding: 9px 20px; border: none; border-radius: 8px; cursor: pointer;
  font-size: 14px; font-weight: 600; transition: all 0.2s; color: #fff;
}
.v-sql .btn-primary { background: var(--primary); }
.v-sql .btn-primary:hover { background: var(--primary-dark); }
.v-sql .btn-success { background: var(--success); }
.v-sql .btn-success:hover { background: #15803d; }
.v-sql .btn-ghost { background: #e2e8f0; color: var(--text); }
.v-sql .btn-ghost:hover { background: #cbd5e1; }
.v-sql .btn:disabled { opacity: 0.5; cursor: not-allowed; }
.v-sql .conn-info { display: flex; gap: 14px; flex-wrap: wrap; align-items: center; font-size: 13px; color: var(--text2); margin-bottom: 12px; }
.v-sql .conn-info input, .v-sql .conn-info select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.v-sql .table-wrap { max-height: 480px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; margin-top: 12px; }
.v-sql table { width: 100%; border-collapse: collapse; font-size: 13px; }
.v-sql thead th {
  position: sticky; top: 0; background: #f1f5f9; padding: 10px 12px; text-align: left;
  font-weight: 700; color: var(--text); border-bottom: 2px solid var(--border); white-space: nowrap;
}
.v-sql tbody td { padding: 8px 12px; border-bottom: 1px solid #f1f5f9; color: var(--text); white-space: nowrap; }
.v-sql tbody tr:hover { background: #f8fafc; }
.v-sql .loading { text-align: center; color: var(--text2); padding: 30px; }
.v-sql .spinner {
  width: 26px; height: 26px; border: 3px solid #e2e8f0; border-top-color: var(--primary);
  border-radius: 50%; margin: 0 auto 12px; animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
/* ===== 模板卡片 ===== */
.v-sql .tpl-card {
  background: linear-gradient(135deg, #f8fafc, #eff6ff); border: 1px solid #bfdbfe;
  border-radius: 12px; padding: 20px; margin-bottom: 18px; box-shadow: 0 1px 3px rgba(37,99,235,0.08);
}
.v-sql .tpl-card h2 { font-size: 16px; margin-bottom: 14px; color: #1e3a8a; display: flex; align-items: center; gap: 8px; }
.v-sql .tpl-card h2 .hint { font-size: 12px; color: var(--text2); font-weight: 400; margin-left: auto; }
.v-sql .tpl-cond { display: flex; flex-wrap: wrap; gap: 18px; align-items: flex-end; margin-bottom: 14px; }
.v-sql .tpl-field { display: flex; flex-direction: column; gap: 6px; }
.v-sql .tpl-field label { font-size: 13px; color: var(--text2); font-weight: 600; }
.v-sql .tpl-field input[type="date"], .v-sql .tpl-field input[type="text"] {
  padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; min-width: 150px;
}
.v-sql .tpl-kinds { display: flex; flex-wrap: wrap; gap: 10px; }
.v-sql .tpl-kind {
  display: flex; align-items: center; gap: 6px; padding: 6px 12px;
  background: #fff; border: 1px solid var(--border); border-radius: 20px;
  font-size: 13px; cursor: pointer; user-select: none; transition: all 0.2s;
}
.v-sql .tpl-kind:hover { border-color: var(--primary); }
.v-sql .tpl-kind input { accent-color: var(--primary); cursor: pointer; }
.v-sql .tpl-kind.active { background: #dbeafe; border-color: var(--primary); color: var(--primary); font-weight: 600; }
.v-sql .tpl-result { font-size: 13px; }
.v-sql .tpl-result .msg { margin-top: 10px; }
</style>
