<script setup lang="ts">
/**
 * 未归类处理：把「没被任何列接住」的样品挂到某个产品类型，或加入忽略名单。
 *
 * 挂载 = 给那一列**追加一个关键词**（不是单条数据打补丁），所以一次挂载会把所有同名样品
 * （含带 `（KH）` 后缀的）一起接住，并且下次同步依然有效。
 * 水质 / 土壤 / 肥料 这类不参与考核的样品，用「忽略」从统计里排除，避免它们永远挂在未归类里。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { Ban, Link2, RefreshCw, Trash2 } from 'lucide-vue-next'
import { progressApi, type ProgressCategoryCfg } from '../../api/client'

const props = defineProps<{ year: number; taskTypeId: number }>()
const emit = defineEmits<{ changed: [msg: string] }>()

const categories = ref<ProgressCategoryCfg[]>([])
const unmatched = ref<{ name: string; count: number; category: string }[]>([])
const ignores = ref<{ name: string; note: string }[]>([])
const pick = ref<Record<string, number>>({})
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')

/** 下拉选项：按大类分组（optgroup），值 = 品类 id */
const grouped = computed(() => {
  const order: string[] = []
  const map = new Map<string, ProgressCategoryCfg[]>()
  categories.value
    .filter((c) => c.enabled)
    .forEach((c) => {
      const k = c.big_kind || '未分大类'
      if (!map.has(k)) {
        map.set(k, [])
        order.push(k)
      }
      ;(map.get(k) as ProgressCategoryCfg[]).push(c)
    })
  return order.map((k) => ({ name: k, items: map.get(k) as ProgressCategoryCfg[] }))
})

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const r = await progressApi.config(props.year, props.taskTypeId)
    categories.value = r.categories
    unmatched.value = r.unmatched
    ignores.value = r.ignore_names
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function assign(name: string): Promise<void> {
  const cid = pick.value[name]
  if (!cid) {
    error.value = '请先选择要挂到的品类'
    return
  }
  busy.value = true
  notice.value = ''
  try {
    const r = await progressApi.assignUnmatched(props.year, name, cid)
    notice.value = r.message
    emit('changed', r.message)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function ignore(name: string): Promise<void> {
  const note = window.prompt(`把「${name}」加入忽略名单（不参与统计），可填原因：`, '不参与考核') || ''
  try {
    const r = await progressApi.ignoreSample(name, note)
    notice.value = r.message
    emit('changed', r.message)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function unignore(name: string): Promise<void> {
  try {
    const r = await progressApi.removeIgnore(name)
    notice.value = r.message
    emit('changed', r.message)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

/** 一键把「水质/土壤/肥料」这类环境与投入品样品全部忽略 */
async function ignoreNonProduct(): Promise<void> {
  const targets = unmatched.value.filter((u) =>
    /水质|土壤|肥料|饲料|水$|环境/.test(u.name) ||
    /水质|土壤|肥料|饲料|环境/.test(u.category || ''),
  )
  if (!targets.length) {
    notice.value = '没有匹配到环境/投入品类样品'
    return
  }
  if (!window.confirm(`将忽略 ${targets.length} 个样品名（合计 ${targets.reduce((a, b) => a + b.count, 0)} 条）：\n` +
    targets.slice(0, 12).map((t) => t.name).join('、') + (targets.length > 12 ? ' …' : ''))) return
  busy.value = true
  try {
    for (const t of targets) await progressApi.ignoreSample(t.name, '环境/投入品，不参与考核')
    notice.value = `已忽略 ${targets.length} 个样品名`
    emit('changed', notice.value)
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
    notice.value = `重算归类完成：更新 ${r.updated} 条，仍未归类 ${r.unmatched} 条`
    emit('changed', notice.value)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

onMounted(load)
watch(() => [props.year, props.taskTypeId], load)
</script>

<template>
  <div class="space-y-4">
    <div class="glass-card flex flex-wrap items-center gap-3 px-5 py-4">
      <button class="tech-btn" :disabled="busy" @click="reclassify">
        <RefreshCw :size="15" :class="busy ? 'animate-spin' : ''" />重算归类
      </button>
      <button class="tech-btn" :disabled="busy || !unmatched.length" @click="ignoreNonProduct">
        <Ban :size="15" />忽略水质/土壤/肥料类
      </button>
      <span class="ml-auto text-[12px] text-ink-muted">
        未归类 {{ unmatched.length }} 个样品名 ·
        {{ unmatched.reduce((a, b) => a + b.count, 0) }} 条
      </span>
    </div>

    <div v-if="error" class="glass-card border-err/40 bg-err/10 px-5 py-3 text-[13px] text-err">{{ error }}</div>
    <div v-if="notice" class="glass-card border-ok/40 bg-ok/10 px-5 py-3 text-[13px] text-ok">{{ notice }}</div>

    <!-- 未归类清单 -->
    <div class="glass-card overflow-hidden">
      <h2 class="panel-title px-5 pt-4">未归类样品（不计入任何列，也不计入合计）</h2>
      <p class="px-5 pb-2 text-[12px] text-ink-muted">
        挂载会给目标列追加一个关键词，所有同名样品（含带括号后缀的）一起生效；挂载后无需重新同步。
      </p>
      <table class="w-full text-[12.5px]">
        <thead>
          <tr class="text-ink-muted">
            <th class="w-[260px] px-5 py-2.5 text-left font-medium">样品名称</th>
            <th class="w-[150px] px-3 py-2.5 text-left font-medium">源库样品类别</th>
            <th class="w-[90px] px-3 py-2.5 text-right font-medium">条数</th>
            <th class="px-3 py-2.5 text-left font-medium">挂到品类（追加关键词）</th>
            <th class="w-[150px] px-5 py-2.5 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in unmatched" :key="u.name" class="border-t border-white/10">
            <td class="px-5 py-2 font-semibold text-ink">{{ u.name }}</td>
            <td class="px-3 py-2 text-ink-muted">{{ u.category || '—' }}</td>
            <td class="px-3 py-2 text-right tabular-nums text-warn">{{ u.count }}</td>
            <td class="px-3 py-2">
              <select v-model.number="pick[u.name]" class="year-select w-full max-w-[420px] py-1 text-[12.5px]">
                <option :value="0">选择品类…</option>
                <optgroup v-for="g in grouped" :key="g.name" :label="g.name">
                  <option v-for="c in g.items" :key="c.id" :value="c.id">{{ c.name }}</option>
                </optgroup>
              </select>
            </td>
            <td class="px-5 py-2 text-right">
              <span class="inline-flex gap-1.5">
                <button class="icon-btn" :disabled="busy" @click="assign(u.name)"><Link2 :size="13" />挂载</button>
                <button class="icon-btn text-warn" @click="ignore(u.name)"><Ban :size="13" />忽略</button>
              </span>
            </td>
          </tr>
          <tr v-if="!unmatched.length && !loading">
            <td colspan="5" class="px-5 py-4 text-[13px] text-ok">
              没有未归类样品——当年镜像数据全部落到了配置的列上。
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 忽略名单 -->
    <div class="glass-card overflow-hidden">
      <h2 class="panel-title px-5 pt-4">忽略名单（{{ ignores.length }}）</h2>
      <p class="px-5 pb-2 text-[12px] text-ink-muted">
        名单里的样品名在统计时直接排除（水质、土壤、肥料、饲料等不参与考核的样品）。
      </p>
      <table class="w-full text-[12.5px]">
        <tbody>
          <tr v-for="g in ignores" :key="g.name" class="border-t border-white/10">
            <td class="w-[280px] px-5 py-2 font-semibold text-ink">{{ g.name }}</td>
            <td class="px-3 py-2 text-ink-muted">{{ g.note }}</td>
            <td class="w-[110px] px-5 py-2 text-right">
              <button class="icon-btn text-err" @click="unignore(g.name)"><Trash2 :size="13" />移出</button>
            </td>
          </tr>
          <tr v-if="!ignores.length">
            <td class="px-5 py-3 text-[12.5px] text-ink-muted">暂无忽略项。</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
