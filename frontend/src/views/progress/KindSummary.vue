<script setup lang="ts">
/**
 * 按产品类别的总完成率汇总（大类口径）。
 *
 * 形态对齐使用方的考核表：一行总览 + 农产品 / 畜产品 / 水产品 三行 + 合计行，
 * 列 = 任务量（批次）/ 完成量 / 未完成 / 完成率，底部队截止日期。
 *
 * 大类来自「品类配置」里的 `big_kind`（农产品 / 畜产品 / 水产品），因此把某个品类换大类，
 * 这张表随之变化，不需要改代码。
 */
import { computed } from 'vue'
import type { ProgressCategoryCol, ProgressCategoryTotal, ProgressOverview } from '../../api/client'

const props = defineProps<{ data: ProgressOverview }>()

interface Row {
  name: string
  quota: number
  done: number
  rest: number
  rate: number | null
}

const cats = computed<ProgressCategoryCol[]>(() => props.data.categories)
const totals = computed(() => new Map(props.data.category_totals.map((t) => [t.category_id, t])))

/** 品类合计 → 大类合计（保持大类在配置里的出现顺序：农产品 → 畜产品 → 水产品） */
const kindRows = computed<Row[]>(() => {
  const order: string[] = []
  const acc = new Map<string, { quota: number; done: number }>()
  cats.value.forEach((c) => {
    const t: ProgressCategoryTotal | undefined = totals.value.get(c.id)
    const kind = c.big_kind || '未分大类'
    if (!acc.has(kind)) {
      acc.set(kind, { quota: 0, done: 0 })
      order.push(kind)
    }
    const cur = acc.get(kind) as { quota: number; done: number }
    cur.quota += t?.quota ?? 0
    cur.done += t?.done ?? 0
  })
  return order.map((k) => {
    const v = acc.get(k) as { quota: number; done: number }
    return {
      name: k,
      quota: v.quota,
      done: v.done,
      rest: v.quota - v.done,
      rate: v.quota > 0 ? Math.round((v.done / v.quota) * 1000) / 10 : null,
    }
  })
})

const g = computed(() => props.data.grand_total)
const rest = computed(() => g.value.quota - g.value.done)

function fmtRate(rate: number | null): string {
  return rate === null ? '—' : `${rate}%`
}

function rateClass(rate: number | null): string {
  if (rate === null) return 'text-ink-muted'
  if (rate >= 100) return 'text-ok'
  if (rate >= 80) return 'text-cyan'
  if (rate >= 60) return 'text-warn'
  return 'text-err'
}

/** 完成率底色：与使用方表里"重点看完成率"的习惯一致，低于 90% 的行整行浅色提醒 */
function rateTint(rate: number | null): string {
  if (rate === null) return ''
  if (rate >= 90) return 'bg-ok/10'
  if (rate >= 80) return 'bg-cyan/10'
  return 'bg-warn/15'
}
</script>

<template>
  <div class="glass-card overflow-hidden">
    <div class="flex flex-wrap items-center justify-between gap-2 px-5 pt-5">
      <h2 class="panel-title mb-0">按产品类别的总完成率</h2>
      <span class="text-[12px] text-ink-muted">
        截止日期：{{ data.filters.cutoff || `${data.year} 年全年` }}
      </span>
    </div>

    <div class="px-5 pb-5 pt-3">
      <table class="w-full border-separate border-spacing-0 text-[13px]">
        <thead>
          <tr>
            <th class="border-b border-white/15 px-3 py-2.5 text-left font-bold text-ink">产品类别</th>
            <th class="border-b border-white/15 px-3 py-2.5 text-right font-bold text-ink">任务量（批次）</th>
            <th class="border-b border-white/15 px-3 py-2.5 text-right font-bold text-ink">完成量</th>
            <th class="border-b border-white/15 px-3 py-2.5 text-right font-bold text-ink">未完成</th>
            <th class="border-b border-white/15 px-3 py-2.5 text-right font-bold text-ink">完成率</th>
          </tr>
        </thead>
        <tbody>
          <!-- 总览行（使用方表里置顶的那一行） -->
          <tr class="bg-white/5">
            <td class="border-b border-white/10 px-3 py-2.5 font-extrabold text-ink">当前统计范围</td>
            <td class="border-b border-white/10 px-3 py-2.5 text-right font-extrabold tabular-nums text-ink">{{ g.quota }}</td>
            <td class="border-b border-white/10 px-3 py-2.5 text-right font-extrabold tabular-nums text-ink">{{ g.done }}</td>
            <td class="border-b border-white/10 px-3 py-2.5 text-right font-extrabold tabular-nums" :class="rest > 0 ? 'text-warn' : 'text-ok'">
              {{ rest }}
            </td>
            <td class="border-b border-white/10 px-3 py-2.5 text-right font-extrabold tabular-nums" :class="rateClass(g.rate)">
              {{ fmtRate(g.rate) }}
            </td>
          </tr>

          <tr v-for="r in kindRows" :key="r.name">
            <td class="border-b border-white/10 px-3 py-2 font-semibold text-ink">{{ r.name }}</td>
            <td class="border-b border-white/10 px-3 py-2 text-right tabular-nums text-ink">{{ r.quota }}</td>
            <td class="border-b border-white/10 px-3 py-2 text-right tabular-nums text-ink">{{ r.done }}</td>
            <td class="border-b border-white/10 px-3 py-2 text-right tabular-nums" :class="r.rest > 0 ? 'text-ink-muted' : 'text-ok'">
              {{ r.rest }}
            </td>
            <td class="border-b border-white/10 px-3 py-2 text-right font-bold tabular-nums" :class="[rateClass(r.rate), rateTint(r.rate)]">
              {{ fmtRate(r.rate) }}
            </td>
          </tr>

          <tr class="bg-white/5">
            <td class="border-t border-white/20 px-3 py-2.5 font-extrabold text-ink">合计</td>
            <td class="border-t border-white/20 px-3 py-2.5 text-right font-extrabold tabular-nums text-ink">{{ g.quota }}</td>
            <td class="border-t border-white/20 px-3 py-2.5 text-right font-extrabold tabular-nums text-ink">{{ g.done }}</td>
            <td class="border-t border-white/20 px-3 py-2.5 text-right font-extrabold tabular-nums text-ink">{{ rest }}</td>
            <td class="border-t border-white/20 px-3 py-2.5 text-right font-extrabold tabular-nums" :class="rateClass(g.rate)">
              {{ fmtRate(g.rate) }}
            </td>
          </tr>
        </tbody>
      </table>
      <p class="mt-2.5 text-[12px] leading-relaxed text-ink-muted">
        未完成 = 任务量 − 完成量（负数表示超额完成）；大类的归属在「品类与产品类型」页维护（农 / 畜 / 水产品）。
      </p>
    </div>
  </div>
</template>
