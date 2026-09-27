// C3 的行为测试：可行动状态 → 页面意图。
//
// 反例（原指令 §4-C3）：新人看得出"没成功"，看不出"该做什么"；而"未知/等材料"这类
// 中性状态容易被渲染成绿色（读成"一切正常"）。这里钉住入口映射与配色语义。
import assert from 'node:assert/strict'
import test from 'node:test'

const { actionIntent, shouldShowActionable, actionableTone } = await import('../src/lib/actionable.ts')

test('补材料 → 切到补材料标签（不是跳走）', () => {
  assert.deepEqual(actionIntent('add_material'), { kind: 'switch-tab', tab: 'materials' })
})

test('看详情 → 切到结果标签', () => {
  assert.deepEqual(actionIntent('view_details'), { kind: 'switch-tab', tab: 'results' })
})

test('模型未配置 → 去设置；资料源不可用 → 去健康页', () => {
  assert.deepEqual(actionIntent('open_settings'), { kind: 'navigate', to: '/settings' })
  assert.deepEqual(actionIntent('open_health'), { kind: 'navigate', to: '/health' })
})

test('重试 → 重新提交', () => {
  assert.deepEqual(actionIntent('retry'), { kind: 'retry' })
})

test('等待/无动作/未知动作都不给按钮', () => {
  for (const a of ['wait', 'none', '', undefined, 'something-new']) {
    assert.deepEqual(actionIntent(a), { kind: 'none' }, String(a))
  }
})

test('执行中与已完成不显示"该做什么"提示', () => {
  assert.equal(shouldShowActionable({ state: 'running', message: 'x' }), false)
  assert.equal(shouldShowActionable({ state: 'done', message: 'x' }), false)
})

test('待材料/待消费/失败/未接收要显示提示', () => {
  for (const s of ['waiting_material', 'pending_consume', 'failed', 'not_received']) {
    assert.equal(shouldShowActionable({ state: s, message: '需要处理' }), true, s)
  }
})

test('没有状态或没有文字时不显示（不占版面说空话）', () => {
  assert.equal(shouldShowActionable(null), false)
  assert.equal(shouldShowActionable({ state: 'failed' }), false)
  assert.equal(shouldShowActionable({ state: 'failed', message: '  ' }), false)
})

test('配色语义：只有已完成算绿；未知/待消费不得算绿', () => {
  assert.equal(actionableTone('done'), 'ok')
  assert.equal(actionableTone('failed'), 'bad')
  assert.equal(actionableTone('waiting_material'), 'warn')
  assert.equal(actionableTone('pending_consume'), 'warn')
  assert.equal(actionableTone('not_received'), 'idle')
  assert.equal(actionableTone(''), 'idle')
  assert.notEqual(actionableTone('pending_consume'), 'ok')
})
