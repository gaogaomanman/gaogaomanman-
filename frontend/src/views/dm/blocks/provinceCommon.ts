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
