<script setup lang="ts">
/**
 * 区域配置：看板的**行**由这里决定。
 *
 * 两个关键字段：
 * - `name`：行标题（县区市）；
 * - `keys`：地址/COUNTY 里可能出现的写法（如「虎丘区、苏州高新区」都归到「高新区」）。
 *   keys 既做精确匹配，也做**子串匹配**（地址含「吴中」就归吴中区），因此不要写会命中外地的短词
 *   （实测写「新区」会把南京「江北新区」算进苏州高新区）。
 *
 * 「未映射区域」是同步时判不出归属的样品——在这里一点即可挂到某个行，并立刻重算归类。
 */
import { computed, onMounted, ref } from 'vue'
import { AlertTriangle, ArrowDown, ArrowUp, MapPin, Plus, RefreshCw, Trash2 } from 'lucide-vue-next'
import { progressApi, type ProgressRegionConflict, type ProgressRegionRow } from '../../api/client'

const props = defineProps<{ year: number }>()
const emit = defineEmits<{ changed: [msg: string] }>()

const regions = ref<ProgressRegionRow[]>([])
const unmapped = ref<{ region_key: string; count: number }[]>([])
const keyDraft = ref<Record<string, string>>({})
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')

const targetPick = ref<Record<string, string>>({})
/** 区域判定优先项：county = 受检单位所在区县（默认）；address = 抽样地址 */
const priority = ref<'county' | 'address'>('county')
const conflicts = ref<ProgressRegionConflict[]>([])
const conflictTotal = ref(0)
const showConflicts = ref(false)

const regionNames = computed(() => regions.value.map((r) => r.name))

