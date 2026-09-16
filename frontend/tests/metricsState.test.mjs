// 指标看板状态机的真实行为测试。
//
// 背景（架构复核 01:05）：原实现刷新失败只设错误文案，**旧的成功率继续显示**，
// 文案却说"显示为未知"。这里覆盖架构师点名的三种情形：
//   首次失败、成功后失败（不得留旧值当当前值）、恢复成功。

import assert from 'node:assert/strict'
import test from 'node:test'

const { METRICS_UNAVAILABLE_MESSAGE, metricsInitial, metricsStart, metricsSuccess, metricsFailure } =
  await import('../src/lib/metricsState.ts')

const SNAP_A = { success_rate: 92, tasks: { available: true, terminal: 25 } }
const SNAP_B = { success_rate: 40, tasks: { available: true, terminal: 30 } }

test('首次失败：统计不可用，没有可展示的数值，也没有"上次成功"', () => {
  let s = metricsInitial()
  s = metricsStart(s)
  s = metricsFailure(s)

  assert.equal(s.data, null, '首次失败不应有任何可当当前值展示的数据')
  assert.equal(s.error, METRICS_UNAVAILABLE_MESSAGE)
  assert.equal(s.lastOkAt, null)
  assert.equal(s.loading, false)
})

test('成功后刷新失败：丢弃旧快照，不得把上次成功率当作当前值', () => {
  let s = metricsInitial()
  s = metricsSuccess(s, SNAP_A, '2026-09-15T10:00:00Z')
  assert.equal(s.data.success_rate, 92)

  s = metricsFailure(s)
  assert.equal(s.data, null, '失败后旧的成功率必须被清掉（否则页面显示的是过期快照）')
  assert.equal(s.error, METRICS_UNAVAILABLE_MESSAGE)
  assert.equal(s.lastOkAt, '2026-09-15T10:00:00Z', '只保留"上次成功刷新"标签，且明确标注不作为当前值')
})

test('恢复成功：写入新值并清掉错误，快照时间更新', () => {
  let s = metricsInitial()
  s = metricsSuccess(s, SNAP_A, '2026-09-15T10:00:00Z')
  s = metricsFailure(s)
  s = metricsSuccess(s, SNAP_B, '2026-09-15T10:05:00Z')

  assert.equal(s.data.success_rate, 40)
  assert.equal(s.error, '')
  assert.equal(s.lastOkAt, '2026-09-15T10:05:00Z')
  assert.equal(s.loading, false)
})

test('刷新中：保留旧值但 loading=true（避免闪成空态）', () => {
  let s = metricsInitial()
  s = metricsSuccess(s, SNAP_A, '2026-09-15T10:00:00Z')
  s = metricsStart(s)
  assert.equal(s.loading, true)
  assert.equal(s.data.success_rate, 92)
})
