import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import config from '../tailwind.config.js'

const source = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const colors = config.theme.extend.colors
const luminance = hex => {
  const channels = hex.slice(1).match(/../g).map(part => Number.parseInt(part, 16) / 255)
    .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
}
const contrast = (foreground, background) => {
  const values = [luminance(foreground), luminance(background)].sort((first, second) => second - first)
  return (values[0] + 0.05) / (values[1] + 0.05)
}

test('正文、次要文字与状态色在纸白底上满足普通文字对比度', () => {
  for (const foreground of Object.values(colors.ink).concat(Object.values(colors.state), colors.brand.DEFAULT)) {
    for (const background of [colors.surface.DEFAULT, colors.surface.sunken]) {
      assert.ok(contrast(foreground, background) >= 4.5, `${foreground} on ${background}`)
    }
  }
  assert.ok(contrast('#ffffff', colors.brand.DEFAULT) >= 4.5)
})

test('研究优先的分组导航保留全部原路由，移动端保留系统与账户入口', () => {
  const shell = source('../src/components/AppLayout.tsx')
  for (const route of ['/', '/history', '/research-study', '/agents', '/health', '/memory', '/evals', '/skills', '/metrics', '/audit', '/settings']) {
    assert.ok(shell.includes(`to: '${route}'`), `missing ${route}`)
  }
  for (const feature of ['MOBILE_PRIMARY', 'MOBILE_MORE', 'aria-modal="true"', "event.key === 'Escape'", '演示模式', '退出登录', 'skip-link']) assert.ok(shell.includes(feature))
})

test('引导式提交同时传递目标和契约，且保留预览、自定义与安全边界', () => {
  const panel = source('../src/components/console/SubmitPanel.tsx')
  assert.ok(panel.includes('onStart={(goalText, fields) => onSubmit(goalText, fields)}'))
  assert.ok(panel.includes('onStart(preview.goal, preview.fields)'))
  assert.ok(panel.includes('setGaps(preview.gaps); return'))
  for (const feature of ['isRunning || demoMode', '预览并编辑研究目标', 'role="tabpanel"', 'event.ctrlKey || event.metaKey', '更多研究选项', 'aria-expanded={showContext}']) assert.ok(panel.includes(feature))
  assert.ok(!panel.includes('hidden sm:block'))
})

test('新研究清除深链状态，演示重置后不能覆盖新研究', () => {
  const page = source('../src/pages/TaskConsole.tsx')
  for (const action of ["setDeepTaskId('')", "setGoal('')", 'setComposerVersion(version => version + 1)']) assert.ok(page.includes(action))
  const runner = source('../src/stores/useDemoRunner.ts')
  assert.ok(runner.includes("useTaskStore.getState().currentTaskId !== 'demo-task-001'"))
  const demo = source('../src/stores/demoData.ts')
  for (const text of ['内置合成演示', '不对应真实证券', '未执行真实规则验收', '本版待复核', 'files: []']) assert.ok(demo.includes(text))
})

test('指南默认收起但隔离提醒始终可见，保留弱动效偏好与安全区', () => {
  const guide = source('../src/components/FirstRunGuide.tsx')
  assert.ok(guide.includes('useState(!configComplete)'))
  assert.ok(guide.indexOf('data-code-execution-notice') < guide.indexOf('{guideOpen &&'))
  const css = source('../src/index.css')
  for (const feature of ['prefers-reduced-motion', 'env(safe-area-inset-bottom)', ':focus-visible', 'color-scheme: light']) assert.ok(css.includes(feature))
})

test('报告提供正文、底稿与导出定位，报告头不再一律宣称绿色完成', () => {
  const report = source('../src/components/ReportViewer.tsx')
  for (const anchor of ['report-body', 'working-paper', 'acceptance-evidence', 'report-export']) {
    assert.ok(report.includes(`id="${anchor}"`), anchor)
  }
  assert.ok(report.includes('<StatusBadge status={report.summary} />'))
  assert.ok(!report.includes('>任务完成</span>'))
  assert.ok(report.includes('报告内容定位'))
})
