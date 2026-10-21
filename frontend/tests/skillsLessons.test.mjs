// S1 行为测试：Skill「自动沉淀教训」正文不得硬截断（实机反例：/api/skills 原文被 slice(0,80) 切成断句）。
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { lessonTruncatedNote, skillLessonView, splitLessonSegments } from '../src/lib/skillLessons.ts'

// 2026-10-22 真机 /api/skills 原文（skill data-pipeline 的教训 id 77b09c3a，逐字）
const RAW_ISSUE = '来源声明检查 17 条，虚假标注 17 条（与换算日期、数值逐字一致、'
  + '中属于其他主体的数值、的分级判定标准、」列填写对应 [n] 编号：）；建议补录域名媒体映射：主流财经；'
  + '建议补录域名媒体映射：媒体的转载报；目标要求引用官方/可核实来源，报告未给出任何来源链接或来源清单条目'
// 同页第三条教训（id 6c47ef4a）与 financial-research 卡片下的长教训（真机原文开头）
const RAW_ISSUE_SHORT = '来源声明检查 2 条，虚假标注 2 条（、时间、地域、单位与口径、的同一指标分列并注明差异）；'
  + '目标要求引用官方/可核实来源，报告未给出任何来源链接或来源清单条目'
const RAW_ISSUE_LONG_PREFIX = '反思要求重做：web_search 步骤返回了与贵州茅台财报无关的贵州省人民政府网站'

test('旧实现的 80 字硬截断确实是断句且无省略号（回归反例，固定证据）', () => {
  const legacy = RAW_ISSUE.slice(0, 80)
  assert.equal(legacy.length, 80)
  assert.ok(!legacy.endsWith('…'), '旧实现不加省略号')
  assert.ok(RAW_ISSUE.length > 80, '原文比 80 字长')
  assert.ok(!legacy.endsWith('）'), '旧实现把括号内的内容从中切断')
  // 真机可见文本停在「…建议补录域名媒体映射」，后面的「：主流财经」被吞掉
  assert.ok(!legacy.includes('：主流财经'))
})

test('新实现不截断：整段完整、括号闭合、被吞掉的尾部回来了', () => {
  const view = skillLessonView({ issue: RAW_ISSUE, skill: '' })
  assert.equal(view.fullText, RAW_ISSUE)
  assert.equal(view.length, RAW_ISSUE.length)
  assert.equal(view.truncated, false)
  assert.equal(view.shownLength, RAW_ISSUE.length)
  assert.ok(view.fullText.includes('建议补录域名媒体映射：主流财经'))
  assert.ok(view.fullText.includes('建议补录域名媒体映射：媒体的转载报'))
  assert.ok(view.segments[0].endsWith('）'), '第一条分段以右括号结束，不再是断句')
  assert.equal(lessonTruncatedNote(view), '')
})

test('教训按分号拆成多条并列问题，逐条可读', () => {
  const view = skillLessonView({ issue: RAW_ISSUE })
  assert.equal(view.segments.length, 4)
  assert.deepEqual(view.segments, [
    '来源声明检查 17 条，虚假标注 17 条（与换算日期、数值逐字一致、中属于其他主体的数值、的分级判定标准、」列填写对应 [n] 编号：）',
    '建议补录域名媒体映射：主流财经',
    '建议补录域名媒体映射：媒体的转载报',
    '目标要求引用官方/可核实来源，报告未给出任何来源链接或来源清单条目',
  ])
  assert.deepEqual(splitLessonSegments(RAW_ISSUE_SHORT), [
    '来源声明检查 2 条，虚假标注 2 条（、时间、地域、单位与口径、的同一指标分列并注明差异）',
    '目标要求引用官方/可核实来源，报告未给出任何来源链接或来源清单条目',
  ])
})

