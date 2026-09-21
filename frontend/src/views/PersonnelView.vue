<script setup lang="ts">
/**
 * 人员能力表梳理（移植自 人员能力表梳理/index.html，原 :3007）。
 *
 * UI 保真：DOM 结构、class、文案、表格列头与原文一致；样式声明原样保留（加 `.v-personnel` 前缀隔离）。
 * JS 逻辑已完整改写为 Vue：
 * - 命令式 DOM 操作（getElementById / innerHTML 拼表格）→ 响应式状态 + v-for/v-if 模板渲染
 * - 手写 esc() 转义 → Vue 插值（更安全，渲染结果等价）
 * - 接口路径：/api/query-by-person → /api/personnel/query-by-person（同义改写）
 * - 空闲倒计时：原取自本服务 /api/idle-time（后端恒返回 300s，等价旧行为）→ 改为 /api/dm/idle-time
 * - 导出仍用 ExcelJS（改为本地 npm 依赖，去掉 jsdelivr CDN），文件名/表头/样式/列宽完全一致
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import ExcelJS from 'exceljs'
import { dmApi, personnelApi, type PersonnelByPersonRow, type PersonnelByProjectRow } from '../api/client'
import { parseAbilityWorkbook } from './personnel/abilityTable'
import { abilityCellCls, abilityCellText, buildPersonnelWorkbook, hasAbilityColumn } from './personnel/personnelExport'

const THIN = { style: 'thin' as const }
const BORDER = { top: THIN, left: THIN, bottom: THIN, right: THIN }
const ALIGN = { horizontal: 'center' as const, vertical: 'middle' as const, wrapText: true }

/* ==================== 顶部状态 ==================== */
/* 数据统一来自本地 SQLite 镜像：不再分达梦/SQL Server 展示连接状态，也没有空闲倒计时 */
const mirrorStatus = ref('检测中')

function updateMirrorStatus(errors: { dm: string | null; sql: string | null } | undefined): void {
  mirrorStatus.value = errors && (errors.dm || errors.sql) ? '异常' : '已就绪'
}

/* ==================== 卡1：按姓名查项目 + 标准 ==================== */
const nameInput = ref('')
const rows1 = ref<PersonnelByPersonRow[]>([])
const currentName = ref('')
const querying1 = ref(false)
const status1 = ref({ msg: '', cls: 'text-gray-500' })
const empty1 = ref('输入姓名后点击「查询」，即可查看该人员登记过的检测项目与标准')
const empty1Icon = ref('🔎')
const hasSearched1 = ref(false)

function setStatus1(msg: string, cls: string): void {
  status1.value = { msg, cls }
}

async function doQuery(): Promise<void> {
  const name = nameInput.value.trim()
  if (!name) {
    setStatus1('⚠️ 请输入检测人员姓名', 'text-amber-600')
    return
  }
  currentName.value = name
  querying1.value = true
  setStatus1('正在跨库查询，请稍候...', 'text-gray-500')
  try {
    const r = await personnelApi.byPerson(name)
    if (!r.success) {
      setStatus1(`❌ ${r.error}`, 'text-red-600')
      renderEmpty1()
      return
    }
    rows1.value = r.rows || []
    hasSearched1.value = true
    renderTable1()
    updateMirrorStatus(r.errors)
  } catch (e) {
    setStatus1(`❌ 请求失败: ${(e as Error).message}`, 'text-red-600')
    renderEmpty1()
  } finally {
    querying1.value = false
  }
}

function renderTable1(): void {
  if (!rows1.value.length) {
    hasSearched1.value = true
    empty1Icon.value = '📭'
    empty1.value = `未找到「${currentName.value}」登记的检测项目记录`
    setStatus1('✅ 查询完成，未找到记录', 'text-green-600')
    return
  }
  setStatus1(`✅ 查询完成，共 ${rows1.value.length} 项（去重后）`, 'text-green-600')
}

function renderEmpty1(): void {
  rows1.value = []
  hasSearched1.value = false
  empty1Icon.value = '🔎'
  empty1.value = '输入姓名后点击「查询」'
}

