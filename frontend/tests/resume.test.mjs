// U90-B 行为测试：「继续执行」按钮的统一口径（纯逻辑，不依赖 DOM）。
//
// 要钉住的三条：
//   1) 只有后端判 `available=true`（FAILED/CRASHED）才显示按钮；SUCCESS/CANCELLED/
//      RUNNING/未知都不显示，且**要给一句为什么**（不可逆终态不许悄悄给按钮）；
//   2) 恢复请求的**在途闸**：同一任务的第二次点击不发第二次 fetch；
//   3) 服务端错误原文透传（409 只有失败的任务可恢复 / 503 Redis 未连接），
//      不吞成通用失败文案。
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  resumeVisible, resumeLabel, resumeBlockedReason,
  requestResume, resumeInFlight,
} from '../src/lib/resume.ts'

test('只有 FAILED/CRASHED 才显示继续执行按钮', () => {
  assert.equal(resumeVisible({ available: true, status: 'FAILED' }), true)
  assert.equal(resumeVisible({ available: true, status: 'CRASHED' }), true)
  assert.equal(resumeVisible({ available: false, status: 'SUCCESS' }), false)
  assert.equal(resumeVisible({ available: false, status: 'CANCELLED' }), false)
  assert.equal(resumeVisible({ available: false, status: 'RUNNING' }), false)
  assert.equal(resumeVisible({ available: false, status: '' }), false)
  assert.equal(resumeVisible(null), false)
  assert.equal(resumeVisible(undefined), false)
})

test('按钮文案带跳过步数；缺失时按完成步数兜底', () => {
  assert.equal(
    resumeLabel({ available: true, label: '继续执行（跳过 3/5 步）', completed: 3 }),
    '继续执行（跳过 3/5 步）',
  )
  assert.equal(resumeLabel({ available: true, completed: 0 }), '重新接管执行')
  assert.equal(resumeLabel({ available: true }), '继续执行')
  assert.equal(resumeLabel({ available: false, status: 'SUCCESS' }), '')
})

test('不可恢复状态给一句为什么（不给空话）', () => {
  const done = resumeBlockedReason({
    available: false, status: 'SUCCESS',
    reason: '任务已完成：结论是不可重跑的终态。',
  })
  assert.match(done, /终态/)
  const cancelled = resumeBlockedReason({ available: false, status: 'CANCELLED', reason: '任务已取消' })
  assert.match(cancelled, /取消/)
  assert.equal(resumeBlockedReason({ available: true }), '')
  assert.equal(resumeBlockedReason(null), '')
})

test('在途闸：同任务第二次请求被拒绝且不发第二次 fetch', async () => {
  let calls = 0
  const slowFetch = ((_url, _init) => new Promise((resolve) => {
    calls += 1
    setTimeout(() => resolve(new Response(JSON.stringify({ status: 'ok', note: '已请求恢复' }), { status: 200 })), 30)
  }))
  const first = requestResume('ui-double', slowFetch)
  assert.equal(resumeInFlight('ui-double'), true, '请求在途时应标记')
  const second = await requestResume('ui-double', slowFetch)
  assert.equal(second.ok, false)
  assert.match(second.message, /请勿重复点击/)
  await first
  assert.equal(calls, 1, '第二次点击不得发出第二次请求')
  assert.equal(resumeInFlight('ui-double'), false, '结束后应解除标记')
})

test('失败任务的恢复成功后返回服务端 note', async () => {
  const fakeFetch = async () => new Response(
    JSON.stringify({ status: 'ok', note: '已请求恢复；编排器将按持久化步骤状态续跑' }),
    { status: 200 },
  )
  const r = await requestResume('ui-ok', fakeFetch)
  assert.equal(r.ok, true)
  assert.match(r.message, /续跑/)
})

test('服务端错误原文透传（不吞成通用失败）', async () => {
  const cases = [
    [409, '只有失败的任务可以恢复（当前 RUNNING）', /只有失败的任务可以恢复/],
    [503, 'Redis 未连接，无法恢复任务', /Redis 未连接/],
  ]
  for (const [status, error, expect] of cases) {
    const fakeFetch = async () => new Response(JSON.stringify({ error }), { status })
    const r = await requestResume('ui-err', fakeFetch)
    assert.equal(r.ok, false)
    assert.match(r.message, expect)
  }
})

