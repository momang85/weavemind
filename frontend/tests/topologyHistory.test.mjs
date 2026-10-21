// 真机复验（2026-10-22）行为测试：智能体拓扑标签布局 + 布局类修复。
//
// 反例（桌面 1600 与移动端都出现）：拓扑内圈标签叠印成 `datalaanalymodel_train`、
// `webfetchwo`、`fileiowork`、`codeexecwo`、`rengengene`、`search age`、`content su`。
// 反例（历史页，唯一 P0）：375 宽下列表项标题被挤成 `w=0` 完全不可见，
// 「继续对话」按钮区间 248–338 压盖时间戳 230–348（重叠 90px）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { placeTopologyLabels, shortAgentLabel, topologyNodes } from '../src/lib/topologyLayout.ts'

const read = p => readFileSync(fileURLToPath(new URL(p, import.meta.url)), 'utf8')

// 实机 11 个智能体（id + 能力徽章），顺序与 `/api/status` 一致
const AGENTS = [
  { agent_id: 'codeexecworker', capabilities: 'code_execution' },
  { agent_id: 'content_summarizer', capabilities: 'content_summary' },
  { agent_id: 'dataanalyzerworker', capabilities: 'data_analyzer' },
  { agent_id: 'dataloaderworker', capabilities: 'data_loader' },
  { agent_id: 'fileioworker', capabilities: 'file_io' },
  { agent_id: 'modeltrainerworker', capabilities: 'model_trainer' },
  { agent_id: 'packaging_worker', capabilities: 'package' },
  { agent_id: 'reactagentworker', capabilities: 'react_agent' },
  { agent_id: 'reportgeneratorworker', capabilities: 'report_generator' },
  { agent_id: 'webfetchworker', capabilities: 'web_fetch' },
  { agent_id: 'search_agent', capabilities: 'web_search' },
]

// 12px 字号下的近似文字宽度：中日韩字符全宽，其余约半宽（用于注入 measure）
const measure = t => Array.from(t).reduce((w, ch) => w + (/[\u3000-\u9fff\uff00-\uffef]/.test(ch) ? 12 : 6.6), 0)

test('标签用能力中文短名，最长也只有几个字', () => {
  assert.equal(shortAgentLabel({ agent_id: 'dataloaderworker', capabilities: 'data_loader' }), '数据加载')
  assert.equal(shortAgentLabel({ agent_id: 'search_agent', capabilities: 'web_search' }), '网络检索')
  assert.equal(shortAgentLabel({ agent_id: 'reportgeneratorworker', capabilities: 'report_generator' }), '报告生成')
  // 未知能力退回可读机器名（worker 后缀前补空格；过长时丢掉末尾角色词），且不超长
  assert.equal(shortAgentLabel({ agent_id: 'dataloaderworker' }), 'dataloader')
  assert.ok(shortAgentLabel({ agent_id: 'a_very_long_unknown_worker_name' }).length <= 12)
  assert.equal(shortAgentLabel(null), '未命名')
})

test('11 个标签两两不重叠，且没有叠印出的乱码', () => {
  const W = 1152, H = 320
  const nodes = topologyNodes(AGENTS.length, W, H)
  assert.equal(nodes.length, 11)
  // 节点均匀分布在圆环上，且互不重叠
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      assert.ok(Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y) > nodes[i].r + nodes[j].r,
        `节点 ${i} 与 ${j} 重叠`)
    }
  }
  const layout = placeTopologyLabels(nodes, (_n, i) => shortAgentLabel(AGENTS[i]),
    W / 2, H / 2, W, H, measure)
  // 核心断言：任何两个已放置标签的包围盒都不相交
  for (let i = 0; i < layout.placed.length; i++) {
    for (let j = i + 1; j < layout.placed.length; j++) {
      const a = layout.placed[i], b = layout.placed[j]
      const overlap = a.x < b.x + b.w + 1 && b.x < a.x + a.w + 1
        && a.y < b.y + b.h + 1 && b.y < a.y + a.h + 1
      assert.ok(!overlap, `标签「${a.text}」与「${b.text}」仍然重叠`)
    }
  }
  // 标签必须都在画布内，并且"放不下"的以隐藏计数如实告知，不是悄悄叠印
  for (const l of layout.placed) {
    assert.ok(l.x >= 0 && l.y >= 0 && l.x + l.w <= W && l.y + l.h <= H,
      `标签「${l.text}」超出画布`)
  }
  assert.equal(layout.placed.length + layout.hidden.length, nodes.length)
  // 旧实现的典型乱码不可能再出现（这些是英文 id 截 10 字后叠印的结果）
  const joined = layout.placed.map(l => l.text).join('')
  for (const garble of ['datalaanalymodel_train', 'webfetchwo', 'rengengene', 'search age', 'content su']) {
    assert.ok(!joined.includes(garble), `仍出现叠印乱码 ${garble}`)
  }
})

