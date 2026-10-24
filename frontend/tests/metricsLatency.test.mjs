// S5 行为测试：平均耗时与 P95 同值时必须如实标注"样本过少 / P95 未独立计算"，
// 且说明它与"终态任务数"不是同一范围。数据为 2026-10-22 真机 /api/metrics 原始值。
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { latencyCardView, MIN_P95_SAMPLES } from '../src/lib/metricsLatency.ts'

// 实机 /api/metrics（.tmp/shots-pages/api_snapshot.json + 实时复核）
const LIVE = {
  avg_latency_sec: 1274.4,
  p95_latency_sec: 1274.4,
  latency_sample_size: 1,
  latency_scope: '仅统计本 metrics 进程启动后收到 elapsed_sec 的主终态任务，重启即清零，'
    + '故为空 = 本进程还没结算过任务，而不是延迟为 0。',
}
const LIVE_TERMINAL = 569

test('实机反例：avg == P95 == 1274.4 且只有 1 个样本 → 标注"P95 未独立计算"', () => {
  const v = latencyCardView(LIVE, LIVE_TERMINAL)
  assert.equal(v.value, '21 分 14 秒')
  assert.equal(v.sampleSize, 1)
  assert.equal(v.degenerate, true)
  assert.match(v.sub, /P95 未独立计算/)
  assert.match(v.sub, /样本 1 个/)
  // 旧形态正是这句：把 P95 与均值并列写出来，读起来像"分布非常集中"
  assert.notEqual(v.sub, 'P95 21 分 14 秒')
  assert.ok(!/^P95 21 分 14 秒$/.test(v.sub))
})

test('口径写清分母：与上方终态任务数不是同一范围，且不给用户"同分布"的错觉', () => {
  const v = latencyCardView(LIVE, LIVE_TERMINAL)
  assert.match(v.hint, /本 metrics 进程/)
  assert.match(v.hint, /569/)
  assert.match(v.hint, /不是同一范围/)
  assert.match(v.hint, /样本过少|样本太少/)
  assert.match(v.hint, /不代表耗时分布集中/)
  assert.match(v.hint, /重启即清零/)
})

test('样本充足且 P95 独立于均值 → 正常展示样本数，不再告警', () => {
  const v = latencyCardView(
    { avg_latency_sec: 100, p95_latency_sec: 300, latency_sample_size: 42 },
    569,
  )
  assert.equal(v.value, '1 分 40 秒')
  assert.equal(v.sub, 'P95 5 分 · 样本 42 个')
  assert.equal(v.degenerate, false)
  assert.ok(!v.hint.includes('不代表耗时分布集中'))
})

test('样本数为 2 但 P95 与均值仍完全同值 → 也判为未独立计算（不放过异常）', () => {
  const v = latencyCardView(
    { avg_latency_sec: 60, p95_latency_sec: 60, latency_sample_size: MIN_P95_SAMPLES },
    10,
  )
  assert.equal(v.degenerate, true)
  assert.match(v.hint, /完全相同/)
})

test('没有耗时样本时说"未取得"，绝不显示 0 秒', () => {
  const v = latencyCardView({ avg_latency_sec: null, p95_latency_sec: null, latency_sample_size: 0 }, 0)
  assert.equal(v.value, '未取得')
  assert.match(v.hint, /不是"耗时 0 秒"/)
  assert.ok(!v.value.includes('0 秒'))
})

test('P95 缺失但均值有值时如实说未取得', () => {
  const v = latencyCardView({ avg_latency_sec: 90, p95_latency_sec: null, latency_sample_size: 3 }, 5)
  assert.equal(v.value, '1 分 30 秒')
  assert.match(v.sub, /P95 未取得/)
  assert.equal(v.degenerate, false)
})

test('样本数未回传时不假装知道样本量', () => {
  const v = latencyCardView({ avg_latency_sec: 90, p95_latency_sec: 120 }, 5)
  assert.equal(v.sampleSize, null)
  assert.match(v.sub, /样本数未取得/)
  assert.equal(v.degenerate, false)
})

test('空指标不崩、不给数字', () => {
  const v = latencyCardView(null, null)
  assert.equal(v.value, '未取得')
  assert.equal(v.sampleSize, null)
})
