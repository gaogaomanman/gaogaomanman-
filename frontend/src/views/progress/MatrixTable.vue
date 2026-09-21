<script setup lang="ts">
/**
 * 进度大表：行 = 区域（县区市），列 = 品类，每列「任务量 / 完成量」，末尾合计与完成率。
 *
 * 只做**品类这一层**（使用方明确：只需要汇总的品类，不要产品类型）：
 * 每格取该区域该品类的合计，合计行取全区域列合计 —— 两者数据源不同，不能混用
 * （早期版本在汇总视图误用了全区域合计，导致每个区域显示同一个数，已修）。
 *
 * 表头整体吸顶、首列吸左：品类多时横向滚动仍然能看清是哪个区、哪一列。
 */
import { computed } from 'vue'
import type {
  ProgressCategoryCol,
  ProgressCategoryTotal,
  ProgressMatrixRow,
} from '../../api/client'

const props = defineProps<{
  categories: ProgressCategoryCol[]
  rows: ProgressMatrixRow[]
  categoryTotals: ProgressCategoryTotal[]
  grandTotal: { quota: number; done: number; rate: number | null }
}>()

const categoryTotalMap = computed(() => new Map(props.categoryTotals.map((t) => [t.category_id, t])))
const rowCatMaps = computed(
  () =>
    new Map(
      props.rows.map((r) => [r.region, new Map((r.category_cells ?? []).map((c) => [c.category_id, c]))]),
    ),
)

/** 某区域某品类的格子（**本区域**口径） */
function catCell(region: string, catId: number) {
  return rowCatMaps.value.get(region)?.get(catId) ?? { category_id: catId, quota: 0, done: 0, rate: null }
}

/** 合计行用全区域列合计 */
function catTotal(catId: number) {
  return categoryTotalMap.value.get(catId) ?? { category_id: catId, quota: 0, done: 0, rate: null }
}

function rateClass(rate: number | null): string {
  if (rate === null) return 'text-ink-muted'
  if (rate >= 100) return 'text-ok'
  if (rate >= 80) return 'text-cyan'
  if (rate >= 60) return 'text-warn'
  return 'text-err'
}

function fmtRate(rate: number | null): string {
  return rate === null ? '—' : `${rate}%`
}

/** 0 也照实显示（与使用方原表一致，便于逐格核对）；视觉上靠颜色弱化 */
function num(v: number): string {
  return String(v ?? 0)
}

const hasQuota = computed(() => props.grandTotal.quota > 0)
</script>

