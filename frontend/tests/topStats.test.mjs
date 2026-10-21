// P1-2 行为测试：首屏数据卡的卡头与内容必须同源。
// 实机反例（docs/evidence/20261021-live-verify/README.md，截图 08-new-*.png）：
//   卡头写「取自报告正文【财务对照】表（指标 × 期间 × 口径 × 来源）」，
//   卡里却是 检索步总数 / 有效覆盖率 / 降级执行步 / 零来源步（那是『数据来源摘要』表，
//   表头只有「指标 | 值」），最后一格还被截成「1（由非检索模型完成，来源可能为…」。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  gapStatsOfTable, isComparisonTableHeader, isGapIndicatorsTable, nameColOf,
  parseTopStatsWithSource, periodOfHeader, reportUnitOf, topStatsCardMeta,
  topStatsOfTable, valueColOf,
} from '../src/lib/topStats.ts'

// 真机报告里的『财务对照』表（docs/evidence/.../api_task_ui-d59152ebb9.json）
const COMPARE_MD = [
  '单位：元。',
  '## 财务对照',
  '| 指标 | 2023 | 2024 | 口径 | 来源 |',
  '|---|---|---|---|---|',
  '| 营业收入 | 331.26亿元 | 288.76亿元 | 合并 | [1] |',
  '| 归母净利润 | 100.16亿元 | 66.73亿元 | 合并 | [1] |',
].join('\n')

// 『数据来源摘要』表：两列「指标 | 值」——旧判定把它当成了财务对照
const COVERAGE_MD = [
  '## 数据来源摘要',
  '| 指标 | 值 |',
  '|---|---|',
  '| 检索步总数 | 4 |',
  '| 有效覆盖率 | 75% |',
  '| 降级执行步 | 1（由非检索模型完成，来源可能为结构化数据源） |',
  '| 零来源步 | 0 |',
].join('\n')

test('只有「指标 | 值」两列的说明表不算『财务对照』表', () => {
  assert.equal(isComparisonTableHeader(['指标', '值']), false)
  assert.equal(isComparisonTableHeader(['指标', '2024', '口径', '来源']), true)
  assert.equal(isComparisonTableHeader(['指标', '数值']), false)
  assert.equal(isComparisonTableHeader(['图表', '状态']), false)
})

// ── 实机反例 ②（2026-10-22，任务 ui-8b28c95ab1 的正文真实表格）──────────────
// 正文里**没有**财务对照表，只有这张指标缺口表；旧实现判成 metrics，且把「数值」列
// 当名字列，卡片长成「未获取（缺口）」/「人民币（单位待核）」×2，指标名与期间全丢。
const GAP_MD = [
  '## 指标缺口表',
  '| 年度 | 数值 | 单位 | 报表名称 | 行项目 | 页码/表格编号 | 原始来源 URL | 状态 |',
  '|---|---|---|---|---|---|---|---|',
  '| 2023 | 未获取（缺口） | 人民币（单位待核） | 合并利润表 | 营业收入（待定位至具体行次） | 无法定位（无披露原文） | 无 | 检索零命中 |',
  '| 2024 | 未获取（缺口） | 人民币（单位待核） | 合并利润表 | 营业收入（待定位至具体行次） | 无法定位（无披露原文） | 无 | 检索零命中 |',
].join('\n')

test('缺口表不得冒充『财务对照』表，也不得把「数值」列当指标名列', () => {
  const hdr = ['年度', '数值', '单位', '报表名称', '行项目', '页码/表格编号', '原始来源 URL', '状态']
  assert.equal(isComparisonTableHeader(hdr), false)
  assert.equal(isGapIndicatorsTable(hdr), true)
  assert.equal(nameColOf(hdr), 4)                    // 行项目，不是「数值」
  assert.equal(valueColOf(hdr, nameColOf(hdr)), 1)   // 数值
  const r = parseTopStatsWithSource(GAP_MD)
  assert.equal(r.source, 'metrics_gap')
  const meta = topStatsCardMeta({ source: r.source, periods: r.periods, unit: r.unit })
  assert.equal(meta.title, '关键指标（本次均为缺口）')
  assert.ok(!meta.detail.includes('取自报告正文『财务对照』表'), meta.detail)
  assert.match(meta.detail, /指标缺口表/, meta.detail)
  assert.match(meta.detail, /不用估计值代替/, meta.detail)
  assert.match(meta.detail, /2023年、2024年/, meta.detail)   // 行级期间写出来
  // 键是指标名 + 期间；值是「数值」列，不是「单位」列
  assert.deepEqual(r.stats.map(x => x.k), [
    '营业收入（待定位至具体行次） · 2023年',
    '营业收入（待定位至具体行次） · 2024年',
  ])
  for (const it of r.stats) assert.equal(it.v, '未获取（缺口）')
  // 直接喂表也给同样的键值对（不依赖调用方判断）
  const g = gapStatsOfTable([hdr, ...GAP_MD.split('\n').slice(3).map(l => l.split('|').slice(1, -1).map(c => c.trim()))])
  assert.equal(g.stats.length, 2)
  assert.equal(g.stats[0].v, '未获取（缺口）')
})

