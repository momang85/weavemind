// S4 行为测试：进化锦标赛回放的 `key=value` + 英文布尔必须改成中文句子；
// 评分映射不再 JSON.stringify 直出。数据为 2026-10-22 真机 /api/evolution 原文。
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { boolishValueText, localizeEvolutionSummary, scoreMapText } from '../src/lib/evolutionText.ts'

// recheck/memory.txt:404,409,414,419 逐字
const REAL_SUMMARIES = [
  '演化完成: 胜者=baseline-v1-gen1-A, 稳定=True, 已部署=True',
  '演化完成: 胜者=baseline-v1-gen1-C, 稳定=True, 已部署=True',
  '演化完成: 胜者=baseline-v1-gen1-A, 稳定=True, 已部署=True',
  '演化完成: 胜者=baseline-v1-gen1-C, 稳定=True, 已部署=False',
]
// 真机里还有 stable=False 的一轮
const REAL_UNSTABLE = '演化完成: 胜者=baseline-v1-gen1-A, 稳定=False, 已部署=False'
// 真机 rankings[0].scores（旧实现直接 JSON.stringify 到页面上）
const REAL_SCORES = { 'baseline-v1-gen1-A': 8.5, 'baseline-v1-gen1-B': 6.0, 'baseline-v1-gen1-C': 7.8 }
const REAL_WIN_COUNTS = { 'baseline-v1-gen1-A': 5 }

test('真机四条摘要全部本地化：无英文布尔、无 key=value', () => {
  for (const s of [...REAL_SUMMARIES, REAL_UNSTABLE]) {
    const out = localizeEvolutionSummary(s)
    assert.ok(!/=/.test(out), out)
    assert.ok(!/\bTrue\b|\bFalse\b/.test(out), out)
    assert.ok(!/,/.test(out), out)
    assert.ok(out.startsWith('演化完成：'), out)
  }
})

test('逐字对照：True→是、False→否、胜者名原样保留', () => {
  assert.equal(
    localizeEvolutionSummary(REAL_SUMMARIES[0]),
    '演化完成：胜者 baseline-v1-gen1-A；稳定：是；已部署：是',
  )
  assert.equal(
    localizeEvolutionSummary(REAL_SUMMARIES[3]),
    '演化完成：胜者 baseline-v1-gen1-C；稳定：是；已部署：否',
  )
  assert.equal(
    localizeEvolutionSummary(REAL_UNSTABLE),
    '演化完成：胜者 baseline-v1-gen1-A；稳定：否；已部署：否',
  )
})

test('不认识的键值不翻译也不丢（不编造中文名）', () => {
  assert.equal(
    localizeEvolutionSummary('演化完成: variant=α-3, ok=True'),
    '演化完成：variant α-3；ok：是',
  )
  assert.equal(boolishValueText('True'), '是')
  assert.equal(boolishValueText('false'), '否')
  assert.equal(boolishValueText('None'), '未记录')
  assert.equal(boolishValueText('baseline-v1'), 'baseline-v1')
})

test('摘要缺失时返回空串，由页面显示占位而不是编造"进化完成"', () => {
  assert.equal(localizeEvolutionSummary(''), '')
  assert.equal(localizeEvolutionSummary(null), '')
  assert.equal(localizeEvolutionSummary(undefined), '')
})

test('评分映射不再直出 JSON 花括号与引号', () => {
  const out = scoreMapText(REAL_SCORES)
  assert.ok(!out.includes('{'), out)
  assert.ok(!out.includes('"'), out)
  assert.equal(out, 'baseline-v1-gen1-A 8.5、baseline-v1-gen1-B 6、baseline-v1-gen1-C 7.8')
  assert.equal(scoreMapText(REAL_WIN_COUNTS), 'baseline-v1-gen1-A 5')
  assert.equal(scoreMapText({}), '')
  assert.equal(scoreMapText(null), '')
})

test('评分项过多时如实说明总数（不静默丢弃）', () => {
  const many = {}
  for (let i = 0; i < 9; i++) many[`s${i}`] = i
  const out = scoreMapText(many, 6)
  assert.match(out, /等 9 项/)
  assert.equal(out.split('、').length, 6)
})
