<script setup lang="ts">
/**
 * 表头列配置：**当前任务当年那张表的列**（与使用方考核表的列一一对应）。
 *
 * 为什么按「任务 × 年份」隔离：两个任务（市例行 / 市监督）的指标项并不相同，
 * 且考核表每年会调整一次；新年份可以一键复制上一年的列再改，其它年份的历史数据不受影响。
 *
 * 每一列有两个要点：
 * - **匹配关键词**：这一列接住哪些样品（如「农产品-豇豆、芹菜、辣椒」写 豇豆,豆角,芹菜,辣椒,青椒,甜椒）。
 *   样品名先精确、再去括号后缀、再看样品类别，最后才是「其他XX」兜底桶；
 * - **大类**（农产品/畜产品/水产品）：决定它在「按产品类别的总完成率」里归到哪一行，也是兜底桶的判定依据。
 *
 * 保存即生效：每次保存都会**自动重算归类**（本地秒级，不重连镜像），看板数字随之更新。
 */
import { computed, onMounted, ref, watch } from 'vue'
import {
  ArrowDown, ArrowUp, Copy, Layers, Plus, RefreshCw, Save, Trash2, Wand2,
} from 'lucide-vue-next'
import {
  progressApi, type ProgressBigKind, type ProgressCategoryCfg, type ProgressScheme,
  type ProgressTaskType,
} from '../../api/client'

const props = defineProps<{ year: number; taskTypeId: number; tasks: ProgressTaskType[] }>()
const emit = defineEmits<{ changed: [msg: string] }>()

const bigKinds = ref<ProgressBigKind[]>([])
const categories = ref<ProgressCategoryCfg[]>([])
const taskName = ref('')
const schemes = ref<ProgressScheme[]>([])
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')

/** 其它任务（用于「从另一个任务复制列」） */
const otherTasks = computed(() => props.tasks.filter((t) => t.id !== props.taskTypeId))

/** 行内草稿：`{关键词}` / `{名称,大类}`，未改动的不在表里 */
const kwDraft = ref<Record<number, string>>({})
const catDraft = ref<Record<number, { name: string; big_kind: string }>>({})
const bulkText = ref('')
const bulkOpen = ref(false)
const bulkPreview = ref<{
  items: { big_kind: string; name: string; products: string[]; keywords: string }[]
  errors: { line: number; reason: string; text: string }[]
} | null>(null)

const anyDirty = computed(() => Object.keys(kwDraft.value).length > 0 || Object.keys(catDraft.value).length > 0)
const grandDone = computed(() => categories.value.reduce((a, c) => a + c.done_total, 0))

