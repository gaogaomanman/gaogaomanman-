<script setup lang="ts">
/**
 * 合同管理：这里最关键的一件事是**把合同挂到任务**（市例行 / 市监督 / 未纳入）。
 *
 * 样品属于哪张考核表由它的合同决定：挂到「市例行」的合同，其样品进市例行那张表；
 * 没挂的（0 = 未纳入）不进任何表，只出现在看板的「未纳入清单」里提示。
 *
 * 清单来源是"两者结合"：同步时自动发现（source=auto），配置页可补备注、改启停，
 * 也能预先录入镜像里还没有的合同（source=manual）。自动发现的合同不能删除——
 * 删了下次同步又会出现，只能停用。
 */
import { computed, onMounted, ref } from 'vue'
import { FileSpreadsheet, Plus, RefreshCw, Trash2 } from 'lucide-vue-next'
import { progressApi, type ProgressContractRow, type ProgressTaskType } from '../../api/client'

const props = defineProps<{ year: number; tasks: ProgressTaskType[] }>()
const emit = defineEmits<{ changed: [msg: string] }>()

const rows = ref<ProgressContractRow[]>([])
const loading = ref(false)
const error = ref('')
const notice = ref('')
const noteDraft = ref<Record<string, string>>({})

/** 归并目标下拉的候选：所有合同编号；已作为正式合同的排在前面（不含自己，由模板里过滤） */
const mergeTargets = computed(() =>
  [...rows.value]
    .sort((a, b) => Number(!!a.alias_of) - Number(!!b.alias_of) || a.contract_no.localeCompare(b.contract_no))
    .map((r) => r.contract_no),
)
const mergedCount = computed(() => rows.value.filter((r) => r.alias_of).length)

