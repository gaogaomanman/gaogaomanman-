<script setup lang="ts">
/**
 * 查询模板规则设置页（`/limsrules`）：LIMS 统计查询各模板的「别名映射」与「合并折算」规则配置。
 *
 * 规则范围（本次只做 A / B 两类）：
 * - A 类 项目名别名映射：数据库项目名 → 模板列名；全局表 + 模板表两层，**模板优先**；
 * - B 类 多项目合并：模板列名 ← 多个子项目 + 折算系数（数字或 分子/分母），可选合并方式
 *   与重复策略（分行标红 / 取最新 / 优先有值）；判重固定按「项目名」。
 *
 * 三条安全约束：
 * 1. 保存 = 新增一个版本号，**历史只增不删**；回滚也是"把旧版本内容记为新版本"；
 * 2. 未保存过规则的模板用后端内置默认（= 改造前硬编码常量），导出结果不变；
 * 3. 折算系数只允许数字或分数，非法值后端直接拒绝并返回可读原因。
 */
import { computed, onMounted, ref } from 'vue'
import { dmApi, limsRulesApi, type LimsRuleVersion } from '../api/client'
import { BUILTIN_DEFAULTS, TEMPLATE_LABELS, parseFactor, type CombineMode, type DupKey, type RuleGroup } from './dm/rules'

interface EditableRule {
  nameMap: { key: string; value: string }[]
  groups: RuleGroup[]
  defaultCombine: CombineMode
}

const TEMPLATE_ORDER = ['__global__', 'provinceAgri', 'provinceLivestock', 'provinceAquatic', 'yearlyStats']

const COMBINE_OPTIONS = [
  { value: 'sum', label: '加权求和（按系数）' },
  { value: 'first', label: '取首个检出值' },
  { value: 'max', label: '取最大值' },
  { value: 'min', label: '取最小值' },
]

const DUPPOLICY_OPTIONS = [
  { value: 'keepLines', label: '分行列出 + 标红（保留全部，默认）' },
  { value: 'preferReported', label: '优先取有报告值的；值不同则仍分行标红' },
  { value: 'latest', label: '取创建时间最新的一条' },
]

const loading = ref(false)
const saving = ref(false)
const message = ref('')
const errorMsg = ref('')

const activeId = ref('__global__')
const operator = ref(localStorage.getItem('lims_rules_operator') || '')
const versions = ref<LimsRuleVersion[]>([])
const editable = ref<EditableRule>({ nameMap: [], groups: [], defaultCombine: 'sum' })
const meta = ref({ version: 0, source: 'builtin', updatedAt: '', updatedBy: '', note: '' })

/** 折算示例：真实数据取样结果 */
const sampleRows = ref<{ project: string; value: string; sampleId: string }[]>([])
const sampleNote = ref('')
const sampleLoading = ref(false)

const isGlobal = computed(() => activeId.value === '__global__')
const builtinDef = computed(() => BUILTIN_DEFAULTS[activeId.value])

const dirtyCount = computed(() => editable.value.nameMap.filter((x) => !x.key.trim() && !x.value.trim()).length)

function toEditable(payload: Record<string, unknown>): EditableRule {
  const nameMapRaw = (payload.nameMap || {}) as Record<string, unknown>
  const def = (payload.defaultCombine as CombineMode) || 'sum'
  const groups = ((payload.groups || []) as RuleGroup[]).map((g) => ({
    target: g.target,
    members: (g.members || []).map((m) => ({ name: m.name, factor: String(m.factor ?? '1') })),
    combine: (g.combine || def) as CombineMode,
    // 判重固定按项目名；历史版本里若残留 group，也在读取时归一
    // （必须断言为 DupKey：直接写 'dn' 会被 TS 放宽成 string，赋值给 RuleGroup 会报错）
    dupKey: 'dn' as DupKey,
    dupPolicy: g.dupPolicy || 'keepLines',
  }))
  return {
    nameMap: Object.keys(nameMapRaw).map((k) => ({ key: k, value: String(nameMapRaw[k] ?? '') })),
    groups,
    defaultCombine: def,
  }
}

