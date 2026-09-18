// S2 行为测试：研究入口的契约拼装 + 底稿摘要（纯函数，不依赖 DOM）。
//
// 要钉住的两条：
//   1) 表单不完整时**不拼目标**，而是给出缺口（不靠猜公司/期间/口径）；
//   2) 底稿摘要只报底稿里真实存在的东西——缺证据不算达成，缺字段显示"未知"。

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { buildResearchGoal, summarizeWorkingPaper } from '../src/lib/researchGoal.ts'

test('完整表单拼出符合契约的目标', () => {
  const { goal, gaps } = buildResearchGoal({
    company: '贵州茅台', yearFrom: '2024', yearTo: '2023',
    caliber: '合并', asOf: '2025-04-30',
  })
  assert.deepEqual(gaps, [])
  assert.match(goal, /贵州茅台/)
  assert.match(goal, /2023 与 2024 两个年度/)
  assert.match(goal, /营业收入、归母净利润、经营活动现金流净额/)
  assert.match(goal, /合并报表口径/)
  assert.match(goal, /数据截至 2025-04-30/)
})

test('缺公司/年度/口径/截至日时给出缺口且不拼目标', () => {
  const { goal, gaps } = buildResearchGoal({ company: '', yearFrom: '2024' })
  assert.equal(goal, '', '不完整就不生成目标（不猜）')
  assert.equal(gaps.length, 4, gaps.join('|'))
  assert.ok(gaps.some((g) => g.includes('公司')))
  assert.ok(gaps.some((g) => g.includes('两个年度')))
  assert.ok(gaps.some((g) => g.includes('口径')))
  assert.ok(gaps.some((g) => g.includes('截至日')))
})

test('两个年度相同视为缺口', () => {
  const { gaps } = buildResearchGoal({
    company: '示例公司', yearFrom: '2024', yearTo: '2024',
    caliber: '合并', asOf: '2025-04-30',
  })
  assert.ok(gaps.some((g) => g.includes('需不同')), gaps.join('|'))
})

test('参考资料作为附注进入目标', () => {
  const { goal } = buildResearchGoal({
    company: '示例公司', yearFrom: '2023', yearTo: '2024',
    caliber: '合并', asOf: '2025-04-30', materials: '年报 PDF 与官网新闻稿',
  })
  assert.match(goal, /参考资料：年报 PDF 与官网新闻稿/)
})

// ── A 批：结构化契约字段（与目标一起提交，提交时落库）──────────────

test('完整表单同时给出结构化契约字段', () => {
  const { fields, gaps } = buildResearchGoal({
    company: '贵州茅台', companyId: '600519.SH', market: 'cn',
    yearFrom: '2023', yearTo: '2024', caliber: '合并', asOf: '2025-04-30',
    materials: '年报链接',
  })
  assert.deepEqual(gaps, [])
  assert.deepEqual(fields, {
    company: '贵州茅台', company_id: '600519.SH', market: 'cn',
    caliber: '合并', as_of: '2025-04-30',
    year_from: 2023, year_to: 2024, periods: [2023, 2024],
    materials: '年报链接',
  })
})

test('市场未选时**不填默认值**（不替你猜 A 股），字段里就没有这一项', () => {
  const { fields } = buildResearchGoal({
    company: '示例公司', yearFrom: '2023', yearTo: '2024',
    caliber: '合并', asOf: '2025-04-30',
  })
  assert.ok(fields)
  assert.equal('market' in fields, false, '不能默认成 cn/hk/us')
  assert.equal('company_id' in fields, false)
})

test('市场只接受 cn/hk/us；非法值同样不写进字段', () => {
  const { fields } = buildResearchGoal({
    company: '示例公司', market: 'A股', yearFrom: '2023', yearTo: '2024',
    caliber: '合并', asOf: '2025-04-30',
  })
  assert.equal('market' in fields, false, '中文写法不当作市场代码')
})

