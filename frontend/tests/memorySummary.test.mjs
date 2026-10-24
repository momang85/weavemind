// S3 行为测试：记忆页「系统自述」的数字口径核对——464 条对话被写成「464 项任务」时，
// 页面必须如实指出，并且绝不编造本页没有的数字（如指标页的总任务数 570）。
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { auditMemorySummary, memoryAuthorityNote } from '../src/lib/memorySummary.ts'

// 2026-10-22 真机（recheck/memory.txt:40）逐字原文 + /api/memory 实取 stats
const SUMMARY_BAD = '我是织光——一支运行在你本地的 AI 团队。我已完成了 464 项任务、沉淀了 269 条成功策略。'
  + '我可以帮你调研、分析数据、写报告，还会在任务后自我复盘与进化。'
// 另一次加载返回的缓存自述（措辞不同、数字口径正确）——报告里记录过这个不稳定现象
const SUMMARY_CACHED = '我已积累464条对话、269条成功策略'
const COUNTS = { conversations: 464, strategies: 269 }

test('实机反例：把 464 条对话写成「464 项任务」→ 明确告警', () => {
  const audit = auditMemorySummary(SUMMARY_BAD, COUNTS)
  assert.equal(audit.warnings.length, 1)
  const w = audit.warnings[0]
  assert.match(w, /464/)
  assert.match(w, /对话条数/)
  assert.match(w, /未经核对/)
  assert.equal(audit.trustworthy, false)
  assert.deepEqual(
    audit.claims.map(c => [c.kind, c.value]),
    [['tasks', 464], ['strategies', 269]],
  )
})

test('告警里不出现本页没取到的数字（不编造指标页的 570）', () => {
  const audit = auditMemorySummary(SUMMARY_BAD, COUNTS)
  const all = [...audit.warnings, audit.authorityNote, audit.publishNote].join(' ')
  assert.ok(!all.includes('570'), '本页没有任务总数口径，不得写出 570')
  assert.match(audit.authorityNote, /不在本页统计范围/)
})

test('权威口径行只写本页实取的两个数', () => {
  assert.equal(
    memoryAuthorityNote(COUNTS),
    '本页权威口径（取自 /api/memory 记忆库条数，与上方统计卡同源）：'
    + '对话 464 条、策略 269 条；「任务」总数不在本页统计范围（见「指标看板」），本页不推断。',
  )
})

test('对外发布口径改写：数字对不上时明说不可直接对外发布', () => {
  const audit = auditMemorySummary(SUMMARY_BAD, COUNTS)
  assert.match(audit.publishNote, /不可直接用于对外发布/)
  assert.ok(!audit.publishNote.includes('可直接用于发布到社交媒体'))
})

test('措辞正确的缓存自述：核对一致并给出确认，而不是照抄不可信告警', () => {
  const audit = auditMemorySummary(SUMMARY_CACHED, COUNTS)
  assert.equal(audit.warnings.length, 0)
  assert.equal(audit.trustworthy, true)
  assert.ok(audit.confirmations.length >= 2)
  assert.match(audit.confirmations.join(' '), /464 条对话/)
  assert.match(audit.publishNote, /核对一致/)
})

test('数字真的对不上（999 条对话）→ 以本页口径为准', () => {
  const audit = auditMemorySummary('我已积累 999 条对话、269 条成功策略', COUNTS)
  assert.equal(audit.warnings.length, 1)
  assert.match(audit.warnings[0], /999/)
  assert.match(audit.warnings[0], /464/)
})

test('本页口径未取到（接口失败）时：权威口径写未取得，任务数仍标记为无法核对', () => {
  const audit = auditMemorySummary(SUMMARY_BAD, { conversations: 0, strategies: 0 })
  // 对话/策略无从比对 → 不产生"不一致"告警，也不假装核对过
  assert.equal(audit.confirmations.length, 0)
  assert.match(audit.authorityNote, /未取得/)
  // 「464 项任务」这种主张本页永远没有任务数口径，仍如实标注无法核对
  assert.equal(audit.warnings.length, 1)
  assert.match(audit.warnings[0], /没有任务数口径可核对/)
  assert.equal(audit.trustworthy, false)
  assert.match(audit.publishNote, /不可直接用于对外发布/)
})

test('空自述不产生任何主张', () => {
  const audit = auditMemorySummary('', COUNTS)
  assert.deepEqual(audit.claims, [])
  assert.deepEqual(audit.warnings, [])
  assert.equal(audit.trustworthy, false)
})
