// API 结果判定的真实行为测试：403 演示拦截、真实失败、成功三种结果必须区分开。
//
// 背景（架构复核 01:05）：Memory 的删除/演化/审批不检查 res.ok，403 被拦下仍提示"删除完成"。
// 这里用真实 Response 对象验证判定结果，而不是断言"fetch 返回 403"。

import assert from 'node:assert/strict'
import test from 'node:test'

const { apiOutcome, readApiOutcome } = await import('../src/lib/apiResult.ts')

test('2xx → 成功，用调用方给的文案', () => {
  const out = apiOutcome(200, { summary: 'x' }, '删除完成', '删除失败')
  assert.deepEqual(out, { ok: true, blocked: false, message: '删除完成', status: 200 })
})

test('403 + demo_blocked → 演示拦截，提示里带服务端原因（不得显示成功）', async () => {
  const reason = '演示模式：该操作会真实修改数据或产生费用，已被拦截。'
  const res = new Response(JSON.stringify({ error: reason, demo_blocked: true }), {
    status: 403, headers: { 'Content-Type': 'application/json' },
  })
  const out = await readApiOutcome(res, '删除完成', '删除失败')
  assert.equal(out.ok, false)
  assert.equal(out.blocked, true)
  assert.equal(out.message, reason, '应原样给出拦截原因，而不是"删除完成"')
})

test('非 2xx 的真实失败 → 失败 + HTTP 状态码 + 服务端原因', async () => {
  const res = new Response(JSON.stringify({ error: '数据库忙' }), { status: 500 })
  const out = await readApiOutcome(res, '删除完成', '删除失败')
  assert.equal(out.ok, false)
  assert.equal(out.blocked, false)
  assert.match(out.message, /删除失败/)
  assert.match(out.message, /HTTP 500/)
  assert.match(out.message, /数据库忙/)
})

test('403 但不是演示拦截（例如权限不足）→ 按失败处理', async () => {
  const res = new Response(JSON.stringify({ error: '需要管理员' }), { status: 403 })
  const out = await readApiOutcome(res, '删除完成', '删除失败')
  assert.equal(out.ok, false)
  assert.equal(out.blocked, false)
  assert.match(out.message, /HTTP 403/)
})

test('响应体不是 JSON 也不抛异常（仍按状态码判定）', async () => {
  const res = new Response('<html>gateway</html>', { status: 502 })
  const out = await readApiOutcome(res, '删除完成', '删除失败')
  assert.equal(out.ok, false)
  assert.match(out.message, /HTTP 502/)
})