// ─────────────────────────────────────────────────────────────────────────────
// U90-R3：**页面调用状态层**的三条验证（不只测 requestResume 助手）。
// 下面每个用例都按页面真实调用顺序驱动同一状态层：
//   doResume = 置 pending（beginResumeNote）→ requestResume → applyResumeResult
//   → 按钮态由 resumeButtonState(note, view) 推导 → 失败可再点时再走一遍
// 覆盖：503 失败 → 再点重试成功；挂起超时 → 先查真实状态、不重复恢复；
//      在途双击去重（只发一次请求）。
import {
  beginResumeNote, applyResumeResult, resumeButtonState, noteFromResult,
} from '../src/lib/resume.ts'

/** 页面点击一次（与 History.doResume / ConsoleSideTabs.doResume 同序）。 */
async function pageClick(notes, tid, fetchImpl, opts) {
  const before = notes[tid]
  if (before && (before.phase === 'pending' || before.phase === 'ok')) {
    return { notes, skipped: true, result: null }   // 在途/已接管：页面直接返回
  }
  let next = beginResumeNote(notes, tid)
  const result = await requestResume(tid, fetchImpl, opts)
  next = applyResumeResult(next, tid, result)
  return { notes: next, skipped: false, result }
}

test('R3：503 失败后按钮仍可点，再点即重试成功（失败可再次操作）', async () => {
  let calls = 0
  const fetchImpl = async () => {
    calls += 1
    if (calls === 1) {
      return new Response(JSON.stringify({
        error: 'Redis 未连接，无法恢复任务；稍后可再试',
        status: 'FAILED', retryable: true,
      }), { status: 503 })
    }
    return new Response(JSON.stringify({ status: 'ok', note: '已请求恢复；将跳过已成功步骤' }),
      { status: 200 })
  }
  const view = { available: true, label: '继续执行（跳过 2/5 步）' }
  let notes = {}

  const first = await pageClick(notes, 'ui-503', fetchImpl)
  notes = first.notes
  assert.equal(first.result.ok, false)
  assert.equal(first.result.phase, 'failed')
  assert.equal(notes['ui-503'].retryable, true, '503（发布失败已回滚）应标记可重试')
  let btn = resumeButtonState(notes['ui-503'], view)
  assert.equal(btn.disabled, false, '失败后按钮不得永久禁用（旧缺陷）')
  assert.match(btn.label, /重试/)
  assert.match(btn.hint, /Redis 未连接/, '失败原文必须显示给用户')

  const second = await pageClick(notes, 'ui-503', fetchImpl)
  notes = second.notes
  assert.equal(second.skipped, false, '失败状态下再点必须真的重发请求')
  assert.equal(second.result.ok, true)
  assert.equal(notes['ui-503'].phase, 'ok')
  btn = resumeButtonState(notes['ui-503'], view)
  assert.equal(btn.disabled, true, '成功后禁用（列表刷新会移除按钮）')
  assert.equal(btn.label, '已请求恢复')
  assert.equal(calls, 2, '恰好两次请求：失败一次 + 重试一次')
})

test('R3：挂起超时 → 先查真实任务状态，已在运行则不重复恢复', async () => {
  let posts = 0, statusQueries = 0
  // 真实 fetch 在被 abort 时 reject；替身按同一契约（否则超时路径测不到）
  const hangFetch = (_url, init) => {
    posts += 1
    return new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new Error('aborted')))
    })
  }
  const statusImpl = async () => { statusQueries += 1; return 'RUNNING' }
  const notes = beginResumeNote({}, 'ui-hang')
  const r = await requestResume('ui-hang', hangFetch, { timeoutMs: 20, statusImpl })
  const done = applyResumeResult(notes, 'ui-hang', r)
  assert.equal(r.phase, 'ok', '超时但查到 RUNNING = 已接管，按成功回执')
  assert.equal(r.resumed, true)
  assert.match(r.message, /已在运行/)
  assert.equal(statusQueries, 1, '超时后必须先查询真实状态再裁决')
  assert.equal(posts, 1, '超时不得自动重发 POST（防重复恢复）')
  assert.equal(resumeButtonState(done['ui-hang'], { available: true }).disabled, true)
  assert.equal(resumeInFlight('ui-hang'), false, '超时结算后必须解除在途标记')
})

