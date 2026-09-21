<script setup lang="ts">
/**
 * 任务量录入：按「任务（考核表）」录入，表格为「区域 × 表头列」（与考核表一一对应）。
 *
 * 为什么按任务而不是按合同：使用方的考核表是按任务下达的（市例行 / 市监督 各一张），
 * 同一任务的多个合同编号（源库里有拼写漂移）共用这一份下达量。
 *
 * 三个入口：
 * - **下载模板**：带出当前任务当年的「区域 × 列」全表（名称已填好、任务量留空）→ 填完直接导回；
 * - **导出当前值**：把页面上正在编辑的值导出（用于留档或交接）；
 * - **Excel 导入**：按列名识别，逐行报错，成功行写库、失败行不写。
 *
 * 保存策略：只提交**改动过的格子**（脏值），不会把整张表回写覆盖别人刚改的数据。
 */
import { computed, onMounted, ref, watch } from 'vue'
import ExcelJS from 'exceljs'
import { Download, Eraser, FileDown, RefreshCw, Save, Upload } from 'lucide-vue-next'
import {
  progressApi, type ProgressImportRow, type ProgressQuotaPanel, type ProgressTaskType,
} from '../../api/client'

const props = defineProps<{ year: number; taskTypeId: number; tasks: ProgressTaskType[] }>()
const emit = defineEmits<{ changed: [msg: string] }>()

const panel = ref<ProgressQuotaPanel | null>(null)
const values = ref<Record<string, string>>({})   // `${region}|${categoryId}` → 输入框文本
const dirty = ref<Set<string>>(new Set())
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

const taskName = computed(
  () => panel.value?.task?.name || props.tasks.find((t) => t.id === props.taskTypeId)?.name || '任务',
)
const columns = computed(() => panel.value?.columns ?? [])
const rows = computed(() => panel.value?.rows ?? [])

function key(region: string, catId: number): string {
  return `${region}|${catId}`
}

async function load(): Promise<void> {
  if (!props.taskTypeId) return
  loading.value = true
  error.value = ''
  try {
    const r = await progressApi.quotas(props.year, props.taskTypeId)
    panel.value = r
    const next: Record<string, string> = {}
    r.category_values.forEach((v) => {
      next[key(v.region, v.category_id)] = v.quota ? String(v.quota) : ''
    })
    values.value = next
    dirty.value = new Set()
  } catch (e) {
    error.value = (e as Error).message
    panel.value = null
  } finally {
    loading.value = false
  }
}

function onInput(region: string, catId: number, text: string): void {
  values.value[key(region, catId)] = text
  dirty.value = new Set(dirty.value).add(key(region, catId))
}

function numOf(region: string, catId: number): number {
  const raw = values.value[key(region, catId)]
  const n = Number(String(raw ?? '').trim() || 0)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
}

/** 完成量参考（只读）：填任务量的人当场看到进度 */
const doneMap = computed(() => {
  const m = new Map<string, number>()
  ;(panel.value?.category_values ?? []).forEach((v) => m.set(key(v.region, v.category_id), v.done))
  return m
})

const rowTotals = computed(() => {
  const m = new Map<string, number>()
  rows.value.forEach((r) => {
    let sum = 0
    columns.value.forEach((c) => (sum += numOf(r.region, c.category_id)))
    m.set(r.region, sum)
  })
  return m
})

const grandTotal = computed(() => [...rowTotals.value.values()].reduce((a, b) => a + b, 0))

async function save(): Promise<void> {
  if (!panel.value) return
  const items: { region: string; category_id: number; quota: number }[] = []
  dirty.value.forEach((k) => {
    const [region, cid] = k.split('|')
    items.push({ region, category_id: Number(cid), quota: numOf(region, Number(cid)) })
  })
  if (!items.length) {
    notice.value = '没有改动需要保存'
    return
  }
  saving.value = true
  notice.value = ''
  error.value = ''
  try {
    const r = await progressApi.saveQuotas(props.year, props.taskTypeId, items)
    notice.value = r.message
    await load()
    emit('changed', r.message)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    saving.value = false
  }
}

async function clearAll(): Promise<void> {
  if (!window.confirm(
    `确定清空「${taskName.value}」在 ${props.year} 年的全部任务量？此操作不可撤销。`)) return
  try {
    const r = await progressApi.clearQuotas(props.year, props.taskTypeId)
    notice.value = r.message
    await load()
    emit('changed', r.message)
  } catch (e) {
    error.value = (e as Error).message
  }
}

