/**
 * 达梦页各功能块统一的「结果描述」。
 *
 * 原页面均把结果写进 `resultEl.innerHTML`，其中包含两种需要交互的结构：
 *   - 任务编号候选列表（点击某个编号即按该编号导出）
 *   - 编号候选列表（点击某个编号即按该编号导出）
 * 因此这里把它们提升为结构化结果，由 Vue 模板渲染成可点击芯片（替代内联 onclick），
 * 其余纯文本/简单 HTML 提示保持原样（原文案、原配色）。
 */
export type DmOutcome =
  | { type: 'html'; html: string }
  | { type: 'noList'; intro: string; col: string; nos: string[] }
  | { type: 'taskList'; intro: string; tasks: string[] }

export const msg = (html: string): DmOutcome => ({ type: 'html', html })

export const DANGER = 'var(--danger)'
export const WARNING = 'var(--warning)'
export const SUCCESS = 'var(--success)'

/** 原 `esc()`：把文本转义为可安全放进 innerHTML 的形式 */
export function esc(s: unknown): string {
  const d = document.createElement('div')
  d.textContent = String(s == null ? '' : s)
  return d.innerHTML
}

export const danger = (html: string): DmOutcome =>
  msg(`<span style="color:${DANGER};">${html}</span>`)
export const warn = (html: string): DmOutcome =>
  msg(`<span style="color:${WARNING};">${html}</span>`)
export const ok = (html: string): DmOutcome =>
  msg(`<span style="color:${SUCCESS};">${html}</span>`)