test('R3：超时后确认任务仍失败 → 明确"可重试"，重试发出第二次请求', async () => {
  let posts = 0
  const hangFetch = (_url, init) => {
    posts += 1
    return new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new Error('aborted')))
    })
  }
  const statusImpl = async () => 'FAILED'
  const r1 = await requestResume('ui-hang2', hangFetch, { timeoutMs: 20, statusImpl })
  assert.equal(r1.phase, 'timeout')
  assert.equal(r1.retryable, true)
  assert.equal(r1.taskStatus, 'FAILED')
  assert.match(r1.message, /仍是 FAILED/)
  assert.match(r1.message, /不会双重接管/)
  const btn = resumeButtonState(noteFromResult(r1), { available: true })
  assert.equal(btn.disabled, false)
  assert.match(btn.label, /重试/)
  assert.equal(posts, 1)

  // 重试：这次服务端正常返回
  const okFetch = async () => new Response(
    JSON.stringify({ status: 'ok', note: '已请求恢复' }), { status: 200 })
  const r2 = await requestResume('ui-hang2', okFetch, { timeoutMs: 50, statusImpl })
  assert.equal(r2.ok, true)
  assert.equal(posts, 1, '正常路径不查询状态、不重复 POST')
})

test('R3：查不到真实状态时如实说"未确认"，但仍允许重试（服务端有状态门槛）', async () => {
  const hangFetch = (_url, init) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => reject(new Error('aborted')))
  })
  const r = await requestResume('ui-hang3', hangFetch,
    { timeoutMs: 20, statusImpl: async () => '' })
  assert.equal(r.phase, 'timeout')
  assert.equal(r.retryable, true)
  assert.match(r.message, /未能确认任务当前状态/)
  assert.equal(resumeButtonState(noteFromResult(r), { available: true }).disabled, false)
})

test('R3：在途双击只发一次请求（页面 pending 短路 + 在途闸双保险）', async () => {
  let calls = 0
  const slowFetch = (_url, init) => new Promise((resolve) => {
    calls += 1
    setTimeout(() => resolve(new Response(
      JSON.stringify({ status: 'ok', note: '已请求恢复' }), { status: 200 })), 30)
  })
  let notes = {}
  const first = pageClick(notes, 'ui-dbl', slowFetch, { timeoutMs: 500 })
  notes = beginResumeNote(notes, 'ui-dbl')
  // 在途：页面按钮禁用 + 再点直接短路（不发第二次请求）
  assert.equal(resumeButtonState(notes['ui-dbl'], { available: true }).disabled, true)
  const second = await pageClick(notes, 'ui-dbl', slowFetch, { timeoutMs: 500 })
  assert.equal(second.skipped, true)
  assert.equal(second.result, null)
  const done = await first
  assert.equal(calls, 1, '双击只允许一次真实请求')
  assert.equal(done.result.ok, true)
})

test('R3：不可恢复终态（409 retryable=false）→ 按钮禁用并说明原因', async () => {
  const fetchImpl = async () => new Response(JSON.stringify({
    error: '只有失败的任务可以恢复（当前 SUCCESS）',
    status: 'SUCCESS', retryable: false,
  }), { status: 409 })
  const r = await requestResume('ui-term', fetchImpl)
  assert.equal(r.ok, false)
  assert.equal(r.retryable, false, '服务端判定不可重试时不得放开按钮')
  const btn = resumeButtonState(noteFromResult(r), { available: true })
  assert.equal(btn.disabled, true)
  assert.match(btn.hint, /只有失败的任务可以恢复/)
})