/* ==================== Excel 模板 / 导出 / 导入 ==================== */

async function download(fileName: string, fillCurrent: boolean): Promise<void> {
  if (!panel.value) return
  const wb = new ExcelJS.Workbook()
  const ws = wb.addWorksheet('任务量')
  ws.addRow(['任务', '区域', '列（品类）', '任务量'])
  ws.getRow(1).font = { bold: true }
  columns.value.forEach((c) =>
    rows.value.forEach((r) => {
      ws.addRow([
        taskName.value,
        r.region,
        c.category,
        fillCurrent ? numOf(r.region, c.category_id) || '' : '',
      ])
    }),
  )
  ws.getColumn(1).width = 14
  ws.getColumn(2).width = 14
  ws.getColumn(3).width = 26
  ws.getColumn(4).width = 12
  // 使用说明写在第二个工作表，避免占用导入数据区
  const tips = wb.addWorksheet('填写说明')
  tips.addRow(['填写说明'])
  tips.addRow(['1. 请在「任务量」列填数字；区域与列名已填好，不要改名（改名会导入失败并提示行号）'])
  tips.addRow([`2. 本模板对应任务「${taskName.value}」${props.year} 年的列方案`])
  tips.addRow([`3. 本模板共 ${columns.value.length * rows.value.length} 行 = ${rows.value.length} 个区域 × ${columns.value.length} 列`])
  const buf = await wb.xlsx.writeBuffer()
  const blob = new Blob([buf], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  a.click()
  URL.revokeObjectURL(url)
}

function pickFile(): void {
  fileInput.value?.click()
}

async function onFile(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  notice.value = ''
  error.value = ''
  try {
    const wb = new ExcelJS.Workbook()
    await wb.xlsx.load(await file.arrayBuffer())
    const ws = wb.worksheets[0]
    if (!ws) throw new Error('Excel 里没有工作表')
    const header: string[] = []
    ws.getRow(1).eachCell((cell, col) => (header[col - 1] = String(cell.value ?? '').trim()))
    const find = (kw: string[]) => header.findIndex((h) => kw.some((k) => (h || '').includes(k)))
    const iRegion = find(['区域', '县区'])
    const iCat = find(['品类', '列'])
    const iQuota = find(['任务量', '数量'])
    if (iRegion < 0 || iCat < 0 || iQuota < 0) {
      throw new Error('表头缺少必要列：区域 / 列（品类）/ 任务量（建议直接用「下载模板」）')
    }
    const rowsOut: ProgressImportRow[] = []
    ws.eachRow((row, idx) => {
      if (idx === 1) return
      const get = (i: number) => (i >= 0 ? String(row.getCell(i + 1).value ?? '').trim() : '')
      const region = get(iRegion)
      const cname = get(iCat)
      const quota = get(iQuota)
      if (!region && !cname && !quota) return
      rowsOut.push({ region, category: cname, quota: quota === '' ? 0 : quota })
    })
    if (!rowsOut.length) throw new Error('没有解析到数据行（第 1 行必须是表头）')
    const r = await progressApi.importQuotas(props.year, props.taskTypeId, rowsOut)
    notice.value = r.message + (r.errors?.length ? `；失败示例：第 ${r.errors[0].row} 行 ${r.errors[0].reason}` : '')
    await load()
    emit('changed', r.message)
  } catch (err) {
    error.value = `导入失败：${(err as Error).message}`
  } finally {
    input.value = ''
  }
}

watch(() => [props.year, props.taskTypeId], load)
onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <div class="glass-card flex flex-wrap items-end gap-3 px-5 py-4">
      <div class="flex flex-col gap-1">
        <span class="text-[12px] text-ink-muted">当前任务（考核表）</span>
        <span class="rounded-lg bg-white/5 px-3 py-1.5 text-[13.5px] font-bold text-ink">
          {{ taskName }}
          <span class="ml-1 font-normal text-ink-muted">{{ year }} 年 · {{ columns.length }} 列</span>
        </span>
      </div>

      <button class="tech-btn" :disabled="saving || !dirty.size" @click="save">
        <Save :size="15" />{{ saving ? '保存中…' : `保存改动（${dirty.size}）` }}
      </button>
      <button class="tech-btn" :disabled="!panel" @click="download(`任务量模板_${year}_${taskName}.xlsx`, false)">
        <FileDown :size="15" />下载模板
      </button>
      <button class="tech-btn" :disabled="!panel" @click="download(`任务量_${year}_${taskName}.xlsx`, true)">
        <Download :size="15" />导出当前值
      </button>
      <button class="tech-btn" @click="pickFile"><Upload :size="15" />Excel 导入</button>
      <button class="tech-btn" @click="load"><RefreshCw :size="15" />刷新</button>
      <button class="tech-btn" @click="clearAll"><Eraser :size="15" />清空该任务</button>
      <input ref="fileInput" type="file" accept=".xlsx,.xls" class="hidden" @change="onFile" />

      <span class="ml-auto text-[12px] text-ink-muted">
        {{ rows.length }} 个区域 × {{ columns.length }} 列
      </span>
    </div>

    <div v-if="error" class="glass-card border-err/40 bg-err/10 px-5 py-3 text-[13px] text-err">{{ error }}</div>
    <div v-if="notice" class="glass-card border-ok/40 bg-ok/10 px-5 py-3 text-[13px] text-ok">{{ notice }}</div>

    <div v-if="loading && !panel" class="glass-card px-6 py-14 text-center">
      <div class="loader" />
      <p class="text-sm text-ink-muted">正在读取任务量…</p>
    </div>

    <div v-else-if="panel" class="glass-card overflow-auto px-5 pb-5 pt-4" style="max-height: 74vh">
      <table class="border-separate border-spacing-0 text-[12.5px]">
        <thead class="sticky top-0 z-20">
          <tr>
            <th class="sticky left-0 z-30 min-w-[104px] border-b border-white/15 bg-[#241f52] px-3 py-2 text-left font-bold text-ink">
              县区市
            </th>
            <th
              v-for="c in columns"
              :key="'c' + c.category_id"
              class="border-b border-l border-white/15 bg-[#241f52] px-2 py-2 text-center font-bold text-ink"
              :title="c.big_kind"
            >
              {{ c.category }}
            </th>
            <th class="border-b border-l border-white/15 bg-[#241f52] px-3 py-2 text-center font-bold text-ink">行合计</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.region" class="group">
            <td class="sticky left-0 z-10 whitespace-nowrap border-b border-white/10 bg-[#241f52] px-3 py-2 font-semibold text-ink group-hover:bg-[#2d2765]">
              {{ r.region }}
            </td>
            <td v-for="c in columns" :key="'v' + c.category_id" class="border-b border-l border-white/10 px-2 py-1.5 text-center">
              <input
                :value="values[key(r.region, c.category_id)] ?? ''"
                type="number"
                min="0"
                class="w-[62px] rounded-md border bg-white/5 px-1.5 py-1 text-center text-[13px] tabular-nums outline-none transition-colors focus:border-primary"
                :class="dirty.has(key(r.region, c.category_id)) ? 'border-warn/70 text-warn' : 'border-white/15 text-ink'"
                placeholder="0"
                @input="onInput(r.region, c.category_id, ($event.target as HTMLInputElement).value)"
              />
              <div class="mt-0.5 text-[10px] text-ink-muted">完成 {{ doneMap.get(key(r.region, c.category_id)) ?? 0 }}</div>
            </td>
            <td class="border-b border-l border-white/10 px-3 py-2 text-center font-bold tabular-nums text-ink">
              {{ rowTotals.get(r.region) ?? 0 }}
            </td>
          </tr>
          <tr class="bg-white/5 font-bold">
            <td class="sticky left-0 z-10 border-t border-white/20 bg-[#2d2765] px-3 py-2 text-ink">合计</td>
            <td
              v-for="c in columns"
              :key="'t' + c.category_id"
              class="border-t border-l border-white/20 px-2 py-2 text-center tabular-nums text-ink"
            >
              {{ rows.reduce((acc, r) => acc + numOf(r.region, c.category_id), 0) }}
            </td>
            <td class="border-t border-l border-white/20 px-3 py-2 text-center text-[15px] tabular-nums text-ink">{{ grandTotal }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="glass-card px-5 py-3 text-[12.5px] text-ink-muted">
      录入说明：任务量**按任务（考核表）下达**，同任务下的多份合同共用这一份；单元格填任务量，
      下方灰字是当前完成量（只读参考）。改动过的格子会变黄，点「保存改动」只提交这些格子。
      想批量填写就点「下载模板」——模板里区域与列名都已排好（{{ rows.length || 9 }} 行 ×
      {{ columns.length || 15 }} 列），填完用「Excel 导入」原样导回即可。
      列名称不同（比如市监督与市例行指标项不同）就切到对应任务，各自录入、互不影响。
    </p>
  </div>
</template>