function toPayload(): Record<string, unknown> {
  const nameMap: Record<string, string> = {}
  editable.value.nameMap.forEach((x) => {
    const k = x.key.trim()
    const v = x.value.trim()
    if (k && v) nameMap[k] = v
  })
  return {
    nameMap,
    groups: editable.value.groups,
    defaultCombine: editable.value.defaultCombine,
  }
}

async function loadRule(templateId: string): Promise<void> {
  loading.value = true
  errorMsg.value = ''
  message.value = ''
  try {
    const r = await limsRulesApi.one(templateId)
    if (!r.success) {
      errorMsg.value = r.error || '读取规则失败'
      return
    }
    editable.value = toEditable(r.payload || {})
    meta.value = {
      version: Number(r.version || 0),
      source: r.source || 'builtin',
      updatedAt: r.updatedAt || '',
      updatedBy: r.updatedBy || '',
      note: r.note || '',
    }
    await loadVersions()
  } catch (e) {
    errorMsg.value = `读取规则失败：${(e as Error).message}`
  } finally {
    loading.value = false
  }
}

async function loadVersions(): Promise<void> {
  try {
    const r = await limsRulesApi.versions(activeId.value)
    versions.value = r.success ? r.versions || [] : []
  } catch {
    versions.value = []
  }
}

function switchTemplate(id: string): void {
  activeId.value = id
  sampleRows.value = []
  sampleNote.value = ''
  void loadRule(id)
}

function addNameMapRow(): void {
  editable.value.nameMap.unshift({ key: '', value: '' })
}

function removeNameMapRow(i: number): void {
  editable.value.nameMap.splice(i, 1)
}

function addGroup(): void {
  editable.value.groups.push({
    target: '',
    members: [{ name: '', factor: '1' }],
    combine: editable.value.defaultCombine,
    dupKey: 'dn',
    dupPolicy: 'keepLines',
  })
}

function removeGroup(i: number): void {
  editable.value.groups.splice(i, 1)
}

function addMember(g: RuleGroup): void {
  g.members.push({ name: '', factor: '1' })
}

function removeMember(g: RuleGroup, i: number): void {
  g.members.splice(i, 1)
}

async function save(): Promise<void> {
  saving.value = true
  errorMsg.value = ''
  message.value = ''
  try {
    const r = await limsRulesApi.save(activeId.value, toPayload(), `页面保存（v${meta.value.version} → 新版本）`, operator.value)
    if (!r.success) {
      errorMsg.value = r.error || '保存失败'
      return
    }
    localStorage.setItem('lims_rules_operator', operator.value)
    message.value = r.message || '保存成功'
    await loadRule(activeId.value)
  } catch (e) {
    errorMsg.value = `保存失败：${(e as Error).message}`
  } finally {
    saving.value = false
  }
}

async function resetDefault(): Promise<void> {
  if (!confirm('确定恢复该模板的内置默认规则？会记为一个新版本（可再回滚回来）。')) return
  errorMsg.value = ''
  try {
    const r = await limsRulesApi.reset(activeId.value, operator.value)
    if (!r.success) {
      errorMsg.value = r.error || '恢复默认失败'
      return
    }
    message.value = r.message || '已恢复内置默认'
    await loadRule(activeId.value)
  } catch (e) {
    errorMsg.value = `恢复默认失败：${(e as Error).message}`
  }
}

async function rollback(v: number): Promise<void> {
  if (!confirm(`确定回滚到 v${v}？回滚内容会记为一个新版本，历史不会丢失。`)) return
  errorMsg.value = ''
  try {
    const r = await limsRulesApi.activate(activeId.value, v, operator.value)
    if (!r.success) {
      errorMsg.value = r.error || '回滚失败'
      return
    }
    message.value = r.message || '回滚成功'
    await loadRule(activeId.value)
  } catch (e) {
    errorMsg.value = `回滚失败：${(e as Error).message}`
  }
}

