/**
 * 省例行（农/畜/水产品）三块的共享工具（原文件模块级定义，行 2302~2321 与 2374 副本）。
 *
 * 说明：`formatSigNum` / `notDetectedText` 在原文件中就是**模块级单一实现**（行 2302 / 2316），
 * 被省例行三块共用（另有一对同名实现位于 `queryNewTemplate` 内部，行 703 / 716，属不同作用域，未合并）。
 * 本文件的 `parseCityCounty` 取自行 2374 副本，与 1476 / 1632 / 1773 三份逐字一致；
 * 行 688（年度统计内部）与 2612（省例行水产品内部）两份待对应块迁移时再比对。
 */

/** 有效数字格式化：<1 保留 2 位有效数字，>=1 保留 3 位有效数字 */
export function formatSigNum(value: unknown): string {
  const num = parseFloat(String(value))
  if (isNaN(num)) return String(value)
  if (num === 0) return '0'
  const abs = Math.abs(num)
  const sig = abs < 1 ? 2 : 3
  const exp = Math.floor(Math.log10(abs))
  const factor = Math.pow(10, sig - 1 - exp)
  const rounded = Math.round(num * factor) / factor
  // 转为字符串，避免科学计数法
  return String(rounded)
}

/** 未检出显示：未检出(检出限类型:检出限值) */
export function notDetectedText(limitType: unknown, limitValue: unknown): string {
  if (limitType || limitValue) {
    return '未检出(' + (limitType || '检出限') + ':' + (limitValue || '') + ')'
  }
  return '未检出'
}

/** 判定文本是否为「不合格」（样本级/单据级结论、单项目判定共用）。
 *
 * ⚠️ **只能正向匹配"不合格"，绝不能写成 `includes('合格')`**：
 *    `'不合格'` 本身包含子串 `'合格'`，`'不符合'` 包含 `'符合'`。
 *    实测全库有 30 条单据级结论同时含「不合格」和「合格」两个字样。
 *
 * ⚠️ 判定列是报告的最终结论，**必须以 LIMS 的结论为准**：不能因为单项目判定（`SINGLE_JUDGE`）
 *    漏填就改判为「合格」。实测任务 RW2026042 的 12 张检测单里，9 张样本级结论已明写"不合格"，
 *    但只有 2 张的单项目判定填了"不合格"；另有 7 张的涉事项目（恩诺沙星 140、环丙沙星 3.34，
 *    远超残留限量）单项目判定却是 `/`（未判定）。
 *
 * 用法（三个模板统一）：`let hasFail = isFailText(样本级结论) || isFailText(单据级结论)`，
 * 再在项目循环里 `if (isFailText(judge)) hasFail = true` 作交叉验证。
 */
export function isFailText(v: unknown): boolean {
  const s = String(v ?? '')
  return s.includes('不合格') || s.includes('不符合')
}

/** 原块内 parseCityCounty（2374 副本），未改动 */
export function parseCityCounty(addr: string): string {
  if (!addr) return ''
  let s = addr
  const m = s.match(/(省|自治区)/)
  if (m) s = s.substring(m.index + m[0].length)
  const m2 = s.match(/(.+?[市])([^路街道\d]+?(?:区|县|市|园区|新区|开发区))/) || s.match(/(.+?[市])(.+?[区县])/)
  if (m2) return m2[1] + m2[2]
  const m3 = s.match(/(.+?[市])/)
  if (m3) return m3[1]
  const m4 = s.match(/^([^路街道\d]+?(?:园区|新区|开发区))/)
  if (m4) return m4[1]
  return s
}
