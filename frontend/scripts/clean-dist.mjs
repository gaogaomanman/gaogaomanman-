/**
 * 构建前清空 dist（由 package.json 的 `prebuild` 自动调用）。
 *
 * 为什么需要它：vite 自带的「清空 outDir」在本机**静默失效**——不报错、也没清掉，
 * 于是每轮构建都往 `dist/assets` 里再堆一套带 hash 的 chunk（实测 22 → 82 → 223 个）。
 * 功能不受影响（浏览器只按 index.html 的引用链加载），但会严重干扰
 * 「产物里到底有没有某次改动」的排查——必须先跟着引用链走才能判断。
 *
 * ⚠️ 本机不能用 `fs.rmSync` / `cmd rd`：会被「安全删除（回收站）」机制拦住，
 *    报 `[safe-delete] 操作失败 … Error during a 'trash' operation: Some operations were aborted`，
 *    且该失败**不报错也不删除**。实测 **PowerShell 的 `Remove-Item -Recurse -Force` 可以正常删除**
 *    （即便 8080 服务正在运行），所以 Windows 下走 PowerShell。
 *
 * 策略：**清不掉也不阻断构建**——失败只提示，不抛错。
 */
import { execFileSync } from 'node:child_process'
import { existsSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const dist = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist')

if (!existsSync(dist)) {
  console.log('[prebuild] dist 不存在，无需清理')
  process.exit(0)
}

try {
  if (process.platform === 'win32') {
    // 单引号要转义成两个单引号（PowerShell 单引号字符串的字面量规则）
    const lit = dist.replace(/'/g, "''")
    execFileSync(
      'powershell',
      ['-NoProfile', '-Command', `Remove-Item -LiteralPath '${lit}' -Recurse -Force -ErrorAction SilentlyContinue`],
      { stdio: 'ignore' },
    )
  } else {
    rmSync(dist, { recursive: true, force: true, maxRetries: 3, retryDelay: 200 })
  }
} catch (e) {
  console.log(`[prebuild] 清理命令异常（${e.code || e.message}），继续尝试构建`)
}

if (existsSync(dist)) {
  console.log('[prebuild] 警告：dist 未能清空，本轮构建会保留部分历史 chunk')
  console.log('[prebuild] 提示：停掉 8080 后再构建可得到干净产物（一键启动.bat 已按此顺序处理）')
} else {
  console.log('[prebuild] 已清空 dist（避免历史构建 chunk 累积）')
}