function exportJson(): void {
  const data = {
    templateId: activeId.value,
    exportedAt: new Date().toISOString(),
    version: meta.value.version,
    payload: toPayload(),
  }
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `LIMS规则_${activeId.value}.json`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(a.href)
}

async function importJson(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const file = input.files && input.files[0]
  if (!file) return
  errorMsg.value = ''
  try {
    const text = await file.text()
    const obj = JSON.parse(text) as Record<string, unknown>
    const payload = (obj.payload || obj) as Record<string, unknown>
    editable.value = toEditable(payload)
    message.value = '已载入 JSON 内容，请核对后点「保存为新版本」'
  } catch (e) {
    errorMsg.value = `JSON 解析失败：${(e as Error).message}`
  } finally {
    input.value = ''
  }
}

/**
 * 折算示例：取真实检测数据（同一合并组各子项目最近的一条可转数字报告值），
 * 按当前配置的系数算出"折算后 → 合计"，便于核对系数是否正确。
 */
async function loadSample(g: RuleGroup): Promise<void> {
  const names = g.members.map((m) => m.name.trim()).filter(Boolean)
  if (!names.length) {
    sampleNote.value = '该合并组还没有成员项目'
    return
  }
  sampleLoading.value = true
  sampleNote.value = ''
  sampleRows.value = []
  try {
    const inList = names.map((n) => `'${n.replace(/'/g, "''")}'`).join(',')
    const sql =
      'SELECT sp.DECIDE_PROJECT_NAME, r.REPORT_VAL, sp.SAMPLE_ID ' +
      'FROM DETECTION.DT_SAMPLE_PROJECT sp ' +
      'LEFT JOIN DETECTION.DT_RESULT_CHECK_IN r ON r.SAMPLE_PROJECT_ID = sp.ID AND r.IS_DELETED = 0 ' +
      "WHERE sp.IS_DELETED = 0 AND NVL(sp.IS_LOGOUT, 'NO') <> 'YES' " +
      'AND sp.DECIDE_PROJECT_NAME IN (' + inList + ') ' +
      "AND r.REPORT_VAL IS NOT NULL AND r.REPORT_VAL <> '' " +
      'ORDER BY sp.SAMPLE_ID DESC LIMIT 400'
    const res = await dmApi.query(sql)
    if (!res.success) {
      sampleNote.value = `取真实数据失败：${res.error || '未知错误'}`
      return
    }
    const rows = (res.rows || []).map((r) => ({
      project: String(r[0] ?? ''),
      value: String(r[1] ?? ''),
      sampleId: String(r[2] ?? ''),
    }))
    // 优先挑"同一样品里子项目最多"的那组，示例更完整
    const bySample = new Map<string, typeof rows>()
    rows.forEach((x) => {
      const arr = bySample.get(x.sampleId) || []
      arr.push(x)
      bySample.set(x.sampleId, arr)
    })
    let best: typeof rows = []
    bySample.forEach((arr) => {
      if (arr.length > best.length) best = arr
    })
    const picked = new Map<string, string>()
    ;(best.length ? best : rows).forEach((x) => {
      if (!picked.has(x.project) && !isNaN(parseFloat(x.value))) picked.set(x.project, x.value)
    })
    sampleRows.value = names
      .filter((n) => picked.has(n))
      .map((n) => ({ project: n, value: picked.get(n) as string, sampleId: best[0]?.sampleId || '' }))
    if (!sampleRows.value.length) {
      sampleNote.value = '未取到可用的真实数值（该项目可能都是未检出）'
    } else if (!best.length) {
      sampleNote.value = '未找到同一样品同时含多个子项的记录，示例取各子项目的最近有效值'
    } else {
      sampleNote.value = `示例取自真实记录（样品 ID = ${best[0]?.sampleId || '—'}）`
    }
  } catch (e) {
    sampleNote.value = `取真实数据失败：${(e as Error).message}`
  } finally {
    sampleLoading.value = false
  }
}

