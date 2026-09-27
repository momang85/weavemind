// C3/H3b 的行为测试：提交幂等键的分配规则。
//
// 反例（原指令 §3.4）：网页每次提交都生成新任务 id，双击或失败重试会变成**第二次真实
// 执行**（重复付费、重复写库）。这里钉住三条行为：
//   1) 同一次提交意图的重复请求 → 复用同一个键（服务端据此去重）；
//   2) 内容变了 → 新键（用户改了题目就是一次新提交）；
//   3) 上一次已被接收（pending 被清空）后再提交 → 新键（主动再跑一次不算重复）。
import assert from 'node:assert/strict'
import test from 'node:test'

const { resolveSubmissionKey, submissionSignature } = await import('../src/lib/submissionKey.ts')

const makeSeq = () => {
  let n = 0
  return () => `k-${++n}`
}

test('同一次提交的重复点击复用同一个键', () => {
  const make = makeSeq()
  const input = { goal: '研究洋河股份', project: 'default' }
  const first = resolveSubmissionKey(null, input, make)
  const second = resolveSubmissionKey(first.pending, input, make)
  assert.equal(first.key, 'k-1')
  assert.equal(second.key, 'k-1', '重复点击必须复用键，否则服务端会新建第二个任务')
  assert.equal(make().toString(), 'k-2', '只应该生成过一次键')
})

test('空白差异不影响复用（trim 后同一意图）', () => {
  const make = makeSeq()
  const first = resolveSubmissionKey(null, { goal: '研究洋河股份' }, make)
  const second = resolveSubmissionKey(first.pending, { goal: '  研究洋河股份  ' }, make)
  assert.equal(second.key, first.key)
})

test('提交内容改变 → 换新键', () => {
  const make = makeSeq()
  const first = resolveSubmissionKey(null, { goal: '研究洋河股份' }, make)
  const other = resolveSubmissionKey(first.pending, { goal: '研究贵州茅台' }, make)
  assert.equal(other.key, 'k-2', '改了题目是新的一次提交，不能复用旧键')
})

test('同名但研究契约不同也算不同提交', () => {
  const make = makeSeq()
  const first = resolveSubmissionKey(
    null, { goal: '研究洋河股份', fields: { company: '洋河股份', periods: [2023, 2024] } }, make)
  const other = resolveSubmissionKey(
    first.pending,
    { goal: '研究洋河股份', fields: { company: '洋河股份', periods: [2022, 2023] } }, make)
  assert.notEqual(other.key, first.key, '契约（期间）不同必须视为新提交')
})

test('上一次已被接收（pending 清空）后再提交 → 新键', () => {
  const make = makeSeq()
  const input = { goal: '研究洋河股份' }
  const first = resolveSubmissionKey(null, input, make)
  // 提交成功：调用方把 pending 清空，用户再点就是一次新提交
  const again = resolveSubmissionKey(null, input, make)
  assert.equal(again.key, 'k-2')
})

test('签名对渠道字段敏感：项目/会话/确认模式/模板/上下文', () => {
  const base = { goal: 'g' }
  const variants = [
    { ...base, project: 'p2' },
    { ...base, conversationId: 'c1' },
    { ...base, confirmMode: true },
    { ...base, templateName: 't1' },
    { ...base, context: 'ctx' },
  ]
  const baseSig = submissionSignature(base)
  for (const v of variants) {
    assert.notEqual(submissionSignature(v), baseSig, `字段变化必须改变签名：${JSON.stringify(v)}`)
  }
})