async function exportExcel(): Promise<void> {
  if (!rows1.value.length) return
  querying1.value = true
  try {
    // 工作簿构造在 personnelExport.ts（纯函数，可被自动化测试真实生成并回读断言）
    const buf = await buildPersonnelWorkbook(currentName.value, rows1.value, hasAbilityColumn(rows1.value, ability.value.loaded))
    const url = URL.createObjectURL(new Blob([buf]))
    const a = document.createElement('a')
    a.href = url
    a.download = `人员能力表_${currentName.value}.xlsx`
    a.click()
    URL.revokeObjectURL(url)
    setStatus1(`✅ 已导出：人员能力表_${currentName.value}.xlsx（${rows1.value.length} 项）`, 'text-green-600')
  } catch (e) {
    setStatus1(`❌ 导出失败: ${(e as Error).message}`, 'text-red-600')
  } finally {
    querying1.value = false
  }
}

/* ==================== 卡2：按项目/标准查人员 ==================== */
const kwInput = ref('')
const rows2 = ref<PersonnelByProjectRow[]>([])
const currentKw = ref('')
const querying2 = ref(false)
const status2 = ref({ msg: '', cls: 'text-gray-500' })
const empty2 = ref('输入检测项目或检测标准后点击「查询」，罗列出两个系统中所有登记过该项目的检测人员')
const empty2Icon = ref('🔎')
const hasSearched2 = ref(false)

function setStatus2(msg: string, cls: string): void {
  status2.value = { msg, cls }
}

async function doProjectQuery(): Promise<void> {
  const keyword = kwInput.value.trim()
  if (!keyword) {
    setStatus2('⚠️ 请输入检测项目或检测标准关键词', 'text-amber-600')
    return
  }
  currentKw.value = keyword
  querying2.value = true
  setStatus2('正在跨库查询所有登记人员，请稍候...', 'text-gray-500')
  try {
    const r = await personnelApi.byProject(keyword)
    if (!r.success) {
      setStatus2(`❌ ${r.error}`, 'text-red-600')
      renderEmpty2()
      return
    }
    rows2.value = r.rows || []
    hasSearched2.value = true
    renderTable2()
    updateMirrorStatus(r.errors)
  } catch (e) {
    setStatus2(`❌ 请求失败: ${(e as Error).message}`, 'text-red-600')
    renderEmpty2()
  } finally {
    querying2.value = false
  }
}

function renderTable2(): void {
  if (!rows2.value.length) {
    hasSearched2.value = true
    empty2Icon.value = '📭'
    empty2.value = `未找到「${currentKw.value}」相关的检测人员记录`
    setStatus2('✅ 查询完成，未找到记录', 'text-green-600')
    return
  }
  setStatus2(`✅ 查询完成，共 ${rows2.value.length} 项（去重后）`, 'text-green-600')
}

function renderEmpty2(): void {
  rows2.value = []
  hasSearched2.value = false
  empty2Icon.value = '🔎'
  empty2.value = '输入检测项目或标准后点击「查询」'
}

async function exportProjectExcel(): Promise<void> {
  if (!rows2.value.length) return
  querying2.value = true
  try {
    const wb = new ExcelJS.Workbook()
    wb.creator = '人员能力表梳理'
    const ws = wb.addWorksheet('检测人员表')

    ws.mergeCells('A1:E1')
    const titleCell = ws.getCell('A1')
    titleCell.value = `检测人员表：${currentKw.value}`
    titleCell.font = { name: 'Times New Roman', size: 13, bold: true }
    titleCell.alignment = { horizontal: 'center', vertical: 'middle' }
    ws.getRow(1).height = 24

    const hRow = ws.addRow(['序号', '检测项目', '检测标准', '检测人员', '最后登记日期'])
    hRow.eachCell((c) => {
      c.font = { name: 'Times New Roman', size: 11, bold: true }
      c.alignment = ALIGN
      c.border = BORDER
      c.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFD1FAE5' } }
    })

    rows2.value.forEach((row, i) => {
      const r = ws.addRow([i + 1, row.project, row.method, row.person, row.lastDate || ''])
      r.eachCell((c) => {
        c.font = { name: 'Times New Roman', size: 11 }
        c.alignment = ALIGN
        c.border = BORDER
      })
    })

    ws.getColumn(1).width = 8
    ws.getColumn(2).width = 28
    ws.getColumn(3).width = 36
    ws.getColumn(4).width = 18
    ws.getColumn(5).width = 16

    const buf = await wb.xlsx.writeBuffer()
    const url = URL.createObjectURL(new Blob([buf]))
    const a = document.createElement('a')
    a.href = url
    a.download = `检测人员表_${currentKw.value}.xlsx`
    a.click()
    URL.revokeObjectURL(url)
    setStatus2(`✅ 已导出：检测人员表_${currentKw.value}.xlsx（${rows2.value.length} 项）`, 'text-green-600')
  } catch (e) {
    setStatus2(`❌ 导出失败: ${(e as Error).message}`, 'text-red-600')
  } finally {
    querying2.value = false
  }
}

