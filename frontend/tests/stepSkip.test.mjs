// 共享任务 task-1（控制台 gate_skipped 显示口径）的行为测试：
// S1 门禁跳过（资料缺口）呈现为「跳过（资料缺口）」的中性 skipped，
// 真正失败的步骤（FAILED 且不含判据）必须仍然失败——两条分支都钉住。
// 判据与后端 `_is_search_gap_skip`（orchestrator_v2.py:8536-8548）同源：
// `gate_skipped` 为真，或结果文本含「无候选 URL」（历史数据没有标记时的兜底）。
import assert from 'node:assert/strict'
import test from 'node:test'

const { isSearchGapSkip, stepDisplayStatus, stepLogStyle, GATE_SKIP_LABEL } =
  await import('../src/lib/stepSkip.ts')

// 实机形状（agents.db ui-d59152ebb9 步骤 2/2b/2c，只读验证）：result 是结构化 dict，
// status='FAILED'，gate_skipped=True，elapsed_sec=0.0，result 文本含「无候选 URL」
const gateSkippedApiStep = {
  step_id: '2', capability: 'web_fetch', instruction: '抓取公司近两期财报数据',
  result: {
    task_id: '2', status: 'FAILED', gate_skipped: true, elapsed_sec: 0.0,
    result: '无候选 URL（检索未产出可用来源），已跳过抓取；需补资料或换检索词后重试',
  },
}

// 历史数据兜底：没有 gate_skipped 标记，只有结果文本（后端谓词的第二条判据）
const gateSkippedByText = {
  step_id: '2b', capability: 'web_fetch', instruction: '抓取公司主营业务构成',
  result: { status: 'FAILED', result: '无候选 URL（检索未产出可用来源），已跳过抓取' },
}

// 真失败：FAILED 且不含任何判据——不得被洗白成跳过（语义红线）
const realFailure = {
  step_id: '7', capability: 'web_fetch', instruction: '抓取 https://example.com/report.pdf',
  result: { task_id: '7', status: 'FAILED', result: 'HTTP 503: upstream unavailable', elapsed_sec: 12.3 },
}

test('gate_skipped（结构化标记）→ 显示为跳过，不再是红色失败', () => {
  assert.equal(isSearchGapSkip(gateSkippedApiStep), true)
  assert.equal(stepDisplayStatus(gateSkippedApiStep), 'skipped')
})

test('历史数据：结果文本含「无候选 URL」也判为跳过（与后端谓词同源）', () => {
  assert.equal(isSearchGapSkip(gateSkippedByText), true)
  assert.equal(stepDisplayStatus(gateSkippedByText), 'skipped')
})

test('真失败（FAILED 且不含判据）仍然是 failed——红线：不得洗白', () => {
  assert.equal(isSearchGapSkip(realFailure), false)
  assert.equal(stepDisplayStatus(realFailure), 'failed')
})

test('只有 FAILED 会被改写：SUCCESS/RUNNING/PENDING 原样返回', () => {
  // 正文里出现「无候选 URL」字样也不改写非失败状态
  assert.equal(stepDisplayStatus({ result: { status: 'SUCCESS', result: '全文未出现无候选 URL 字样' } }), 'success')
  assert.equal(stepDisplayStatus({ result: { status: 'RUNNING' } }), 'running')
  assert.equal(stepDisplayStatus({ step_id: 'x' }), 'pending')
})

test('已映射的 TaskNode 重复判定结果一致（幂等，树和详情面板各自算一遍）', () => {
  const skippedNode = { id: '2', status: 'skipped', result: gateSkippedApiStep.result }
  assert.equal(stepDisplayStatus(skippedNode), 'skipped')
  const failedNode = { id: '7', status: 'failed', result: realFailure.result }
  assert.equal(stepDisplayStatus(failedNode), 'failed')
})

test('结果为空或形状认不出时不判跳过（判不出就判不出，不按“看起来像”猜）', () => {
  assert.equal(isSearchGapSkip(null), false)
  assert.equal(isSearchGapSkip(undefined), false)
  assert.equal(isSearchGapSkip({ step_id: 'x', result: null }), false)
  assert.equal(isSearchGapSkip({ step_id: 'x', result: '纯文本结果' }), false)
})

test('实时动态日志口径：门禁跳过走中性 info（跳过（资料缺口）： ），不进红色 error 流', () => {
  assert.deepEqual(stepLogStyle(gateSkippedApiStep, 'failed'),
    { type: 'info', prefix: GATE_SKIP_LABEL + ': ' })
})

test('实时动态日志口径：真失败仍是 error/“Failed: ”，成功仍是 dispatch/“Completed: ”', () => {
  assert.deepEqual(stepLogStyle(realFailure, 'failed'), { type: 'error', prefix: 'Failed: ' })
  assert.deepEqual(stepLogStyle({ result: { status: 'SUCCESS' } }, 'success'),
    { type: 'dispatch', prefix: 'Completed: ' })
})

test('用户口径文案固定为「跳过（资料缺口）」：中性说法，不带“失败”字样', () => {
  assert.equal(GATE_SKIP_LABEL, '跳过（资料缺口）')
})
