// P0-4 行为测试：交付包口径只有一个来源。
// 实机反例：同一屏「分析工作台」写"当前包: deliverables_20261021_190912.zip（与采纳稿同版才可下载）"，
// 「导出与复核」写"本次任务暂无交付包"。两处必须读同一次 resolvePackage 的结果；
// "暂无交付包"只能出现在**两个来源都为空**的时候。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  NO_PACKAGE_NOTE, PACKAGE_LOADING_NOTE, exportPanelNote, packageLine, resolvePackage,
} from '../src/lib/packageState.ts'

const srcOf = (p) => readFileSync(fileURLToPath(new URL(p, import.meta.url)), 'utf8')

/** frontend/src 下所有 .ts/.tsx 的代码行（去掉注释行）。 */
function codeLines() {
  const root = fileURLToPath(new URL('../src', import.meta.url))
  const out = []
  const walk = (dir) => {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      const p = `${dir}/${e.name}`
      if (e.isDirectory()) walk(p)
      else if (/\.tsx?$/.test(e.name)) {
        const live = readFileSync(p, 'utf8').split('\n')
          .filter(l => !l.trim().startsWith('//') && !l.trim().startsWith('*')).join('\n')
        out.push([p, live])
      }
    }
  }
  walk(root)
  return out
}

test('「暂无交付包」在整个 src 里只有一个模块持有（其余全部走同一函数）', () => {
  const holders = codeLines().filter(([, code]) => code.includes('暂无交付包')).map(([p]) => p)
  assert.equal(holders.length, 1, JSON.stringify(holders))
  assert.ok(/lib[\\/]packageState\.ts$/.test(holders[0]), holders[0])
})

test('export.package 有值 → 有包，来源是 export，绝不说"暂无交付包"', () => {
  const res = resolvePackage({ exportPackage: 'deliverables_20261021_190912.zip' })
  assert.equal(res.hasPackage, true)
  assert.equal(res.source, 'export')
  assert.equal(res.name, 'deliverables_20261021_190912.zip')
  assert.ok(!exportPanelNote(res, '2026-10-21 19:09:12').includes('暂无交付包'))
  assert.match(exportPanelNote(res, '2026-10-21 19:09:12'), /deliverables_20261021_190912\.zip/)
})

test('只有分析工作台的 current_package 有值 → 也算有包（来源 analysis），两处仍一致', () => {
  const res = resolvePackage({ exportPackage: '', analysisPackage: 'deliverables_20261021_190912.zip' })
  assert.equal(res.source, 'analysis')
  assert.equal(res.hasPackage, true)
  // 同一解析结果分别喂给两个面板：说的是同一个包名
  assert.match(packageLine(res), /当前包：deliverables_20261021_190912\.zip/)
  assert.match(exportPanelNote(res), /deliverables_20261021_190912\.zip/)
})

test('「暂无交付包」只在两个来源都为空时出现（且是唯一的字面量处）', () => {
  const none = resolvePackage({ exportPackage: null, analysisPackage: undefined })
  assert.equal(none.hasPackage, false)
  assert.equal(none.source, 'none')
  assert.equal(exportPanelNote(none), NO_PACKAGE_NOTE)
  assert.match(NO_PACKAGE_NOTE, /暂无交付包/)
  // 有任一来源时不得出现该字样
  for (const r of [resolvePackage({ exportPackage: 'a.zip' }),
    resolvePackage({ analysisPackage: 'b.zip' })]) {
    assert.ok(!exportPanelNote(r).includes('暂无交付包'), exportPanelNote(r))
    assert.ok(!packageLine(r).includes('暂无交付包'), packageLine(r))
  }
})

test('有采纳稿但没有同版包 → 说"尚无与采纳稿同版的包"，不说"暂无交付包"', () => {
  const res = resolvePackage({ analysisPackage: '', adoptedIdentity: undefined,
    packageNote: '采纳稿 revision_9 尚未打包' })
  const line = packageLine(res, { adoptedIdentity: 'rev-2f3c41d9ab77' })
  // 采纳稿标识按前 12 位显示（与复核栏同一口径），足以区分是哪个版本
  assert.match(line, /尚无与当前采纳稿（rev-2f3c41d9）同版的包/, line)
  assert.ok(!line.includes('暂无交付包'), line)
  assert.equal(packageLine(res).startsWith('交付包：'), true)
})

test('三个面板都改用同一个解析函数（源码扫描：没有各自的判定，也没有写死的"暂无交付包"）', () => {
  const viewer = srcOf('../src/components/ReportViewer.tsx')
  const workbench = srcOf('../src/components/AnalysisWorkbenchPanel.tsx')
  const brief = srcOf('../src/components/ResearchBriefPanel.tsx')
  for (const [name, code] of [['ReportViewer', viewer], ['AnalysisWorkbenchPanel', workbench],
    ['ResearchBriefPanel', brief]]) {
    // 只扫代码行（注释里引用实机反例的文案是允许的）
    const live = code.split('\n')
      .filter(l => !l.trim().startsWith('//') && !l.trim().startsWith('*'))
      .join('\n')
    assert.ok(/usePackageState\(|briefPackageLine\(/.test(live), `${name} 未使用同一来源`)
    assert.ok(!live.includes('暂无交付包'), `${name} 里仍有写死的"暂无交付包"文案`)
  }
  assert.ok(/exportPanelNote\(pkg\.res/.test(viewer), '导出面板未使用 exportPanelNote(pkg.res…)')
  assert.ok(/packageLine\(pkg\.res/.test(workbench), '工作台未使用 packageLine(pkg.res…)')
  assert.ok(/packageRes=\{pkg\.res\}/.test(viewer), '简报面板未拿到同一份解析结果')
  // 未读到包状态时不下"没有包"的结论
  assert.ok(viewer.includes('PACKAGE_LOADING_NOTE'), '导出面板缺少读取中的中性文案')
  assert.ok(PACKAGE_LOADING_NOTE.includes('先不下'), PACKAGE_LOADING_NOTE)
})
