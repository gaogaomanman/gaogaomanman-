/**
 * 模块发布开关（`frontend/dist` 是 8080 实际托管的产物，调试环境 5173 不走这里）。
 *
 * 用法：模块还在核对口径/数字时，把它设为 `false` —— 产品构建里该模块只显示「建设中」占位页，
 * 避免使用者在口径未确认时看到半成品数字；调试环境（`vite dev`）**不受影响**，始终渲染真实模块，
 * 方便边改边看。
 *
 * 定版上线：把对应开关改成 `true` → `npm run build` → 重启 8080。
 */

/** 抽采样进度统计（/progress）：2026-09-21 使用方确认口径后正式上线 */
export const PROGRESS_PUBLISHED = true

/** 模块 key → 是否在产品构建里显示「建设中」 */
export function isUnderConstruction(key: string): boolean {
  if (key === 'progress') return !PROGRESS_PUBLISHED && !import.meta.env.DEV
  return false
}