/* ==================== 能力表（检验检测能力表）==================== */
/* 上传的能力表存后端；卡1 的每行由此得到「是否在能力表」判定。未上传时该列不渲染。 */
const ability = ref({ loaded: false, fileName: '', uploadedAt: '', count: 0, uniquePairs: 0, uniqueProjects: 0 })
const abilityBusy = ref(false)
const abilityMsg = ref({ text: '', cls: 'text-gray-500' })
const abilityFileInput = ref<HTMLInputElement | null>(null)

function setAbilityMsg(text: string, cls: string): void {
  abilityMsg.value = { text, cls }
}

async function loadAbilityStatus(): Promise<void> {
  try {
    const r = await personnelApi.abilityStatus()
    ability.value = {
      loaded: !!r.loaded,
      fileName: r.fileName || '',
      uploadedAt: r.uploadedAt || '',
      count: r.count || 0,
      uniquePairs: r.uniquePairs || 0,
      uniqueProjects: r.uniqueProjects || 0,
    }
  } catch (e) {
    setAbilityMsg(`❌ 读取能力表状态失败: ${(e as Error).message}`, 'text-red-600')
  }
}

/** 选择文件 → 解析 → 上传。已上传时即"直接替换"，无需先清除。 */
async function onAbilityFileChange(e: Event): Promise<void> {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  target.value = '' // 清空 value，允许再次选择同一个文件
  if (!file) return

  abilityBusy.value = true
  setAbilityMsg('正在解析能力表...', 'text-gray-500')
  try {
    const parsed = await parseAbilityWorkbook(await file.arrayBuffer())
    setAbilityMsg(`已解析「${parsed.sheetName}」${parsed.items.length} 条（${parsed.how}；跳过空行 ${parsed.skipped} 行），正在保存...`, 'text-gray-500')

    const r = await personnelApi.uploadAbility(file.name, parsed.items)
    if (!r.success) {
      setAbilityMsg(`❌ ${r.error || '能力表保存失败'}（已保留上一次生效的能力表）`, 'text-red-600')
      return
    }
    ability.value = {
      loaded: !!r.loaded,
      fileName: r.fileName || file.name,
      uploadedAt: r.uploadedAt || '',
      count: r.count || 0,
      uniquePairs: r.uniquePairs || 0,
      uniqueProjects: r.uniqueProjects || 0,
    }
    setAbilityMsg(
      `✅ 能力表已保存：${ability.value.fileName}（条目 ${ability.value.count} 条 / 唯一组合 ${ability.value.uniquePairs} / 唯一项目 ${ability.value.uniqueProjects}）`,
      'text-green-600',
    )
    // 卡1 已查过就自动重跑，让比对列立刻反映最新能力表
    if (hasSearched1.value && currentName.value) await doQuery()
  } catch (err) {
    setAbilityMsg(`❌ ${(err as Error).message}（已保留上一次生效的能力表）`, 'text-red-600')
  } finally {
    abilityBusy.value = false
  }
}