test('表单不完整时既不给目标也不给字段', () => {
  const { goal, fields, gaps } = buildResearchGoal({ company: '', yearFrom: '2024' })
  assert.equal(goal, '')
  assert.equal(fields, null)
  assert.ok(gaps.length > 0)
})

test('只填稳定标识栏也算有效身份（与后端契约同规则）', () => {
  const { goal, fields, gaps } = buildResearchGoal({
    company: '', companyId: '600519.SH', yearFrom: '2023', yearTo: '2024',
    caliber: '合并', asOf: '2025-04-30',
  })
  assert.deepEqual(gaps, [])
  assert.match(goal, /600519\.SH/)
  assert.equal(fields.company, '600519.SH')
  assert.equal(fields.company_id, '600519.SH')
})

test('公司栏写代码：作为 company_id 一并送出，市场仍不猜', () => {
  const { fields } = buildResearchGoal({
    company: '600519.SH', yearFrom: '2023', yearTo: '2024',
    caliber: '合并', asOf: '2025-04-30',
  })
  assert.equal(fields.company, '600519.SH')
  assert.equal(fields.company_id, '600519.SH')
  assert.equal('market' in fields, false, '市场由后端按代码后缀规范化，前端不猜')
})

const PAPER = {
  ok: false,
  request: { as_of: '2025-04-30' },
  completeness: { required: 6, present: 4, missing: ['经营活动现金流净额 2023年'] },
  rows: [
    { fact_id: 'fact-1', metric_label: '营业收入', entity: '示例公司', entity_id: '000001',
      period: '2024年', currency: 'CNY', unit: '亿元', value: 1380.0,
      verify_state: 'unverified', source_url: 'https://example.invalid/a' },
    { fact_id: 'fact-2', metric_label: '归母净利润', entity: '示例公司', period: '2024年',
      currency: 'CNY', unit: '亿元', value: null },
  ],
  derived: [
    { fact_id: 'fact-d1', metric: 'revenue_yoy', period: '2024年同比', value: 15.0,
      formula: '(1380 - 1200) / 1200 * 100', derived_from: ['fact-1', 'fact-0'] },
  ],
  gaps: [{ kind: 'fact', detail: '缺少必需事实：经营活动现金流净额 2023年' }],
  problems: [{ kind: 'subject_mismatch', detail: '主体与请求不一致：不得算已核验' }],
}

test('底稿摘要如实转述：未达成 + 缺口 + 待核验', () => {
  const s = summarizeWorkingPaper(PAPER)
  assert.equal(s.hasPaper, true)
  assert.equal(s.goalMet, false, '底稿判未达成就不能显示达成')
  assert.equal(s.present, 4)
  assert.equal(s.required, 6)
  assert.deepEqual(s.missing, ['经营活动现金流净额 2023年'])
  assert.equal(s.gaps.length, 1)
  assert.equal(s.problems.length, 1)
  assert.equal(s.asOf, '2025-04-30')
})

test('明细缺字段显示“未知”而不是 0', () => {
  const s = summarizeWorkingPaper(PAPER)
  const profit = s.facts.find((f) => f.label === '归母净利润')
  assert.ok(profit, '应能找到归母净利润那一行')
  assert.equal(profit.value, '未知')
  assert.equal(profit.entity, '示例公司')
})

test('派生值带公式与输入 fact_id', () => {
  const s = summarizeWorkingPaper(PAPER)
  assert.equal(s.derived.length, 1)
  const d = s.derived[0]
  assert.equal(d.derived, true)
  assert.match(d.formula, /1380 - 1200/)
  assert.deepEqual(d.inputs, ['fact-1', 'fact-0'])
})

test('没有底稿时不假装有', () => {
  const s = summarizeWorkingPaper(null)
  assert.equal(s.hasPaper, false)
  assert.equal(s.goalMet, false)
  assert.deepEqual(s.facts, [])
})
