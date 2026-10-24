// P1-8 行为测试：报告页统计条的口径。
// 实机反例：23 分钟、SUCCESS_WITH_ISSUES、验收 overall=fail 的任务，统计条写
//   `耗时 0s` / `成功率 100%` / `Steps 7/7`，而计划里同时挂着失败徽章。
// 这里只断言口径：耗时取真实 elapsed_sec、成功率写明"步骤执行"且与验收结论并排有解释、
// 跳过/降级/失败显式列出；不改任何验收语义。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { acceptanceVerdict, reportStatsView, stepsTally } from '../src/lib/reportStats.ts'
import { formatDuration } from '../src/lib/format.ts'

const srcOf = (p) => readFileSync(fileURLToPath(new URL(p, import.meta.url)), 'utf8')

test('耗时：后端 elapsed_sec（1402s ≈ 23 分）优先，绝不再写 0s', () => {
  const v = reportStatsView({
    summary: 'SUCCESS_WITH_ISSUES', acceptanceOverall: 'fail',
    stats: { totalSteps: 7, successSteps: 7, failedSteps: 0, duration: 0 },
    elapsedSec: 1402,
  })
  assert.equal(v.elapsed, formatDuration(1402))
  assert.match(v.elapsed, /23 分 22 秒/, v.elapsed)
  assert.ok(!v.elapsed.includes('0 秒') || v.elapsed.includes('22 秒'), v.elapsed)
})

test('耗时：两者都拿不到 → 显示 "—" 而不是 0 秒', () => {
  const v = reportStatsView({
    summary: 'SUCCESS', stats: { totalSteps: 3, successSteps: 3, duration: 0 },
    elapsedSec: null,
  })
  assert.equal(v.elapsed, '—')
})

test('成功率：标签写明「步骤执行」口径，与验收未通过并排时必须带解释', () => {
  const v = reportStatsView({
    summary: 'SUCCESS_WITH_ISSUES', acceptanceOverall: 'fail',
    stats: { totalSteps: 7, successSteps: 7, failedSteps: 0 },
    elapsedSec: 1402,
  })
  assert.equal(v.rateLabel, '步骤执行成功率')
  assert.equal(v.rateValue, '100%')
  assert.equal(v.acceptance.failed, true)
  // 同屏出现"100%"与"未通过"时，解释行必须说明两者口径不同
  assert.ok(v.note.includes('步骤执行成功率'), v.note)
  assert.ok(v.note.includes('未通过'), v.note)
  assert.ok(v.note.includes('不等于验收通过'), v.note)
})

test('验收结论：只读后端字段；读不到写"未知"，不猜"通过"', () => {
  assert.equal(acceptanceVerdict('SUCCESS_WITH_ISSUES', 'fail').failed, true)
  assert.equal(acceptanceVerdict('SUCCESS', 'pass').failed, false)
  assert.equal(acceptanceVerdict('SUCCESS_WITH_ISSUES', '').failed, true)
  assert.equal(acceptanceVerdict('', '').overall, '未知')
  assert.equal(acceptanceVerdict('', '').known, false)
})

test('Steps：7/7 里含 1 跳过、1 降级、1 失败时必须显式写出，不能吞成全绿', () => {
  const v = reportStatsView({
    summary: 'SUCCESS_WITH_ISSUES', acceptanceOverall: 'pass',
    stats: { totalSteps: 7, successSteps: 6, failedSteps: 0, duration: 0 },
    elapsedSec: 1402,
    steps: [
      { status: 'success' }, { status: 'success' }, { status: 'skipped' },
      { status: 'success' }, { status: 'success' }, { status: 'failed' },
      { status: 'success' },
    ],
    searchCoverage: { total_search_steps: 4, ok_steps: 3, degraded_steps: 1 },
  })
  assert.match(v.steps, /^6\/7/)
  assert.match(v.steps, /1 跳过/, v.steps)
  assert.match(v.steps, /1 降级/, v.steps)
  assert.match(v.steps, /1 失败/, v.steps)
  // 裸计数（无跳过/降级/失败）保持 7/7 形式
  assert.equal(stepsTally({ success: 7, total: 7 }), '7/7')
})

test('报告页使用 reportStatsView 的口径（源码扫描：不再有写死的"成功率"与裸 7/7）', () => {
  const code = srcOf('../src/components/ReportViewer.tsx')
  assert.ok(code.includes('reportStatsView('), 'ReportViewer 未使用 reportStatsView')
  assert.ok(code.includes('statsView.rateLabel'), '统计条未使用"步骤执行成功率"标签')
  assert.ok(code.includes('机器验收结论：'), '统计条缺少与验收结论的口径说明行')
  assert.ok(/elapsedSec: report\.elapsedSec/.test(code), '统计条未读 elapsedSec')
})