async function clearAbility(): Promise<void> {
  if (!ability.value.loaded) return
  abilityBusy.value = true
  try {
    const r = await personnelApi.clearAbility()
    if (!r.success) {
      setAbilityMsg(`❌ ${r.error || '清除能力表失败'}`, 'text-red-600')
      return
    }
    ability.value = { loaded: false, fileName: '', uploadedAt: '', count: 0, uniquePairs: 0, uniqueProjects: 0 }
    setAbilityMsg('✅ 能力表已清除，卡1 不再显示比对列', 'text-green-600')
    if (hasSearched1.value && currentName.value) await doQuery()
  } catch (e) {
    setAbilityMsg(`❌ 清除失败: ${(e as Error).message}`, 'text-red-600')
  } finally {
    abilityBusy.value = false
  }
}

/** 是否展示/导出比对列：已上传能力表且后端确实给出了判定 */
const abilityColumn = computed(() => hasAbilityColumn(rows1.value, ability.value.loaded))

/* ==================== 生命周期 ==================== */
onMounted(() => {
  void loadAbilityStatus()
})
</script>

<template>
  <div class="v-personnel min-h-screen">
    <!-- 顶部横幅 -->
    <div class="bg-hero text-white px-6 py-8">
      <div class="max-w-4xl mx-auto">
        <h1 class="text-2xl font-bold">🧪 人员能力表梳理</h1>
        <p class="mt-2 text-white/90 text-sm">输入检测人员姓名，基于本地 SQLite 镜像查询其登记的检测项目与标准，去重后导出 Excel</p>
        <div class="mt-4 flex items-center gap-5 text-xs">
          <span class="flex items-center gap-2"><span class="dot" :class="mirrorStatus === '已就绪' ? 'dot-on' : 'dot-off'"></span>数据来源：本地 SQLite 镜像 <span class="text-white/80">{{ mirrorStatus }}</span></span>
        </div>
      </div>
    </div>

    <div class="max-w-5xl mx-auto px-6 py-6 space-y-6">
      <!-- ==================== 能力表：上传 / 替换 / 清除（按键常驻页面）==================== -->
      <div class="rounded-2xl border-2 border-indigo-100 bg-gradient-to-br from-indigo-50 to-white overflow-hidden">
        <div class="px-5 py-3 bg-indigo-600 text-white flex items-center gap-2 text-sm font-bold">
          <span class="text-base">📋</span> 检验检测能力表（比对「检测项目 + 检测标准」是否在能力范围内）
        </div>
        <div class="p-5">
          <div class="flex flex-wrap items-center gap-3">
            <button
              :disabled="abilityBusy"
              class="btn bg-indigo-600 text-white px-6 py-3 rounded-xl text-sm font-semibold"
              @click="abilityFileInput?.click()"
            >
              {{ abilityBusy ? '⏳ 处理中...' : (ability.loaded ? '🔄 替换能力表（.xlsx）' : '📊 上传能力表（.xlsx）') }}
            </button>
            <button
              :disabled="abilityBusy || !ability.loaded"
              class="btn bg-gray-500 text-white px-6 py-3 rounded-xl text-sm font-semibold"
              @click="clearAbility"
            >
              🧹 清除
            </button>
            <input ref="abilityFileInput" type="file" accept=".xlsx" class="hidden" @change="onAbilityFileChange" />
            <span class="text-xs text-gray-600">已上传时按钮变为「替换能力表」——直接选新文件即覆盖，无需先清除。</span>
          </div>

          <!-- 状态区：完整文件名（不截断）+ 上传时间 / 条目数 -->
          <div class="mt-4 rounded-xl border border-indigo-100 bg-white px-4 py-3 text-sm">
            <template v-if="ability.loaded">
              <div class="flex items-start gap-2">
                <span class="text-gray-500 shrink-0">当前能力表：</span>
                <span class="ability-file-name font-semibold text-gray-900" :title="ability.fileName">{{ ability.fileName }}</span>
              </div>
              <div class="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-600">
                <span>上传时间：{{ ability.uploadedAt || '—' }}</span>
                <span>条目数：{{ ability.count }}</span>
                <span>唯一组合：{{ ability.uniquePairs }}</span>
                <span>唯一项目：{{ ability.uniqueProjects }}</span>
              </div>
            </template>
            <div v-else class="text-gray-600">尚未上传能力表 —— 上传后卡1 的查询结果与导出 Excel 才会出现「是否在能力表」列。</div>
          </div>

          <div v-if="abilityMsg.text" class="mt-3 text-sm" :class="abilityMsg.cls">{{ abilityMsg.text }}</div>
        </div>
      </div>

      <!-- ==================== 模板卡1：按姓名查项目/标准 ==================== -->
      <div class="rounded-2xl border-2 border-blue-100 bg-gradient-to-br from-blue-50 to-white overflow-hidden">
        <div class="px-5 py-3 bg-blue-600 text-white flex items-center gap-2 text-sm font-bold">
          <span class="text-base">①</span> 按检测人员姓名查询（查项目 + 标准）
        </div>
        <div class="p-5">
          <div class="flex gap-3">
            <input
              v-model="nameInput"
              type="text"
              placeholder="请输入姓名，如：曹佳、顾蕴倩"
              autocomplete="off"
              class="flex-1 px-4 py-3 border-2 border-gray-200 rounded-xl text-sm"
              @keydown.enter="doQuery"
            />
            <button
              :disabled="querying1"
              class="btn bg-blue-600 text-white px-6 py-3 rounded-xl text-sm font-semibold"
              @click="doQuery"
            >
              {{ querying1 ? '⏳ 查询中...' : '🔍 查询' }}
            </button>
            <button
              :disabled="querying1 || !rows1.length"
              class="btn bg-green-600 text-white px-6 py-3 rounded-xl text-sm font-semibold"
              @click="exportExcel"
            >
              {{ querying1 ? '⏳ 导出中...' : '📥 导出Excel' }}
            </button>
          </div>
          <div class="mt-3 text-sm" :class="status1.cls">{{ status1.msg }}</div>
          <div class="mt-3 max-h-80 overflow-auto">
            <div v-if="!rows1.length" class="empty-state">
              <div class="text-3xl mb-2">{{ empty1Icon }}</div>
              <div>{{ empty1 }}</div>
            </div>
            <table v-else>
              <thead><tr><th class="w-14">序号</th><th>检测项目</th><th>检测标准</th><th v-if="abilityColumn" class="w-40">是否在能力表</th></tr></thead>
              <tbody>
                <tr v-for="(row, i) in rows1" :key="`${row.project}-${row.method}-${i}`" class="row-anim">
                  <td class="text-gray-500">{{ i + 1 }}</td>
                  <td class="font-medium">{{ row.project }}</td>
                  <td class="text-gray-700">{{ row.method }}</td>
                  <td v-if="abilityColumn" :class="abilityCellCls(row)">{{ abilityCellText(row) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- ==================== 模板卡2：按项目/标准查人员 ==================== -->
      <div class="rounded-2xl border-2 border-blue-100 bg-gradient-to-br from-blue-50 to-white overflow-hidden">
        <div class="px-5 py-3 bg-blue-600 text-white flex items-center gap-2 text-sm font-bold">
          <span class="text-base">②</span> 按检测项目或者检测标准查询检测人员
        </div>
        <div class="p-5">
          <div class="flex gap-3">
            <input
              v-model="kwInput"
              type="text"
              placeholder="请输入检测项目或检测标准关键词，如：克百威、GB 2763"
              autocomplete="off"
              class="flex-1 px-4 py-3 border-2 border-gray-200 rounded-xl text-sm"
              @keydown.enter="doProjectQuery"
            />
            <button
              :disabled="querying2"
              class="btn bg-blue-600 text-white px-6 py-3 rounded-xl text-sm font-semibold"
              @click="doProjectQuery"
            >
              {{ querying2 ? '⏳ 查询中...' : '🔍 查询' }}
            </button>
            <button
              :disabled="querying2 || !rows2.length"
              class="btn bg-green-600 text-white px-6 py-3 rounded-xl text-sm font-semibold"
              @click="exportProjectExcel"
            >
              {{ querying2 ? '⏳ 导出中...' : '📥 导出Excel' }}
            </button>
          </div>
          <div class="mt-3 text-sm" :class="status2.cls">{{ status2.msg }}</div>
          <div class="mt-3 max-h-80 overflow-auto">
            <div v-if="!rows2.length" class="empty-state">
              <div class="text-3xl mb-2">{{ empty2Icon }}</div>
              <div>{{ empty2 }}</div>
            </div>
            <table v-else>
              <thead><tr><th class="w-14">序号</th><th>检测项目</th><th>检测标准</th><th>检测人员</th><th>最后登记日期</th></tr></thead>
              <tbody>
                <tr v-for="(row, i) in rows2" :key="`${row.project}-${row.method}-${row.person}-${i}`" class="row-anim">
                  <td class="text-gray-500">{{ i + 1 }}</td>
                  <td class="font-medium">{{ row.project }}</td>
                  <td class="text-gray-700">{{ row.method }}</td>
                  <td class="text-gray-800">{{ row.person }}</td>
                  <td class="text-gray-600">{{ row.lastDate || '' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- 导出说明 -->
      <div class="mt-2 text-xs text-gray-400 leading-6">
        <p>💡 说明：结果均来自本地 SQLite 镜像，按「检测项目 + 检测标准」组合去重（卡2再加检测人员），按检测项目升序排列。</p>
        <p>📌 项目名会去除 ﹡△ 前缀和括号简写/别名，化学结构与"包括"合并组保留。</p>
        <p>📋 能力表比对口径：检测项目与检测标准「同时命中」才算「在」；先精确匹配，未命中再做宽松匹配（忽略标准年号版本差异、忽略项目名括号别名），宽松命中会在括号内注明差异原因，便于人工复核。</p>
      </div>
    </div>
  </div>
</template>

<style>
/* 原样式完整保留，仅加 `.v-personnel` 前缀做隔离；body 规则改挂根容器 */
.v-personnel {
  font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif;
  background: #f9fafb;
  /* 本页是浅色底，必须显式声明字色：全局 body 是深色主题（近白色 #f1f5f9），
     不覆盖会出现"浅底白字"（输入框里输入的内容几乎看不见）。 */
  color: #111827;
  min-height: 100vh;
}
.v-personnel { --primary:#2563eb; --success:#059669; --danger:#dc2626; }
.v-personnel .bg-hero { background: linear-gradient(135deg, #2563eb 0%, #7c3aed 100%); }
.v-personnel .card { background:#fff; border-radius:16px; box-shadow:0 4px 20px rgba(0,0,0,0.06); border:1px solid #eef2f7; }
.v-personnel .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
.v-personnel .dot-on { background:#059669; box-shadow:0 0 0 3px rgba(5,150,105,0.15); }
.v-personnel .dot-off { background:#cbd5e1; }
.v-personnel .btn { transition:all .2s; cursor:pointer; }
.v-personnel .btn:hover { transform:translateY(-1px); box-shadow:0 4px 12px rgba(37,99,235,0.25); }
.v-personnel .btn:active { transform:translateY(0); }
.v-personnel .btn:disabled { opacity:.5; cursor:not-allowed; transform:none; }
.v-personnel input { color:#111827; background:#fff; }
.v-personnel input::placeholder { color:#9ca3af; }
.v-personnel input:focus { outline:none; border-color:var(--primary); box-shadow:0 0 0 3px rgba(37,99,235,0.15); }
/* 能力表文件名：允许换行、不截断，长文件名（含空格/括号/日期）逐字可见 */
.v-personnel .ability-file-name { word-break:break-all; }
.v-personnel table { width:100%; border-collapse:collapse; font-size:13px; }
.v-personnel th { background:#f3f4f6; color:#374151; font-weight:600; text-align:left; padding:10px 14px; position:sticky; top:0; }
.v-personnel td { padding:9px 14px; border-bottom:1px solid #f1f5f9; color:#111827; }
.v-personnel tr:hover td { background:#f8fafc; }
.v-personnel .row-anim { animation:fadeIn .3s ease; }
@keyframes fadeIn { from{opacity:0; transform:translateY(4px);} to{opacity:1; transform:translateY(0);} }
.v-personnel .empty-state { padding:40px 20px; text-align:center; color:#9ca3af; }
.v-personnel ::-webkit-scrollbar { width:8px; height:8px; }
.v-personnel ::-webkit-scrollbar-thumb { background:#d1d5db; border-radius:4px; }
</style>
