<script setup lang="ts">
/**
 * 「建设中」占位页。
 *
 * 存在的意义：模块功能已就绪但**口径还在与使用方核对**（兜底桶定义、区域归属差异等），
 * 若此时对外开放，使用者会拿看到的数字去汇报，而数字还可能变——那比"看不到"更糟。
 * 因此产品构建默认只显示本页；核对完成后由 `features.ts` 的开关一次性放开。
 */
import { ArrowLeft, Construction, ListChecks } from 'lucide-vue-next'

const DONE = [
  '表头结构：县区市 × 品类 → 产品类型 → 任务量 / 完成量，末尾合计与完成率',
  '完成量：按年从达梦镜像按「合同 × 区域 × 产品类型」统计去重样品数',
  '任务量：按合同录入（区域 × 产品类型），支持 Excel 导入导出',
  '品类 / 产品类型 / 区域别名 / 合同 均可在页面上增删改，表头随之变化',
  '未归类样品与未映射区域可一键处理，处理结果即时生效',
]

const PENDING = [
  '兜底桶口径（「其他蔬菜 / 其他禽蛋 / 其他水产」到底接哪些样品）与使用方逐一核对',
  '区域归属差异：源库按受检单位地址，考核表按任务分配区县，需确认以哪个为准',
  '完成后由管理员确认上线',
]
</script>

<template>
  <div class="min-h-screen px-5 pb-16 pt-7">
    <div class="mx-auto max-w-[900px] animate-fade-in-up">
      <div class="glass-card px-7 py-9 text-center">
        <div class="mb-3 inline-flex items-center gap-2 rounded-full bg-warn/20 px-4 py-1.5 text-[13px] font-bold text-warn">
          <Construction :size="16" />建设中
        </div>
        <h1
          class="bg-gradient-to-r from-[#60A5FA] via-[#A78BFA] to-[#F472B6] bg-clip-text text-[26px] font-extrabold tracking-wide text-transparent"
        >
          抽采样进度统计
        </h1>
        <p class="mx-auto mt-3 max-w-[620px] text-[13.5px] leading-relaxed text-ink-muted">
          本模块正在按使用方的考核表逐项核对口径与数字，<strong class="text-ink">暂不对外开放</strong>，
          以免在口径确认前产生需要返工的数字。其它工具不受影响，可正常使用。
        </p>
      </div>

      <div class="mt-5 grid grid-cols-1 gap-5 md:grid-cols-2">
        <div class="glass-card px-5 py-4">
          <h2 class="panel-title inline-flex items-center gap-1.5">
            <ListChecks :size="15" class="text-ok" />已完成
          </h2>
          <ul class="mt-2 space-y-1.5 text-[12.5px] leading-relaxed text-ink-muted">
            <li v-for="d in DONE" :key="d" class="flex gap-2">
              <span class="text-ok">✓</span><span>{{ d }}</span>
            </li>
          </ul>
        </div>

        <div class="glass-card px-5 py-4">
          <h2 class="panel-title inline-flex items-center gap-1.5">
            <Construction :size="15" class="text-warn" />待确认
          </h2>
          <ul class="mt-2 space-y-1.5 text-[12.5px] leading-relaxed text-ink-muted">
            <li v-for="p in PENDING" :key="p" class="flex gap-2">
              <span class="text-warn">•</span><span>{{ p }}</span>
            </li>
          </ul>
        </div>
      </div>

      <div class="mt-6 text-center">
        <a
          href="/"
          class="tech-btn inline-flex items-center gap-2"
        >
          <ArrowLeft :size="15" />返回导航首页
        </a>
      </div>
    </div>
  </div>
</template>