async function setPriority(next: 'county' | 'address'): Promise<void> {
  if (next === priority.value) return
  busy.value = true
  notice.value = ''
  try {
    const r = await progressApi.setRegionPriority(props.year, next)
    priority.value = next
    notice.value = r.message
    emit('changed', r.message)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    // 区域是**跨任务共用**的看板行，所以任务参数传 0（后端回落第一个任务），这里只用区域相关字段
    const r = await progressApi.config(props.year, 0)
    regions.value = r.regions
    unmapped.value = r.unmapped_regions
    priority.value = r.region_priority
    conflicts.value = r.region_conflicts?.items ?? []
    conflictTotal.value = r.region_conflicts?.total ?? 0
    keyDraft.value = {}
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function saveKeys(r: ProgressRegionRow, text: string): Promise<void> {
  busy.value = true
  try {
    await progressApi.saveRegion({ name: r.name, keys: text })
    const next = { ...keyDraft.value }
    delete next[r.name]
    keyDraft.value = next
    // 保存即重算：改完别名立刻生效，看板数字随之更新
    const res = await progressApi.reclassify(props.year)
    notice.value = `「${r.name}」的别名已保存并重算（更新 ${res.updated} 条），看板数据已更新`
    emit('changed', notice.value)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function toggle(r: ProgressRegionRow): Promise<void> {
  try {
    await progressApi.saveRegion({ name: r.name, enabled: !r.enabled })
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function remove(r: ProgressRegionRow): Promise<void> {
  if (!window.confirm(`删除区域「${r.name}」？若该行已录入任务量，系统会拒绝并提示改为停用。`)) return
  try {
    const res = await progressApi.deleteRegion(r.name)
    emit('changed', res.message)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function addRegion(): Promise<void> {
  const name = window.prompt('新增区域（看板行的名称，如「工业园区」）：')
  if (!name) return
  try {
    await progressApi.saveRegion({ name, keys: name })
    emit('changed', `区域「${name}」已新增`)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function move(r: ProgressRegionRow, dir: 'up' | 'down'): Promise<void> {
  const idx = regions.value.findIndex((x) => x.name === r.name)
  const swap = dir === 'up' ? idx - 1 : idx + 1
  if (swap < 0 || swap >= regions.value.length) return
  try {
    await progressApi.saveRegion({ name: r.name, sort_no: regions.value[swap].sort_no })
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function mapKey(regionKey: string): Promise<void> {
  const target = targetPick.value[regionKey]
  if (!target) {
    error.value = '请先选择要挂到哪个区域'
    return
  }
  busy.value = true
  try {
    const r = await progressApi.mapRegion(props.year, regionKey, target)
    notice.value = r.message
    emit('changed', r.message)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function reclassify(): Promise<void> {
  busy.value = true
  try {
    const r = await progressApi.reclassify(props.year)
    notice.value = `重算归类完成：更新 ${r.updated} 条，区域未映射 ${r.unmapped} 条`
    emit('changed', notice.value)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <div class="glass-card flex flex-wrap items-center gap-3 px-5 py-4">
      <button class="tech-btn" @click="addRegion"><Plus :size="15" />新增区域</button>
      <button class="tech-btn" :disabled="busy" @click="reclassify">
        <RefreshCw :size="15" :class="busy ? 'animate-spin' : ''" />重算归类
      </button>
      <span class="ml-auto text-[12px] text-ink-muted">{{ regions.length }} 个区域</span>
    </div>

    <!-- 区域判定优先项：COUNTY 与地址不一致时，以哪个为准 -->
    <div class="glass-card px-5 py-4">
      <div class="flex flex-wrap items-center gap-3">
        <h2 class="panel-title mb-0">区域判定优先</h2>
        <span class="inline-flex overflow-hidden rounded-lg border border-white/15">
          <button
            class="cursor-pointer px-3 py-1.5 text-[12.5px] font-bold transition-colors"
            :class="priority === 'county' ? 'bg-primary/80 text-white' : 'text-ink-muted hover:bg-white/10'"
            @click="setPriority('county')"
          >
            受检单位所在区县（COUNTY）
          </button>
          <button
            class="cursor-pointer px-3 py-1.5 text-[12.5px] font-bold transition-colors"
            :class="priority === 'address' ? 'bg-primary/80 text-white' : 'text-ink-muted hover:bg-white/10'"
            @click="setPriority('address')"
          >
            抽样地址
          </button>
        </span>
        <button
          v-if="conflictTotal"
          class="inline-flex cursor-pointer items-center gap-1 text-[12.5px] text-warn hover:underline"
          @click="showConflicts = !showConflicts"
        >
          <AlertTriangle :size="14" />
          {{ conflictTotal }} 条样品的两种判定不一致（点{{ showConflicts ? '收起' : '查看' }}）
        </button>
        <span v-else class="text-[12.5px] text-ok">两种判定没有冲突</span>
      </div>

      <div v-if="showConflicts && conflicts.length" class="mt-3 overflow-hidden rounded-lg border border-white/10">
        <table class="w-full text-[12.5px]">
          <thead>
            <tr class="text-ink-muted">
              <th class="px-3 py-2 text-left font-medium">按 COUNTY 判定</th>
              <th class="px-3 py-2 text-left font-medium">按抽样地址判定</th>
              <th class="w-[90px] px-3 py-2 text-right font-medium">样品数</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in conflicts" :key="c.county_region + c.addr_region" class="border-t border-white/10">
              <td class="px-3 py-1.5 text-ink">{{ c.county_region }}</td>
              <td class="px-3 py-1.5 text-ink">{{ c.addr_region }}</td>
              <td class="px-3 py-1.5 text-right tabular-nums text-warn">{{ c.count }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <p class="mt-2 text-[12px] leading-relaxed text-ink-muted">
        源库里"受检单位所在区县"与"抽样地址"有时不在同一个区（实测 2026 年 18 条），
        切换后系统会**本地重算**（不重连镜像），当前生效：
        <span class="text-ink">{{ priority === 'county' ? '受检单位所在区县' : '抽样地址' }}优先</span>。
      </p>
    </div>

    <div v-if="error" class="glass-card border-err/40 bg-err/10 px-5 py-3 text-[13px] text-err">{{ error }}</div>
    <div v-if="notice" class="glass-card border-ok/40 bg-ok/10 px-5 py-3 text-[13px] text-ok">{{ notice }}</div>

    <!-- 未映射区域 -->
    <div v-if="unmapped.length" class="glass-card px-5 py-4">
      <h2 class="panel-title inline-flex items-center gap-1.5">
        <MapPin :size="15" class="text-warn" />未映射区域（{{ unmapped.length }} 个 · 合计
        {{ unmapped.reduce((a, b) => a + b.count, 0) }} 条样品没进表）
      </h2>
      <table class="mt-2 w-full text-[12.5px]">
        <thead>
          <tr class="text-ink-muted">
            <th class="w-[260px] px-2 py-1.5 text-left font-medium">解析出的区域</th>
            <th class="w-[90px] px-2 py-1.5 text-right font-medium">样品数</th>
            <th class="px-2 py-1.5 text-left font-medium">挂到看板行</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in unmapped" :key="u.region_key" class="border-t border-white/10">
            <td class="px-2 py-1.5 text-ink">{{ u.region_key }}</td>
            <td class="px-2 py-1.5 text-right tabular-nums text-warn">{{ u.count }}</td>
            <td class="px-2 py-1.5">
              <div class="flex items-center gap-2">
                <select v-model="targetPick[u.region_key]" class="year-select w-[160px] py-1 text-[12.5px]">
                  <option value="">选择区域…</option>
                  <option v-for="n in regionNames" :key="n" :value="n">{{ n }}</option>
                </select>
                <button class="tech-btn" :disabled="busy" @click="mapKey(u.region_key)">挂载并重算</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="mt-2 text-[12px] text-ink-muted">
        「(空地址)」表示源库里受检单位地址与单位所在区县都为空，挂载前建议先核对源数据。
      </p>
    </div>

    <div v-else-if="!loading" class="glass-card px-5 py-3 text-[13px] text-ok">
      当前年份没有未映射区域，所有样品都能落到看板行上。
    </div>

    <!-- 区域列表 -->
    <div class="glass-card overflow-hidden">
      <table class="w-full text-[12.5px]">
        <thead>
          <tr class="text-ink-muted">
            <th class="w-[130px] px-4 py-2.5 text-left font-medium">区域（行标题）</th>
            <th class="px-3 py-2.5 text-left font-medium">匹配写法（地址/区县别名，逗号分隔）</th>
            <th class="w-[90px] px-3 py-2.5 text-right font-medium">完成量</th>
            <th class="w-[90px] px-3 py-2.5 text-right font-medium">任务量</th>
            <th class="w-[170px] px-3 py-2.5 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in regions" :key="r.name" class="border-t border-white/10">
            <td class="px-4 py-2 font-semibold text-ink">
              {{ r.name }}
              <span v-if="!r.enabled" class="ml-1 text-[11px] text-warn">已停用</span>
              <div class="text-[10px] font-normal text-ink-muted">{{ r.source === 'base' ? '基础' : r.source === 'auto' ? '自动' : '人工' }}</div>
            </td>
            <td class="px-3 py-2">
              <input
                :value="keyDraft[r.name] ?? r.keys"
                class="w-full rounded-md border bg-white/5 px-2 py-1 text-[12.5px] text-ink outline-none transition-colors focus:border-primary"
                :class="keyDraft[r.name] !== undefined ? 'border-warn/70' : 'border-white/15'"
                placeholder="如：虎丘区,苏州高新区"
                @input="keyDraft = { ...keyDraft, [r.name]: ($event.target as HTMLInputElement).value }"
                @change="saveKeys(r, ($event.target as HTMLInputElement).value)"
              />
            </td>
            <td class="px-3 py-2 text-right tabular-nums text-ink">{{ r.done_total }}</td>
            <td class="px-3 py-2 text-right tabular-nums text-ink-muted">{{ r.quota_total }}</td>
            <td class="px-3 py-2 text-right">
              <span class="inline-flex gap-1.5">
                <button class="icon-btn" @click="move(r, 'up')"><ArrowUp :size="13" /></button>
                <button class="icon-btn" @click="move(r, 'down')"><ArrowDown :size="13" /></button>
                <button class="icon-btn" @click="toggle(r)">{{ r.enabled ? '停用' : '启用' }}</button>
                <button class="icon-btn text-err" @click="remove(r)"><Trash2 :size="13" /></button>
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="glass-card px-5 py-3 text-[12.5px] text-ink-muted">
      keys 的匹配顺序：① 受检单位所在区县（COUNTY）**精确**命中 → ② 地址里**包含**某个 key →
      ③ 地址解析出的区县名精确命中。因此 keys 建议写完整地名，避免用「新区」这类会命中外地的短词。
    </p>
  </div>
</template>
