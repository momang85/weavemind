// P2-4 行为测试：带 ?task=<id> 打开时，第一屏应是任务本身，不是完整的「研究一家公司」表单。
// 实机反例（docs/evidence/20261021-live-verify/README.md P2-4）：深链打开一个已有任务，
// 第一屏仍是公司/期间/口径/阅读重点的完整表单，报告被挤到下面。
// 规则：已打开某个任务（深链或本会话跟踪中）→ 表单默认折叠；无任务 → 保持展开。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { shouldCollapseResearchForm } from '../src/lib/consoleLayout.ts'

const srcOf = (p) => readFileSync(fileURLToPath(new URL(p, import.meta.url)), 'utf8')

test('带 ?task=<id> → 折叠（保留展开入口）', () => {
  assert.equal(shouldCollapseResearchForm({ deepTaskId: 'ui-d59152ebb9', currentTaskId: null }), true)
})

test('没有任务（首次使用）→ 不折叠，行为与现在一致', () => {
  assert.equal(shouldCollapseResearchForm({}), false)
  assert.equal(shouldCollapseResearchForm({ deepTaskId: '', currentTaskId: '' }), false)
  assert.equal(shouldCollapseResearchForm({ deepTaskId: '  ', currentTaskId: null }), false)
  assert.equal(shouldCollapseResearchForm({ deepTaskId: null, currentTaskId: undefined }), false)
})

test('本会话已提交/恢复跟踪中的任务 → 同样折叠（第一屏先给任务）', () => {
  assert.equal(shouldCollapseResearchForm({ currentTaskId: 'ui-8b28c95ab1' }), true)
})

test('表单的展开入口必须仍然可用（defaultOpen 驱动，点击可切回）', () => {
  const form = srcOf('../src/components/console/SubmitPanel.tsx')
  assert.ok(/defaultOpen = true/.test(form), '表单缺少 defaultOpen 参数')
  assert.ok(/useState\(defaultOpen\)/.test(form), '折叠状态未由 defaultOpen 初始化')
  assert.ok(/setOpen\(o => !o\)/.test(form), '展开/收起按钮被删掉了')
  assert.ok(/defaultOpen=\{!formCollapsedByDefault\}/.test(form), 'SubmitPanel 未把折叠意图传给表单')
  const page = srcOf('../src/pages/TaskConsole.tsx')
  assert.ok(/shouldCollapseResearchForm\(/.test(page), 'TaskConsole 未按深链决定是否折叠')
  assert.ok(/formCollapsedByDefault=\{/.test(page), 'TaskConsole 未传折叠开关')
  // 深链 id 首帧就要有值（否则会先展开再收起，闪一下）
  assert.ok(/useState<string>\(\(\) => \{/.test(page), '也就是首帧没读 URL 参数')
})