test('长教训正文一字不丢（真机 financial-research 卡下的长文本）', () => {
  const long = RAW_ISSUE_LONG_PREFIX + '，导致后续步骤未能获取2023和2024年营业收入、归母净利润的具体数值及可回溯来源。'
  const view = skillLessonView({ issue: long, skill: 'financial-research' })
  assert.equal(view.fullText, long)
  assert.equal(view.scope, 'bound')
  assert.equal(view.scopeNote, '')
  assert.ok(view.fullText.endsWith('可回溯来源。'))
})

test('未绑定 Skill 的教训如实标注（真机：同一条出现在多张卡片下）', () => {
  const unbound = skillLessonView({ issue: RAW_ISSUE, skill: '' })
  assert.equal(unbound.scope, 'unbound')
  assert.match(unbound.scopeNote, /未绑定具体 Skill/)
  assert.match(unbound.scopeNote, /多张 Skill 卡片/)
  assert.equal(skillLessonView({ issue: RAW_ISSUE, skill: 'data-pipeline' }).scope, 'bound')
  assert.equal(skillLessonView({ issue: RAW_ISSUE }).scope, 'unknown')
})

test('确需限长时：加省略号并给出"已截断"说明', () => {
  const view = skillLessonView({ issue: RAW_ISSUE }, 80)
  assert.equal(view.truncated, true)
  assert.equal(view.length, RAW_ISSUE.length)
  assert.equal(view.shownLength, 80)
  assert.ok(view.fullText.length > 80, 'fullText 仍是全文，供详情/悬停使用')
  assert.match(lessonTruncatedNote(view), /已截断，全文 \d+ 字/)
})

test('空教训不编内容', () => {
  const view = skillLessonView({ issue: '', skill: 'x' })
  assert.equal(view.fullText, '')
  assert.deepEqual(view.segments, [])
  assert.equal(view.length, 0)
})

// 真机 financial-research 卡片下的最后一条教训：正文正好 300 字、结尾停在数字「202」
// （后端 skill_registry.py:147 落库时 str(issue)[:300]，且不加省略号）
const RAW_CAPPED = '反思要求重做：web_search 步骤返回了与贵州茅台财报无关的贵州省人民政府网站，导致后续步骤未能获取2023和2024年'
  + '营业收入、归母净利润、经营活动现金流净额的具体数值及可回溯来源。修复要求：使用精准查询词（如“贵州茅台 2023年年度报告 营业收入 '
  + '归母净利润 经营活动现金流净额”、“600519 2024年报 主要会计数据”），并定向权威站点（巨潮资讯网 cninfo.com.cn、上海证券交易所 '
  + 'sse.com.cn、贵州茅台官网），返回至少2个可直接访问且包含目标数字的财报链接，每个链接需标注包含的关键数据项。验收标准：搜索结果中前3条至少2条与目标直接相关，且摘要中包含202'

test('原文被后端落库截断时不冒充完整内容：显式标注"原文即止于此"', () => {
  assert.equal(RAW_CAPPED.length, 300, '这条是真机上正好 300 字的样本')
  const view = skillLessonView({ issue: RAW_CAPPED, skill: 'financial-research' })
  assert.equal(view.length, 300)
  assert.equal(view.truncated, false, '前端没有截断它')
  assert.equal(view.tailCapped, true)
  assert.match(view.tailNote, /后端按 300 字截断/)
  assert.match(view.tailNote, /无法补全/)
})

test('正常结尾的教训不被误标为"原文被截断"', () => {
  for (const issue of [RAW_ISSUE, RAW_ISSUE_SHORT, '目标要求引用官方/可核实来源，报告未给出任何来源链接或来源清单条目。']) {
    const view = skillLessonView({ issue })
    assert.equal(view.tailCapped, false, issue.slice(0, 12))
    assert.equal(view.tailNote, '')
  }
  // 恰好 300 字但以句末标点收尾 → 不主张"被截断"
  const padded = '甲'.repeat(299) + '。'
  assert.equal(skillLessonView({ issue: padded }).tailCapped, false)
})
