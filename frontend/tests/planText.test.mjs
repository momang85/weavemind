// P2-4b 行为测试：执行计划里「7 个能力步骤说明全是同一句任务目标」的折叠。
// 实机反例（docs/evidence/20261021-live-verify/README.md）：每条 instruction 都由后端拼成
//   `任务目标：<同一句>\n\n用户目标：<同一句>\n原始指令：<该步真正要做的事>…`
// 前端取整段 instruction 当行名 → 7 行看起来一模一样，真正区分各步的「原始指令」看不见。
// 处理：共用说明只显示一处；行名改成「能力动作：该步关键动作」；完整原文保留可查。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { capabilityAction, commonPrefix, foldPlanDescriptions, stepActionText }
  from '../src/lib/planText.ts'

const GOAL = '研究洋河股份 2023 与 2024 两个年度的营业收入、归母净利润、经营活动现金流净额，'
  + '合并报表口径，数据截至 2025-04-30。每个数字须能回溯到来源位置并可重算；缺证据的如实标缺口。'
  + '阅读重点：投研视角（equity）。'
const instruction = (raw) => `任务目标：${GOAL}\n\n用户目标：${GOAL}\n原始指令：${raw}`

const STEPS = [
  { capability: 'web_search', name: instruction('[检索查询] 洋河股份 2023年年度报告') },
  { capability: 'web_fetch', name: instruction('抓取与步骤2不同的一个链接：优先财务附注') },
  { capability: 'content_summary', name: instruction('只解释本次已选定的事实：逐项说明指标含义') },
  { capability: 'data_analyzer', name: instruction('按底稿数值复算同比与比率，不得自行换算') },
  { capability: 'report_generator', name: instruction('生成研究报告，按骨架写全六个小节') },
  { capability: 'package', name: instruction('将本次任务产出的所有文件打包为 ZIP') },
  { capability: 'file_io', name: instruction('把图表写到工作区 charts/ 目录') },
]

test('7 步共用同一段目标说明 → 折叠成一处，行名不再重复目标', () => {
  const r = foldPlanDescriptions(STEPS)
  assert.equal(r.shared.length, 1)
  assert.equal(r.shared[0].count, 7)
  assert.ok(r.shared[0].text.includes('研究洋河股份'), r.shared[0].text)
  // 折叠的只是"每步都一样"的那部分（两段目标说明）；各步自己的动作仍在行名里
  assert.ok(r.shared[0].text.length < STEPS[0].name.length, '折叠文本不应包含各步差异')
  for (const c of r.children) {
    assert.equal(c.folded, true)
    assert.ok(!c.name.includes('研究洋河股份'), `行名仍在重复目标：${c.name}`)
    assert.ok(c.name.length <= 80, c.name)
  }
})

test('行名 = 能力动作：该步关键动作，7 行互不相同（这才是"看得懂哪步在干什么"）', () => {
  const r = foldPlanDescriptions(STEPS)
  const names = r.children.map(c => c.name)
  assert.equal(new Set(names).size, names.length, JSON.stringify(names, null, 2))
  assert.match(names[0], /^检索公开来源：/)
  assert.match(names[1], /^抓取页面正文：抓取与步骤2/)
  assert.match(names[5], /^打包交付物：/)
  // 覆盖能力时如实写能力名，不编动作
  assert.equal(capabilityAction('未知能力x'), '执行 未知能力x')
  assert.equal(capabilityAction(''), '执行步骤')
})

test('不删信息：完整原文仍可取回（title 悬停用），且描述各不相同', () => {
  const r = foldPlanDescriptions(STEPS)
  assert.equal(r.children[1].description, STEPS[1].name)
  assert.ok(r.children[1].description.includes('抓取与步骤2不同的一个链接'))
  assert.equal(new Set(r.children.map(c => c.description)).size, 7)
})

test('各不相同/只有一步时不做折叠（不制造"共用说明"）', () => {
  const a = foldPlanDescriptions([{ capability: 'web_search', name: '检索 A 公司年报' },
    { capability: 'package', name: '打包输出' }])
  assert.deepEqual(a.shared, [])
  assert.deepEqual(a.children.map(c => c.name), ['检索 A 公司年报', '打包输出'])
  assert.equal(foldPlanDescriptions([{ capability: 'web_search', name: '只有一个步骤' }]).shared.length, 0)
  assert.deepEqual(foldPlanDescriptions([]).children, [])
})

test('公共前缀按语义边界切分，半句话不当"共用说明"', () => {
  const p = commonPrefix(['任务目标：研究 A\n原始指令：检索', '任务目标：研究 A\n原始指令：抓取'])
  assert.equal(p, '任务目标：研究 A\n原始指令：')
  assert.equal(stepActionText('原始指令：抓取与步骤2不同的一个链接：优先财务附注'),
    '抓取与步骤2不同的一个链接：优先财务附注')
  assert.equal(stepActionText('原始指令：只解释本次已选定的事实。块内列出的缺口要如实写出'),
    '只解释本次已选定的事实')
  assert.equal(stepActionText('  '), '')
  assert.ok(stepActionText('x'.repeat(200)).endsWith('…'))
  // 前缀太短（< 24 字）时不算共用，避免把"步骤1/步骤2"这类短前缀抽走
  assert.deepEqual(
    foldPlanDescriptions([{ name: '步骤1：检索' }, { name: '步骤2：抓取' }]).shared, [])
})

test('计划树按同一函数渲染（源码扫描：TaskTreeView 用 foldPlanDescriptions）', () => {
  const code = readFileSync(
    fileURLToPath(new URL('../src/components/TaskTreeView.tsx', import.meta.url)), 'utf8')
  assert.ok(/foldPlanDescriptions\(/.test(code), 'TaskTreeView 未折叠重复说明')
  assert.ok(code.includes('fold.shared'), '共用说明没有单独显示一处')
  assert.ok(/title=\{hint \|\| label \|\| node\.name\}/.test(code), '完整原文没有悬停入口')
})