test('卡头按实际取到的表生成：取到财务对照才说“财务对照”', () => {
  const r = parseTopStatsWithSource(COMPARE_MD)
  assert.equal(r.source, 'metrics')
  const meta = topStatsCardMeta({ source: r.source, periods: r.periods, unit: r.unit })
  assert.equal(meta.title, '关键数据速览')
  assert.match(meta.detail, /财务对照/)
  assert.match(meta.detail, /2024年、2023年/, meta.detail)   // 用到的期间写出来
  assert.match(meta.detail, /单位 元/, meta.detail)
  // 内容与卡头同源：卡里的每一项都带期间
  for (const it of r.stats) assert.match(it.k, / · 20\d{2}年/, it.k)
})

test('退回首表时卡头如实写「正文首表前 4 行 / 未判定为结论」，不再谎称财务对照', () => {
  const r = parseTopStatsWithSource(COVERAGE_MD)
  assert.equal(r.source, 'first_table')
  const meta = topStatsCardMeta({ source: r.source, periods: r.periods, unit: r.unit })
  assert.equal(meta.title, '正文首表前 4 行')
  assert.ok(!meta.detail.includes('取自报告正文『财务对照』'), meta.detail)
  assert.match(meta.detail, /没有『财务对照』表/, meta.detail)
  assert.match(meta.detail, /未判定为结论/, meta.detail)
  // 卡片键名仍是表里的指标名（覆盖率那一行不会被说成财务指标）
  assert.ok(r.stats.some(x => x.k === '有效覆盖率'), JSON.stringify(r.stats))
})

test('单元格内容完整可见：卡里不做单行截断（值按 32 字上限整格换行显示）', () => {
  const r = parseTopStatsWithSource(COVERAGE_MD)
  const degraded = r.stats.find(x => x.k === '降级执行步')
  assert.ok(degraded, JSON.stringify(r.stats))
  // 旧文案里被截成「1（由非检索模型完成，来源可能为…」的整句在 32 字内可完整取出
  assert.equal(degraded.v.startsWith('1（由非检索模型完成'), true)
  const code = readFileSync(
    fileURLToPath(new URL('../src/components/ReportViewer.tsx', import.meta.url)), 'utf8')
  const card = code.slice(code.indexOf('F3a 数据卡'), code.indexOf('accTrace ?'))
  assert.ok(!/\btruncate\b/.test(card), '数据卡仍在用 truncate 单行截断')
  assert.ok(/break-words/.test(card), '数据卡缺少换行样式（长句会被裁掉）')
})

test('期间/单位解析：读不到就不写，不猜', () => {
  assert.equal(periodOfHeader('2024'), '2024年')
  assert.equal(periodOfHeader('**2023年**'), '2023年')
  assert.equal(periodOfHeader('2024A'), '2024年')
  assert.equal(periodOfHeader('同比'), '')
  assert.equal(periodOfHeader('口径'), '')
  assert.equal(reportUnitOf(COMPARE_MD), '元')
  assert.equal(reportUnitOf('没有声明单位'), '')
  assert.equal(topStatsCardMeta({ source: 'none' }).title, '')
  // 没有期间列时不硬凑期间标签
  const t = topStatsOfTable([['指标', '值'], ['营业收入', '288.76亿元']])
  assert.deepEqual(t.periods, [])
  assert.deepEqual(t.stats, [{ k: '营业收入', v: '288.76亿元' }])
})