test('窄画布（移动端）也不叠印：放不下的标签改为隐藏并计数', () => {
  const W = 343, H = 320
  const nodes = topologyNodes(AGENTS.length, W, H)
  const layout = placeTopologyLabels(nodes, (_n, i) => shortAgentLabel(AGENTS[i]),
    W / 2, H / 2, W, H, measure)
  for (let i = 0; i < layout.placed.length; i++) {
    for (let j = i + 1; j < layout.placed.length; j++) {
      const a = layout.placed[i], b = layout.placed[j]
      assert.ok(!(a.x < b.x + b.w + 1 && b.x < a.x + a.w + 1 && a.y < b.y + b.h + 1 && b.y < a.y + a.h + 1),
        `窄画布下「${a.text}」与「${b.text}」仍重叠`)
    }
  }
  assert.equal(layout.placed.length + layout.hidden.length, nodes.length)
  assert.equal(layout.hidden.length, 0, '375 宽的移动端画布应当能放下全部标签')
})

test('画布再窄时改为隐藏标签并计数，绝不叠印', () => {
  const W = 170, H = 320
  const nodes = topologyNodes(AGENTS.length, W, H)
  const layout = placeTopologyLabels(nodes, (_n, i) => shortAgentLabel(AGENTS[i]),
    W / 2, H / 2, W, H, measure)
  for (let i = 0; i < layout.placed.length; i++) {
    for (let j = i + 1; j < layout.placed.length; j++) {
      const a = layout.placed[i], b = layout.placed[j]
      assert.ok(!(a.x < b.x + b.w + 1 && b.x < a.x + a.w + 1 && a.y < b.y + b.h + 1 && b.y < a.y + a.h + 1),
        `极窄画布下「${a.text}」与「${b.text}」仍重叠`)
    }
  }
  assert.equal(layout.placed.length + layout.hidden.length, nodes.length)
  assert.ok(layout.hidden.length > 0, '极窄画布应确实放不下全部标签（否则本用例失去意义）')
  // 隐藏数量必须如实上报给用户，不能悄悄少画
  assert.match(read('../src/components/AgentTopology.tsx'), /hiddenCount/)
})

test('历史页列表项在窄屏可换行且标题有保底宽度（P0 回归）', () => {
  const src = read('../src/pages/History.tsx')
  // 行容器必须允许换行，标题必须保底宽度，否则 flex-1 + truncate 会被固定项挤成 0 宽
  assert.match(src, /flex flex-wrap items-center gap-x-3 gap-y-2/)
  assert.match(src, /flex-1 min-w-\[9rem\] text-slate-300 text-sm truncate">\{c\.title/)
  assert.match(src, /flex-1 min-w-\[9rem\] text-slate-300 text-sm truncate">\{t\.goal\}/)
  // 图标不能再让 flex 压缩（否则标题也塌）
  assert.ok(!/ChevronRight className="w-4 h-4 text-slate-500" \/>/.test(src))
  // 50 条上限要如实告知
  assert.match(src, /const LIST_LIMIT = 50/)
  assert.match(src, /只显示最近 \{LIST_LIMIT\} 条/)
})

test('智能体页与拓扑组件不再出现英文/幻数/叠印实现', () => {
  const agents = read('../src/pages/Agents.tsx')
  assert.ok(!/m ago|h ago/.test(agents), 'timeAgo 仍有英文 ago')
  assert.ok(!/agent\.avgTime \?\? '-'\}s/.test(agents), '平均耗时仍会渲染成 -s')
  assert.ok(!/successRate \?\? 0\}%/.test(agents), '成功率仍把未知渲染成 0%')
  assert.match(agents, /未取得/)
  assert.match(agents, /flex flex-wrap gap-2 sm:gap-3/, '筛选行仍不可换行')
  assert.match(agents, /每 3 秒自动重试/)
  const topo = read('../src/components/AgentTopology.tsx')
  assert.ok(!/agent_id\.replace\('_', ' '\)\.slice\(0, 10\)/.test(topo), '拓扑仍用截 10 字的英文 id 当标签')
  assert.match(topo, /placeTopologyLabels/)
  assert.match(topo, /个标签画布放不下已省略/)
})