async function load(): Promise<void> {
  if (!props.taskTypeId) return
  loading.value = true
  error.value = ''
  try {
    const r = await progressApi.config(props.year, props.taskTypeId)
    bigKinds.value = r.big_kinds
    categories.value = r.categories
    taskName.value = r.task?.name || ''
    schemes.value = r.schemes || []
    kwDraft.value = {}
    catDraft.value = {}
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

/** 复制列方案：默认从上一年的同一任务；也可指定「从另一个任务」（同一年） */
async function copyScheme(fromYear: number, fromTask?: number): Promise<void> {
  if (categories.value.length
      && !window.confirm(`当前任务的表已有 ${categories.value.length} 列，复制会**整体覆盖**。确定继续？`)) return
  busy.value = true
  notice.value = ''
  try {
    const r = await progressApi.saveScheme({
      year: props.year,
      task_type_id: props.taskTypeId,
      from_year: fromYear,
      ...(fromTask ? { from_task_type_id: fromTask } : {}),
    })
    await recalc(r.message || '列方案已复制')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

/** 保存后自动重算归类，让看板立刻反映新口径 */
async function recalc(tip: string): Promise<void> {
  const r = await progressApi.reclassify(props.year)
  notice.value = `${tip}；已按新配置重算 ${r.updated} 条，看板数据已更新`
  emit('changed', notice.value)
  await load()
}

async function saveCategory(c: ProgressCategoryCfg, payload: { name?: string; big_kind?: string; keywords?: string }): Promise<void> {
  busy.value = true
  try {
    await progressApi.saveCategory({ id: c.id, name: payload.name ?? c.name, ...payload })
    const kw = { ...kwDraft.value }
    delete kw[c.id]
    kwDraft.value = kw
    const cd = { ...catDraft.value }
    delete cd[c.id]
    catDraft.value = cd
    await recalc(`「${payload.name ?? c.name}」已保存`)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function addCategory(): Promise<void> {
  const name = window.prompt('新增品类名称（直接写考核表里的列名，如「农产品-黄瓜」）：')
  if (!name) return
  const big = name.includes('-') ? name.split('-')[0] : name.includes('—') ? name.split('—')[0] : ''
  try {
    await progressApi.saveCategory({
      name,
      big_kind: big,
      is_other: name.includes('其他'),
      keywords: name.split(/[-—]/).slice(1).join('-') || name,
      scheme_id: schemes.value.find((s) => s.task_type_id === props.taskTypeId)?.id,
    })
    await recalc(`品类「${name}」已新增`)
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function toggle(c: ProgressCategoryCfg): Promise<void> {
  try {
    await progressApi.saveCategory({ id: c.id, name: c.name, enabled: !c.enabled })
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function remove(c: ProgressCategoryCfg): Promise<void> {
  if (!window.confirm(`删除品类「${c.name}」？若已录入过任务量，系统会拒绝并提示改为停用。`)) return
  try {
    const r = await progressApi.deleteCategory(c.id)
    await recalc(r.message)
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function move(c: ProgressCategoryCfg, dir: 'up' | 'down'): Promise<void> {
  try {
    await progressApi.moveCategory(c.id, dir)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function reclassify(): Promise<void> {
  busy.value = true
  notice.value = ''
  try {
    await recalc('已按当前配置重算归类')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function previewBulk(): Promise<void> {
  try {
    const r = await progressApi.bulkDefineProductTypes(bulkText.value, true, props.year, props.taskTypeId)
    bulkPreview.value = { items: r.items, errors: r.errors }
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function applyBulk(): Promise<void> {
  busy.value = true
  try {
    const r = await progressApi.bulkDefineProductTypes(bulkText.value, false, props.year, props.taskTypeId)
    await recalc(r.message || '批量定义完成')
    bulkOpen.value = false
    bulkPreview.value = null
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function saveBigKind(b: ProgressBigKind, keywords: string): Promise<void> {
  try {
    await progressApi.saveBigKind({ name: b.name, keywords })
    await recalc(`大类「${b.name}」关键词已保存`)
  } catch (e) {
    error.value = (e as Error).message
  }
}

onMounted(load)
watch(() => [props.year, props.taskTypeId], load)
</script>

<template>
  <div class="space-y-4">
    <div class="glass-card flex flex-wrap items-center gap-3 px-5 py-4">
      <span class="rounded-lg bg-white/5 px-3 py-1.5 text-[13.5px] font-bold text-ink">
        任务「{{ taskName }}」{{ year }} 年的表头列
      </span>
      <button class="tech-btn" @click="addCategory"><Plus :size="15" />新增列</button>
      <button class="tech-btn" @click="bulkOpen = !bulkOpen"><Wand2 :size="15" />批量定义</button>
      <button class="tech-btn" :disabled="busy" @click="copyScheme(year - 1)">
        <Copy :size="15" />复制 {{ year - 1 }} 年
      </button>
      <button
        v-for="t in otherTasks"
        :key="'copy' + t.id"
        class="tech-btn"
        :disabled="busy"
        :title="`用「${t.name}」${year} 年的列整体覆盖本任务`"
        @click="copyScheme(year, t.id)"
      >
        <Copy :size="15" />复制自「{{ t.name }}」
      </button>
      <button class="tech-btn" :disabled="busy" @click="reclassify">
        <RefreshCw :size="15" :class="busy ? 'animate-spin' : ''" />重算归类
      </button>
      <span class="ml-auto text-[12px] text-ink-muted">
        {{ categories.length }} 列 · 完成量合计 {{ grandDone }}
        <span v-if="anyDirty" class="ml-2 text-warn">有未保存的改动</span>
      </span>
    </div>

    <div v-if="error" class="glass-card border-err/40 bg-err/10 px-5 py-3 text-[13px] text-err">{{ error }}</div>
    <div v-if="notice" class="glass-card border-ok/40 bg-ok/10 px-5 py-3 text-[13px] text-ok">{{ notice }}</div>

    <!-- 批量定义 -->
    <div v-if="bulkOpen" class="glass-card px-5 py-4">
      <h2 class="panel-title">批量定义（每行一条，可直接粘考核表的列名清单）</h2>
      <p class="mb-2 text-[12px] text-ink-muted">
        格式：<code>大类-品类名[：关键词|关键词]</code>；名称含「其他」的自动按兜底桶处理。
      </p>
      <textarea
        v-model="bulkText"
        rows="8"
        class="w-full rounded-lg border border-white/15 bg-white/5 px-3 py-2 font-mono text-[12.5px] text-ink outline-none focus:border-primary"
        placeholder="农产品-豇豆、芹菜、辣椒：豇豆|豆角|芹菜|辣椒|青椒|甜椒&#10;农产品-其他蔬菜：蔬菜|蔬果&#10;水产品—其他水产：水产|鱼类|虾蟹类"
      />
      <div class="mt-2 flex flex-wrap items-center gap-3">
        <button class="tech-btn" @click="previewBulk">预览解析</button>
        <button class="tech-btn" :disabled="busy || !bulkText.trim()" @click="applyBulk">写入并重算</button>
        <span v-if="bulkPreview" class="text-[12px] text-ink-muted">
          解析出 {{ bulkPreview.items.length }} 个品类，{{ bulkPreview.errors.length }} 处问题
        </span>
      </div>
      <div v-if="bulkPreview" class="mt-3 space-y-1 text-[12.5px]">
        <p v-for="it in bulkPreview.items" :key="it.name" class="text-ink">
          <span class="text-ink-muted">[{{ it.big_kind }}]</span> {{ it.name }}
          <span v-if="it.keywords" class="text-ink-muted">（关键词：{{ it.keywords }}）</span>
        </p>
        <p v-for="er in bulkPreview.errors" :key="er.line" class="text-err">第 {{ er.line }} 行：{{ er.reason }} —— {{ er.text }}</p>
      </div>
    </div>

    <div v-if="loading && !categories.length" class="glass-card px-6 py-14 text-center">
      <div class="loader" />
      <p class="text-sm text-ink-muted">正在读取配置…</p>
    </div>

    <!-- 品类表 -->
    <div class="glass-card overflow-hidden">
      <table class="w-full text-[12.5px]">
        <thead>
          <tr class="text-ink-muted">
            <th class="w-[240px] px-4 py-2.5 text-left font-medium">品类（表头列名）</th>
            <th class="w-[110px] px-3 py-2.5 text-left font-medium">大类</th>
            <th class="px-3 py-2.5 text-left font-medium">匹配关键词（样品名 / 样品类别，逗号或竖线分隔）</th>
            <th class="w-[90px] px-3 py-2.5 text-right font-medium">完成量</th>
            <th class="w-[90px] px-3 py-2.5 text-right font-medium">任务量</th>
            <th class="w-[190px] px-3 py-2.5 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in categories" :key="c.id" class="border-t border-white/10">
            <td class="px-4 py-2">
              <template v-if="catDraft[c.id]">
                <input
                  v-model="catDraft[c.id].name"
                  class="w-full rounded-md border border-white/20 bg-white/5 px-2 py-1 text-[12.5px] text-ink outline-none focus:border-primary"
                />
              </template>
              <template v-else>
                <span class="font-semibold text-ink">{{ c.name }}</span>
                <span v-if="c.is_other" class="ml-1 text-warn" title="兜底桶：接住本大类里没被具体列接住的样品">*</span>
                <span v-if="!c.enabled" class="ml-1 text-[11px] text-warn">已停用</span>
              </template>
            </td>
            <td class="px-3 py-2">
              <select
                v-if="catDraft[c.id]"
                v-model="catDraft[c.id].big_kind"
                class="w-full rounded-md border border-white/20 bg-white/5 px-1.5 py-1 text-[12px] text-ink outline-none focus:border-primary"
              >
                <option value="">未分大类</option>
                <option v-for="b in bigKinds" :key="b.name" :value="b.name">{{ b.name }}</option>
              </select>
              <span v-else class="text-ink-muted">{{ c.big_kind || '—' }}</span>
            </td>
            <td class="px-3 py-2">
              <div class="flex items-center gap-2">
                <input
                  :value="kwDraft[c.id] ?? c.keywords"
                  class="min-w-[260px] flex-1 rounded-md border bg-white/5 px-2 py-1 text-[12.5px] text-ink outline-none transition-colors focus:border-primary"
                  :class="kwDraft[c.id] !== undefined ? 'border-warn/70' : 'border-white/15'"
                  placeholder="如：豇豆,豆角,芹菜,辣椒"
                  @input="kwDraft = { ...kwDraft, [c.id]: ($event.target as HTMLInputElement).value }"
                />
                <span class="shrink-0 text-[11px] text-ink-muted">{{ c.key_count }} 键</span>
                <button
                  v-if="kwDraft[c.id] !== undefined || catDraft[c.id]"
                  class="tech-btn"
                  :disabled="busy"
                  @click="saveCategory(c, { name: catDraft[c.id]?.name, big_kind: catDraft[c.id]?.big_kind, keywords: kwDraft[c.id] })"
                >
                  <Save :size="13" />保存
                </button>
              </div>
            </td>
            <td class="px-3 py-2 text-right tabular-nums text-ink">{{ c.done_total }}</td>
            <td class="px-3 py-2 text-right tabular-nums text-ink-muted">{{ c.quota_total }}</td>
            <td class="px-3 py-2 text-right">
              <span class="inline-flex gap-1.5">
                <button class="icon-btn" title="上移" @click="move(c, 'up')"><ArrowUp :size="13" /></button>
                <button class="icon-btn" title="下移" @click="move(c, 'down')"><ArrowDown :size="13" /></button>
                <button
                  class="icon-btn"
                  title="改名 / 改大类"
                  @click="catDraft = { ...catDraft, [c.id]: { name: c.name, big_kind: c.big_kind } }"
                >
                  编辑
                </button>
                <button class="icon-btn" @click="toggle(c)">{{ c.enabled ? '停用' : '启用' }}</button>
                <button class="icon-btn text-err" @click="remove(c)"><Trash2 :size="13" /></button>
              </span>
            </td>
          </tr>
          <tr v-if="!categories.length">
            <td colspan="6" class="px-4 py-3 text-[12.5px] text-warn">
              当前任务在 {{ year }} 年还没有列，看板不会有任何一列。
              常见做法：点上面「复制 {{ year - 1 }} 年」沿用去年的表头列，再按今年的考核表增删改；
              或点「批量定义」把考核表的列名整段粘进来。
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 大类关键词 -->
    <div class="glass-card px-5 py-4">
      <h2 class="panel-title inline-flex items-center gap-1.5">
        <Layers :size="15" />大类关键词（决定没被具体列接住的样品落到哪个兜底桶）
      </h2>
      <div v-for="b in bigKinds" :key="b.name" class="mt-3 flex flex-wrap items-center gap-2">
        <span class="w-[80px] font-bold text-ink">{{ b.name }}</span>
        <input
          :value="b.keywords"
          class="min-w-[320px] flex-1 rounded-md border border-white/15 bg-white/5 px-2 py-1 text-[12.5px] text-ink outline-none focus:border-primary"
          @change="saveBigKind(b, ($event.target as HTMLInputElement).value)"
        />
      </div>
      <p class="mt-3 text-[12px] text-ink-muted">
        判定顺序：水产 → 畜产 → 农产（避免「牛蛙」被判成畜产品）；这些词同时用于样品类别名与样品名。
        保存后会自动重算归类。
      </p>
    </div>
  </div>
</template>