async function setAlias(r: ProgressContractRow, target: string): Promise<void> {
  try {
    await progressApi.saveContract({ contract_no: r.contract_no, alias_of: target })
    notice.value = target
      ? `「${r.contract_no}」已并入「${target}」，看板与筛选都会合并统计`
      : `「${r.contract_no}」已取消归并`
    emit('changed', notice.value)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

/** 把合同挂到任务（或不纳入）；后端会顺手重算完成量的任务归属 */
async function setTask(r: ProgressContractRow, taskId: number): Promise<void> {
  try {
    await progressApi.saveContract({ contract_no: r.contract_no, task_type_id: taskId })
    const name = props.tasks.find((t) => t.id === taskId)?.name
    notice.value = name
      ? `「${r.contract_no}」已挂到任务「${name}」，完成量归属已重算`
      : `「${r.contract_no}」已设为「未纳入考核表」`
    emit('changed', notice.value)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    // 合同清单与任务无关（整年），任务参数只是为了让后端解析出「当前任务」用于其它字段
    const r = await progressApi.config(props.year, 0)
    rows.value = r.contracts
    noteDraft.value = {}
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function addContract(): Promise<void> {
  const no = window.prompt('合同编号（与达梦里的 CONTRACTS_NO 一致，如 PS2026008-市专项）：')
  if (!no) return
  const note = window.prompt('备注（可留空）：') || ''
  try {
    await progressApi.saveContract({ contract_no: no, note })
    emit('changed', `合同「${no}」已新增`)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function saveNote(r: ProgressContractRow, note: string): Promise<void> {
  try {
    await progressApi.saveContract({ contract_no: r.contract_no, note })
    notice.value = `「${r.contract_no}」备注已保存`
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function toggle(r: ProgressContractRow): Promise<void> {
  try {
    await progressApi.saveContract({ contract_no: r.contract_no, enabled: !r.enabled })
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function remove(r: ProgressContractRow): Promise<void> {
  if (!window.confirm(`删除合同「${r.contract_no}」？已被任务量引用或自动发现时会拒绝。`)) return
  try {
    const res = await progressApi.deleteContract(r.contract_no)
    emit('changed', res.message)
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <div class="glass-card flex flex-wrap items-center gap-3 px-5 py-4">
      <button class="tech-btn" @click="addContract"><Plus :size="15" />新增合同</button>
      <button class="tech-btn" @click="load"><RefreshCw :size="15" />刷新</button>
      <span class="ml-auto text-[12px] text-ink-muted">
        {{ rows.length }} 个合同{{ mergedCount ? `（${mergedCount} 个已归并）` : '' }}
      </span>
    </div>

    <div v-if="error" class="glass-card border-err/40 bg-err/10 px-5 py-3 text-[13px] text-err">{{ error }}</div>
    <div v-if="notice" class="glass-card border-ok/40 bg-ok/10 px-5 py-3 text-[13px] text-ok">{{ notice }}</div>

    <div class="glass-card overflow-hidden">
      <table class="w-full text-[12.5px]">
        <thead>
          <tr class="text-ink-muted">
            <th class="w-[260px] px-4 py-2.5 text-left font-medium">合同编号</th>
            <th class="w-[90px] px-3 py-2.5 text-left font-medium">来源</th>
            <th class="w-[150px] px-3 py-2.5 text-left font-medium">
              归属考核表（决定进哪张表）
            </th>
            <th class="w-[90px] px-3 py-2.5 text-right font-medium">完成量</th>
            <th class="w-[100px] px-3 py-2.5 text-right font-medium">任务量</th>
            <th class="px-3 py-2.5 text-left font-medium">备注（自己写给自己看）</th>
            <th class="w-[200px] px-3 py-2.5 text-left font-medium">归并到（拼写变体并入正式编号）</th>
            <th class="w-[130px] px-3 py-2.5 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.contract_no" class="border-t border-white/10">
            <td class="px-4 py-2 font-semibold text-ink">
              <span class="inline-flex items-center gap-1.5">
                <FileSpreadsheet :size="13" class="text-ink-muted" />{{ r.contract_no }}
              </span>
              <span v-if="!r.enabled" class="ml-2 text-[11px] text-warn">已停用</span>
            </td>
            <td class="px-3 py-2 text-ink-muted">{{ r.source === 'auto' ? '自动发现' : '人工录入' }}</td>
            <td class="px-3 py-2">
              <select
                :value="r.task_type_id || 0"
                class="year-select w-full py-1 text-[12.5px]"
                :class="r.task_type_id ? '' : 'text-warn'"
                @change="setTask(r, Number(($event.target as HTMLSelectElement).value))"
              >
                <option :value="0">未纳入考核表</option>
                <option v-for="t in tasks" :key="t.id" :value="t.id">{{ t.name }}</option>
              </select>
            </td>
            <td class="px-3 py-2 text-right tabular-nums text-ink">{{ r.done_total }}</td>
            <td class="px-3 py-2 text-right tabular-nums text-ink-muted">{{ r.quota_total }}</td>
            <td class="px-3 py-2">
              <input
                :value="noteDraft[r.contract_no] ?? r.note"
                class="w-full rounded-md border border-white/15 bg-white/5 px-2 py-1 text-[12.5px] text-ink outline-none focus:border-primary"
                placeholder="可留空"
                @input="noteDraft = { ...noteDraft, [r.contract_no]: ($event.target as HTMLInputElement).value }"
                @change="saveNote(r, ($event.target as HTMLInputElement).value)"
              />
            </td>
            <td class="px-3 py-2">
              <select
                :value="r.alias_of || ''"
                class="year-select w-full py-1 text-[12.5px]"
                @change="setAlias(r, ($event.target as HTMLSelectElement).value)"
              >
                <option value="">不归并（独立合同）</option>
                <option v-for="no in mergeTargets.filter((x) => x !== r.contract_no)" :key="no" :value="no">
                  {{ no }}
                </option>
              </select>
              <span v-if="r.alias_of" class="mt-0.5 block text-[11px] text-warn">已并入 {{ r.alias_of }}</span>
            </td>
            <td class="px-3 py-2 text-right">
              <span class="inline-flex gap-1.5">
                <button class="icon-btn" @click="toggle(r)">{{ r.enabled ? '停用' : '启用' }}</button>
                <button class="icon-btn text-err" @click="remove(r)"><Trash2 :size="13" /></button>
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length && !loading" class="empty-tip">还没有合同——请先到「数据看板」点一次「同步完成量」自动发现，或在这里手工新增。</p>
    </div>

    <p class="glass-card px-5 py-3 text-[12.5px] text-ink-muted">
      提示：同步只**自动新增**发现的合同编号，不会覆盖你写的备注与启停状态；
      停用的合同仍保留历史数据，只是不参与看板默认筛选。
    </p>
  </div>
</template>