<template>
  <div class="glass-card overflow-hidden">
    <div class="flex flex-wrap items-center justify-between gap-2 px-5 pt-5">
      <h2 class="panel-title mb-0">区域 × 品类 进度表</h2>
      <p class="text-[12px] text-ink-muted">
        每格两列：<span class="font-bold text-ink">任务量</span> ·
        <span class="font-bold text-ink">完成量</span>，右边是该行完成率；带 * 的列是兜底桶
      </p>
    </div>

    <p v-if="!hasQuota" class="empty-tip">
      当前统计范围还没有录入任务量，只有完成量——请到「任务量录入」页按品类填写任务量，完成率才有意义。
    </p>

    <div class="overflow-auto px-5 pb-5 pt-3" style="max-height: 72vh">
      <table class="border-separate border-spacing-0 text-[15px]">
        <thead class="sticky top-0 z-20">
          <tr>
            <th
              class="sticky left-0 z-30 min-w-[120px] border-b border-white/15 bg-[#241f52] px-3 py-2.5 text-left font-bold text-ink"
              rowspan="2"
            >
              县区市
            </th>
            <th
              v-for="cat in categories"
              :key="'c' + cat.id"
              class="border-b border-l border-white/15 bg-[#241f52] px-2.5 py-2.5 text-center font-bold text-ink"
              colspan="2"
              :title="`${cat.big_kind || ''}${cat.is_other ? '（兜底桶：接住本大类里没被具体列接住的样品）' : ''}`"
            >
              {{ cat.name }}<span v-if="cat.is_other" class="text-warn">*</span>
            </th>
            <th class="border-b border-l border-white/15 bg-[#241f52] px-2.5 py-2.5 text-center font-bold text-ink" colspan="2">
              合计
            </th>
            <th
              class="border-b border-l border-white/15 bg-[#241f52] px-2.5 py-2.5 text-center font-bold text-ink"
              rowspan="2"
            >
              完成率
            </th>
          </tr>
          <tr>
            <template v-for="cat in categories" :key="'m' + cat.id">
              <th class="border-b border-l border-white/15 bg-[#302a63] px-2 py-1.5 text-center text-[13px] font-medium text-ink-muted">
                任务量
              </th>
              <th class="border-b border-white/15 bg-[#302a63] px-2 py-1.5 text-center text-[13px] font-medium text-ink-muted">
                完成量
              </th>
            </template>
            <th class="border-b border-l border-white/15 bg-[#302a63] px-2 py-1.5 text-center text-[13px] text-ink-muted">任务量</th>
            <th class="border-b border-white/15 bg-[#302a63] px-2 py-1.5 text-center text-[13px] text-ink-muted">完成量</th>
          </tr>
        </thead>

        <tbody>
          <tr v-for="row in rows" :key="row.region" class="group">
            <td
              class="sticky left-0 z-10 whitespace-nowrap border-b border-white/10 bg-[#241f52] px-3 py-2 font-semibold text-ink transition-colors group-hover:bg-[#2d2765]"
            >
              {{ row.region }}
            </td>
            <template v-for="cat in categories" :key="'d' + cat.id">
              <td class="border-b border-l border-white/10 px-2 py-2 text-center tabular-nums text-ink-muted">
                {{ num(catCell(row.region, cat.id).quota) }}
              </td>
              <td
                class="border-b border-white/10 px-2 py-2 text-center font-bold tabular-nums transition-colors group-hover:bg-white/5"
                :class="rateClass(catCell(row.region, cat.id).rate)"
                :title="`完成率 ${fmtRate(catCell(row.region, cat.id).rate)}`"
              >
                {{ num(catCell(row.region, cat.id).done) }}
              </td>
            </template>
            <td class="border-b border-l border-white/10 px-2.5 py-2 text-center tabular-nums text-ink-muted">
              {{ num(row.quota) }}
            </td>
            <td class="border-b border-white/10 px-2.5 py-2 text-center font-extrabold tabular-nums text-ink">
              {{ num(row.done) }}
            </td>
            <td class="border-b border-l border-white/10 px-2.5 py-2 text-center font-bold tabular-nums" :class="rateClass(row.rate)">
              {{ fmtRate(row.rate) }}
            </td>
          </tr>

          <tr class="bg-white/5">
            <td class="sticky left-0 z-10 border-t border-white/20 bg-[#2d2765] px-3 py-2.5 font-extrabold text-ink">合计</td>
            <template v-for="cat in categories" :key="'t' + cat.id">
              <td class="border-t border-l border-white/20 px-2 py-2.5 text-center font-bold tabular-nums text-ink">
                {{ num(catTotal(cat.id).quota) }}
              </td>
              <td class="border-t border-white/20 px-2 py-2.5 text-center font-extrabold tabular-nums text-ink">
                {{ num(catTotal(cat.id).done) }}
              </td>
            </template>
            <td class="border-t border-l border-white/20 px-2.5 py-2.5 text-center font-extrabold tabular-nums text-ink">
              {{ grandTotal.quota }}
            </td>
            <td class="border-t border-white/20 px-2.5 py-2.5 text-center font-extrabold tabular-nums text-ink">
              {{ grandTotal.done }}
            </td>
            <td class="border-t border-l border-white/20 px-2.5 py-2.5 text-center font-extrabold tabular-nums" :class="rateClass(grandTotal.rate)">
              {{ fmtRate(grandTotal.rate) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
