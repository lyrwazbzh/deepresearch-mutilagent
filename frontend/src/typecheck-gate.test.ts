import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * P-19 回归门禁：前端曾长期**没有类型门禁** —— 根 `tsconfig.json` 是 solution 桩
 * （`files: []` + `references`），不跑 `-b` 时 `tsc --noEmit` 编译**空集**、恒 0 错退出；
 * 且 `tsc` 既不在 CI、也不在 `package.json`，`npm run build`（vite/esbuild）也不做类型检查。
 * 于是 17 处真实类型错误长期无人拦。
 *
 * 这组断言把「真实类型检查」钉进仓库：一旦脚本或 CI step 被移除、或被换回空真命令，立即失败。
 */
// vitest 的 root 即 frontend/（vitest.config.ts 所在目录）；也兼容从仓库根启动。
const candidates = [process.cwd(), resolve(process.cwd(), 'frontend')]
const FE = candidates.find((dir) => existsSync(resolve(dir, 'package.json')))

if (!FE) {
  throw new Error('找不到 frontend/package.json —— 无法校验类型门禁')
}

const pkgPath = resolve(FE, 'package.json')
const ciPath = resolve(FE, '..', '.github', 'workflows', 'ci-frontend.yml')

// 必须指向 app 配置的真实检查；solution 桩上的 `tsc --noEmit` 为空真，不算数
const REAL_TYPECHECK = 'tsc -p tsconfig.app.json --noEmit'

describe('frontend type gate (P-19)', () => {
  it('package.json 暴露指向 app 配置的真实 typecheck 脚本', () => {
    const pkg = JSON.parse(readFileSync(pkgPath, 'utf8')) as {
      scripts?: Record<string, string>
    }
    expect(pkg.scripts?.typecheck).toContain(REAL_TYPECHECK)
  })

  it('CI 运行该 typecheck（而非空真的 tsc --noEmit）', () => {
    const ci = readFileSync(ciPath, 'utf8')
    expect(ci).toMatch(
      /(npm run typecheck|npx tsc -p tsconfig\.app\.json --noEmit)/,
    )
    // 防回归：不得把 solution 桩上的 `tsc --noEmit`（无 -p/--build）当作门禁
    expect(ci).not.toMatch(/tsc\s+--noEmit/)
  })
})