function factorText(g: RuleGroup, name: string): string {
  const hit = (g.members || []).find((m) => m.name === name)
  return hit ? String(hit.factor) : '1'
}

function scaledValue(g: RuleGroup, name: string, raw: string): number {
  return parseFloat(raw) * parseFactor(factorText(g, name))
}

function sampleTotal(g: RuleGroup): number {
  return sampleRows.value.reduce((acc, x) => acc + scaledValue(g, x.project, x.value), 0)
}

onMounted(() => {
  switchTemplate(activeId.value)
})
</script>

<template>
  <div class="v-rules">
    <div class="container">
      <header>
        <h1>⚙️ 查询模板规则设置</h1>
        <p>LIMS 统计查询（/dm）各模板的项目名别名与多项目合并折算规则 · 后端存档 + 版本可回滚</p>
      </header>

      <div class="layout">
        <!-- 左侧：模板列表 -->
        <aside class="side">
          <div class="side-title">规则模板</div>
          <button
            v-for="tid in TEMPLATE_ORDER"
            :key="tid"
            class="side-item"
            :class="{ active: tid === activeId }"
            @click="switchTemplate(tid)"
          >
            {{ TEMPLATE_LABELS[tid] || tid }}
          </button>
          <div class="side-hint">
            全局别名对所有模板生效（年度统计除外——它只用自己的别名表）；模板自己的别名优先。
          </div>
        </aside>

        <!-- 右侧：编辑区 -->
        <main class="main">
          <div class="bar">
            <div class="bar-info">
              <span class="chip" :class="meta.source === 'custom' ? 'chip-custom' : 'chip-builtin'">
                {{ meta.source === 'custom' ? '后端已保存' : '内置默认' }}
              </span>
              <span class="mono">v{{ meta.version }}</span>
              <span v-if="meta.updatedAt" class="muted">{{ meta.updatedAt }} · {{ meta.updatedBy || '未署名' }}</span>
            </div>
            <div class="bar-actions">
              <input v-model="operator" class="op" placeholder="操作人（记入版本）" />
              <button class="btn" :disabled="loading" @click="loadRule(activeId)">重新载入</button>
              <button class="btn" @click="exportJson">导出 JSON</button>
              <label class="btn file">
                导入 JSON
                <input type="file" accept=".json,application/json" @change="importJson" />
              </label>
              <button class="btn warn" @click="resetDefault">恢复默认</button>
              <button class="btn primary" :disabled="saving" @click="save">
                {{ saving ? '保存中…' : '保存为新版本' }}
              </button>
            </div>
          </div>

          <p v-if="errorMsg" class="alert err">{{ errorMsg }}</p>
          <p v-if="message" class="alert ok">{{ message }}</p>
          <p v-if="dirtyCount > 0" class="alert warn2">
            有 {{ dirtyCount }} 行别名只填了一半（该行不会保存），请补全或删除。
          </p>

          <!-- 别名映射 -->
          <section class="card">
            <div class="card-head">
              <h2>A 类 · 项目名别名映射</h2>
              <button class="btn small" @click="addNameMapRow">+ 新增映射</button>
            </div>
            <p class="tip">
              「数据库项目名」是 LIMS 里的原始检测项目名，「模板列名」是导出表里要显示的列名。
              <template v-if="isGlobal">这里是<strong>全局表</strong>，所有模板共用。</template>
              <template v-else>这里只对<strong>{{ TEMPLATE_LABELS[activeId] }}</strong>生效，优先于全局表。</template>
            </p>
            <div v-if="editable.nameMap.length === 0" class="empty">暂无别名映射</div>
            <table v-else class="grid">
              <thead>
                <tr><th style="width:44%">数据库项目名</th><th style="width:44%">模板列名</th><th></th></tr>
              </thead>
              <tbody>
                <tr v-for="(x, i) in editable.nameMap" :key="i">
                  <td><input v-model="x.key" placeholder="如：呋喃唑酮代谢物[AOZ]" /></td>
                  <td><input v-model="x.value" placeholder="如：呋喃唑酮代谢物" /></td>
                  <td class="c"><button class="del" @click="removeNameMapRow(i)">删除</button></td>
                </tr>
              </tbody>
            </table>
          </section>

          <!-- 合并组 -->
          <section class="card">
            <div class="card-head">
              <h2>B 类 · 多项目合并（折算公式）</h2>
              <button class="btn small" @click="addGroup">+ 新增合并组</button>
            </div>
            <p class="tip">
              折算系数支持<strong>数字</strong>（如 <span class="mono">1</span>、<span class="mono">0.89</span>）或
              <strong>分数</strong>（如 <span class="mono">260.38/292.38</span>，= 主项目分子量/子项目分子量）。
              合并结果 = Σ(各子项目值 × 系数)，即组内各子项<strong>合并为一个值</strong>；
              只有<strong>同一检测项目出现多条记录</strong>时才算重复，按下面的「重复时怎么显示」处理。
            </p>
            <div v-if="editable.groups.length === 0" class="empty">该模板没有合并组（各列独立取值）</div>

            <div v-for="(g, gi) in editable.groups" :key="gi" class="group">
              <div class="group-head">
                <input v-model="g.target" class="target" placeholder="模板列名，如：甲拌磷（包括甲拌磷砜和甲拌磷亚砜）" />
                <button class="del" @click="removeGroup(gi)">删除该组</button>
              </div>

              <div class="opts">
                <label>
                  合并方式
                  <select v-model="g.combine">
                    <option v-for="o in COMBINE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
                  </select>
                </label>
                <label>
                  重复时怎么显示
                  <select v-model="g.dupPolicy">
                    <option v-for="o in DUPPOLICY_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
                  </select>
                </label>
              </div>

              <table class="grid">
                <thead>
                  <tr><th style="width:50%">子项目名（数据库里的名字）</th><th style="width:30%">折算系数</th><th></th></tr>
                </thead>
                <tbody>
                  <tr v-for="(m, mi) in g.members" :key="mi">
                    <td><input v-model="m.name" placeholder="如：甲拌磷砜" /></td>
                    <td><input v-model="m.factor" class="mono" placeholder="1 或 260.38/292.38" /></td>
                    <td class="c"><button class="del" @click="removeMember(g, mi)">删除</button></td>
                  </tr>
                </tbody>
              </table>
              <div class="group-foot">
                <button class="btn small" @click="addMember(g)">+ 成员</button>
                <button class="btn small" :disabled="sampleLoading" @click="loadSample(g)">
                  {{ sampleLoading ? '取数中…' : '取真实数据看折算示例' }}
                </button>
              </div>

              <div v-if="sampleRows.length" class="sample">
                <div class="sample-title">折算示例 <span class="muted">{{ sampleNote }}</span></div>
                <table class="grid sample-grid">
                  <thead>
                    <tr><th>子项目</th><th>报告值</th><th>系数</th><th>折算后</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="x in sampleRows" :key="x.project">
                      <td>{{ x.project }}</td>
                      <td class="mono">{{ x.value }}</td>
                      <td class="mono">{{ factorText(g, x.project) }}</td>
                      <td class="mono">{{ scaledValue(g, x.project, x.value).toFixed(4) }}</td>
                    </tr>
                    <tr class="total">
                      <td colspan="3">合计</td>
                      <td class="mono">{{ sampleTotal(g).toFixed(4) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </section>

          <!-- 版本历史 -->
          <section class="card">
            <div class="card-head">
              <h2>版本历史（只增不删）</h2>
              <button class="btn small" @click="loadVersions">刷新</button>
            </div>
            <div v-if="versions.length === 0" class="empty">还没有保存过规则（当前使用内置默认）</div>
            <table v-else class="grid">
              <thead>
                <tr><th>版本</th><th>时间</th><th>操作人</th><th>说明</th><th></th></tr>
              </thead>
              <tbody>
                <tr v-for="v in versions" :key="v.version" :class="{ current: v.active }">
                  <td class="mono">v{{ v.version }}<span v-if="v.active" class="tag">当前</span></td>
                  <td class="muted">{{ v.createdAt }}</td>
                  <td>{{ v.operator || '—' }}</td>
                  <td class="muted">{{ v.note || '—' }}</td>
                  <td class="c">
                    <button v-if="!v.active" class="btn small" @click="rollback(v.version)">回滚到此版本</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          <section class="card note">
            <h2>生效说明</h2>
            <ul>
              <li>保存后<strong>立即生效</strong>：下一次导出（农产品 / 畜产品 / 水产品 / 年度统计）自动使用新规则，无需重启服务。</li>
              <li>本页只覆盖<strong>别名映射</strong>与<strong>合并折算</strong>两类规则；判定口径、字段硬编码、归类规则不在本次范围。</li>
              <li>后端规则服务不可用时，导出会自动退回<strong>内置默认规则</strong>，不会中断业务。</li>
              <li>默认规则 = 改造前的硬编码常量，所以「没动过」的模板导出结果与以前完全一致。</li>
            </ul>
            <p v-if="builtinDef" class="muted">
              内置默认：{{ Object.keys(builtinDef.nameMap || {}).length }} 条别名 ·
              {{ (builtinDef.groups || []).length }} 个合并组
            </p>
          </section>
        </main>
      </div>
    </div>
  </div>
</template>

<style>
.v-rules,
.v-rules * { margin: 0; padding: 0; box-sizing: border-box; }
.v-rules {
  font-family: -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif;
  background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
  min-height: 100vh;
  color: #e2e8f0;
  padding: 32px 20px 60px;
}
.v-rules .container { max-width: 1240px; margin: 0 auto; }
.v-rules header { text-align: center; margin-bottom: 26px; }
.v-rules header h1 {
  font-size: 28px; font-weight: 700;
  background: linear-gradient(90deg, #38bdf8, #818cf8, #c084fc);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.v-rules header p { color: #94a3b8; margin-top: 8px; font-size: 14px; }
.v-rules .layout { display: grid; grid-template-columns: 220px 1fr; gap: 18px; align-items: start; }
@media (max-width: 900px) { .v-rules .layout { grid-template-columns: 1fr; } }

.v-rules .side {
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.1);
  border-radius: 14px; padding: 14px;
  position: sticky; top: 20px;
}
.v-rules .side-title { font-size: 13px; color: #94a3b8; margin-bottom: 10px; }
.v-rules .side-item {
  display: block; width: 100%; text-align: left;
  background: transparent; border: 1px solid transparent; color: #cbd5e1;
  padding: 9px 11px; border-radius: 9px; font-size: 13px; cursor: pointer; margin-bottom: 6px;
  transition: background .15s, border-color .15s;
}
.v-rules .side-item:hover { background: rgba(56,189,248,0.12); }
.v-rules .side-item.active { background: rgba(56,189,248,0.2); border-color: #38bdf8; color: #e2e8f0; font-weight: 600; }
.v-rules .side-hint { font-size: 12px; color: #64748b; line-height: 1.6; margin-top: 10px; }

.v-rules .main { display: flex; flex-direction: column; gap: 16px; }
.v-rules .bar {
  display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;
  background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
  border-radius: 14px; padding: 12px 16px;
}
.v-rules .bar-info { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.v-rules .bar-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.v-rules .chip { font-size: 12px; padding: 3px 10px; border-radius: 20px; }
.v-rules .chip-builtin { background: rgba(148,163,184,0.25); color: #cbd5e1; }
.v-rules .chip-custom { background: rgba(34,197,94,0.22); color: #4ade80; }
.v-rules .mono { font-family: Consolas, monospace; font-size: 12px; }
.v-rules .muted { color: #94a3b8; font-size: 12px; }

.v-rules .op {
  background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.15);
  color: #e2e8f0; border-radius: 8px; padding: 6px 10px; font-size: 12px; width: 150px;
}
.v-rules .btn {
  background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.22);
  color: #e2e8f0; font-size: 12px; padding: 6px 12px; border-radius: 8px; cursor: pointer;
  transition: background .15s;
}
.v-rules .btn:hover:not(:disabled) { background: rgba(255,255,255,0.2); }
.v-rules .btn:disabled { opacity: .55; cursor: default; }
.v-rules .btn.primary { background: linear-gradient(135deg,#0ea5e9,#2563eb); border-color: transparent; }
.v-rules .btn.warn { background: rgba(245,158,11,0.22); border-color: rgba(245,158,11,0.5); color: #fbbf24; }
.v-rules .btn.small { padding: 4px 10px; font-size: 12px; }
.v-rules .btn.file { position: relative; overflow: hidden; display: inline-block; }
.v-rules .btn.file input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }

.v-rules .alert { border-radius: 10px; padding: 10px 14px; font-size: 13px; }
.v-rules .alert.err { background: rgba(239,68,68,0.16); border: 1px solid rgba(239,68,68,0.45); color: #fca5a5; }
.v-rules .alert.ok { background: rgba(34,197,94,0.16); border: 1px solid rgba(34,197,94,0.45); color: #86efac; }
.v-rules .alert.warn2 { background: rgba(245,158,11,0.16); border: 1px solid rgba(245,158,11,0.45); color: #fcd34d; }

.v-rules .card {
  background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
  border-radius: 14px; padding: 16px 18px;
}
.v-rules .card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 10px; }
.v-rules .card h2 { font-size: 15px; font-weight: 600; }
.v-rules .tip { font-size: 12px; color: #94a3b8; line-height: 1.7; margin-bottom: 10px; }
.v-rules .empty { font-size: 13px; color: #64748b; padding: 12px 0; }

.v-rules table.grid { width: 100%; border-collapse: collapse; }
.v-rules table.grid th {
  text-align: left; font-size: 12px; color: #94a3b8; font-weight: 500;
  padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.1);
}
.v-rules table.grid td { padding: 4px 8px; border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 13px; }
.v-rules table.grid td.c { text-align: center; width: 70px; }
.v-rules table.grid tr.current { background: rgba(56,189,248,0.1); }
.v-rules table.grid tr.total td { font-weight: 600; color: #7dd3fc; }
.v-rules input, .v-rules select {
  width: 100%; background: rgba(0,0,0,0.28); border: 1px solid rgba(255,255,255,0.14);
  color: #e2e8f0; border-radius: 7px; padding: 6px 9px; font-size: 13px;
}
.v-rules input:focus, .v-rules select:focus { outline: none; border-color: #38bdf8; }
.v-rules .del {
  background: rgba(239,68,68,0.18); border: 1px solid rgba(239,68,68,0.4);
  color: #fca5a5; border-radius: 7px; padding: 5px 9px; font-size: 12px; cursor: pointer;
}
.v-rules .tag {
  margin-left: 6px; font-size: 11px; padding: 1px 6px; border-radius: 6px;
  background: rgba(56,189,248,0.22); color: #7dd3fc;
}

.v-rules .group {
  border: 1px solid rgba(255,255,255,0.12); border-radius: 11px;
  padding: 12px; margin-top: 12px; background: rgba(0,0,0,0.16);
}
.v-rules .group-head { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; }
.v-rules .group-head .target { flex: 1; font-weight: 600; }
.v-rules .opts { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; margin-bottom: 10px; }
.v-rules .opts label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #94a3b8; }
.v-rules .group-foot { display: flex; gap: 8px; margin-top: 10px; }
.v-rules .sample { margin-top: 10px; border-top: 1px dashed rgba(255,255,255,0.15); padding-top: 10px; }
.v-rules .sample-title { font-size: 13px; margin-bottom: 6px; }
.v-rules .sample-grid { background: rgba(0,0,0,0.2); border-radius: 8px; }
.v-rules .card.note ul { margin-left: 18px; }
.v-rules .card.note li { font-size: 13px; color: #cbd5e1; line-height: 1.9; }
</style>
