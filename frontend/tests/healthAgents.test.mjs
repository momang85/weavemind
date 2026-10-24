// 实机复验（2026-10-22）行为测试：健康页事件流 + 智能体卡片显示名。
//
// 反例 ①（健康页）：事件消息是后端事件体原样透出的 JSON，页面上出现
//   `{"service": "worker_guardian", …"workers_healthy"`（花括号 + 引号 + 被截断）。
// 反例 ②（健康页）：事件时间是后端 UTC ISO，直接显示比本地墙钟早 8 小时。
// 反例 ③（智能体页）：显示名 = `agent_id` 首字母大写 → `Dataloaderworker` /
//   `Reportgeneratorworker`（粘连机器名），且同页 `Search Agent` 风格不一致。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { eventTypeLabel, formatEventTime, readableEventMessage } from '../src/lib/healthEvents.ts'
import { agentDisplayName, agentRawId } from '../src/lib/agentLabel.ts'

// ── 反例 ①：结构化事件体 ────────────────────────────────────────────────
const REAL_EVENT = '{"service": "worker_guardian", "timestamp": "2026-10-21T12:54:02.138020+00:00", '
  + '"workers_tracked": 11, "workers_healthy"'

test('结构化事件体读成一行中文，被截断时如实标注', () => {
  const out = readableEventMessage(REAL_EVENT)
  assert.ok(!out.includes('{') && !out.includes('"'), out)      // 不再有 JSON 语法
  assert.match(out, /服务 worker_guardian/)
  assert.match(out, /跟踪 worker 数 11/)
  assert.match(out, /事件体过长，后端已截断/, out)                 // 截断要说明
  // 完整的 JSON 不该被标成截断
  const full = readableEventMessage('{"service": "worker_guardian", "workers_tracked": 11}')
  assert.match(full, /服务 worker_guardian/)
  assert.ok(!full.includes('已截断'), full)
})

test('普通中文消息与后端英文短句都照实显示，不吞内容', () => {
  const zh = '能力-执行者错配：web_search 错配 32 步（白名单外执行者：content_summarizer）'
  assert.equal(readableEventMessage(zh), zh)                     // 中文原样
  assert.equal(readableEventMessage('Evolution started'), '进化轮次已启动')
  assert.equal(readableEventMessage('Failed - backend unreachable'), '失败——后端不可达')
  assert.equal(readableEventMessage(''), '')
  // 无法逐字段解析时也要给出可读说明，而不是空白
  assert.match(readableEventMessage('{坏数据'), /结构化事件/)
})

test('事件类别徽章也是中文；未知类别原样返回不编', () => {
  assert.equal(eventTypeLabel('guardian'), '守护进程')
  assert.equal(eventTypeLabel('GUARDIAN'), '守护进程')
  assert.equal(eventTypeLabel('crash'), '崩溃')
  assert.equal(eventTypeLabel('什么类别'), '什么类别')
  assert.equal(eventTypeLabel(''), '')
  const health = readFileSync(
    fileURLToPath(new URL('../src/pages/Health.tsx', import.meta.url)), 'utf8')
  assert.ok(!health.includes('uppercase text-slate-600">{e.type}'), '类别徽章仍直接显示英文类型')
})

// ── 反例 ②：时间必须落在本地墙上时钟 ─────────────────────────────────────
test('事件时间按浏览器本地时区显示，且与 Date 的本地读数一致', () => {
  const iso = '2026-10-21T12:54:02.138020+00:00'
  const d = new Date(iso)
  const p = (n) => String(n).padStart(2, '0')
  const expect = `${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  assert.equal(formatEventTime(iso), expect)
  // 与"当天本地日期"一致，而不是 UTC 日期（跨时区时两者会差一天）
  assert.ok(formatEventTime(iso).startsWith(p(d.getMonth() + 1) + '-' + p(d.getDate())))
  // 时钟串原样、无法解析的原样、空串为空
  assert.equal(formatEventTime('14:35'), '14:35')
  assert.equal(formatEventTime('不是时间'), '不是时间')
  assert.equal(formatEventTime(''), '')
})

// ── 反例 ③：智能体显示名 ────────────────────────────────────────────────
test('智能体卡片按能力给中文名，并保留原始运行标识', () => {
  const loader = { agent_id: 'dataloaderworker', capabilities: 'data_loader' }
  assert.equal(agentDisplayName(loader), '数据加载')
  assert.equal(agentRawId(loader), 'dataloaderworker')            // 原始 id 仍可见（可追溯）
  assert.equal(agentDisplayName({ agent_id: 'reportgeneratorworker', capabilities: 'report_generator' }),
    '报告生成')
  assert.equal(agentDisplayName({ agent_id: 'search_agent', capabilities: 'web_search' }), '网络检索')
  // 未知能力：退回可读标题（机器名后缀补空格），不再粘连
  assert.equal(agentDisplayName({ agent_id: 'dataloaderworker', capabilities: 'mystery' }),
    'Dataloader Worker')
  assert.equal(agentDisplayName({ agent_id: 'report_generator' }), 'Report Generator')
  assert.equal(agentDisplayName(null), '未命名智能体')
})

test('页面不再用 agent_id 首字母大写当名字，也不再漏英文文案', () => {
  const read = p => readFileSync(fileURLToPath(new URL(p, import.meta.url)), 'utf8')
  const agents = read('../src/pages/Agents.tsx')
  assert.ok(!agents.includes("agent.agent_id.replace(/_/g, ' ').replace(/\\b\\w/g"),
    'Agents.tsx 仍在用 agent_id 首字母大写当名字')
  assert.ok(agents.includes('agentDisplayName(agent)'), 'Agents.tsx 未接显示名')
  assert.ok(agents.includes('agentRawId(agent)'), 'Agents.tsx 未显示原始标识')
  assert.ok(!agents.includes('Search agents...'), 'Agents.tsx 仍有英文占位符')
  assert.ok(!agents.includes('Agent Topology'), 'Agents.tsx 仍有英文标题')
  const topo = read('../src/components/AgentTopology.tsx')
  assert.ok(!/agents active/.test(topo), 'AgentTopology.tsx 仍有英文 "agents active"')
  assert.ok(!/'unknown'/.test(topo), 'AgentTopology.tsx 仍有英文 "unknown"')
  const health = read('../src/pages/Health.tsx')
  for (const en of ["'Uptime'", "'Tasks Today'", "'Survival Rate'", "'LLM Tokens'",
                    'Event Timeline', 'Health Checks', "'Orchestrator'", "'Worker Pool'"]) {
    assert.ok(!health.includes(en), `Health.tsx 仍有英文标签 ${en}`)
  }
  const metrics = read('../src/pages/MetricsPage.tsx')
  assert.ok(!metrics.includes("slice(0, 19).replace('T', ' ')"),
    'MetricsPage.tsx 仍把 UTC ISO 切片当本地时间显示')
})
