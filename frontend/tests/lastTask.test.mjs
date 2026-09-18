// 运行中任务刷新后恢复跟踪：恢复点读写 + "哪些状态才恢复"的判定。
//
// 背景（实机）：`currentTaskId` 只在内存 store 里，运行中重载后前端不再跟踪该任务
// ——后端仍有该任务日志，页面的日志/进度区是空的（实测 DOM 行数 0）。
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  clearLastTask, readLastTask, saveLastTask, shouldResume,
} from '../src/lib/lastTask.ts'

function withFakeStorage() {
  const data = new Map()
  const fake = {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)) },
    removeItem: (k) => { data.delete(k) },
    clear: () => data.clear(),
    key: () => null,
    get length() { return data.size },
  }
  globalThis.sessionStorage = fake
  return data
}

test('恢复点：写入后能读回 id 与开始时间', () => {
  withFakeStorage()
  saveLastTask('ui-abc123', 1690000000000)
  assert.deepEqual(readLastTask(), { id: 'ui-abc123', startedAt: 1690000000000 })
})

test('恢复点：没有 / 脏数据 / 缺 id 都当作"没有"，不猜', () => {
  withFakeStorage()
  assert.equal(readLastTask(), null)
  globalThis.sessionStorage.setItem('wm.lastTask', '{不是 json')
  assert.equal(readLastTask(), null)
  globalThis.sessionStorage.setItem('wm.lastTask', JSON.stringify({ startedAt: 1 }))
  assert.equal(readLastTask(), null)
})

test('恢复点：开始时间缺失或非法时退化为 0（由调用方用当前时间兜底）', () => {
  withFakeStorage()
  saveLastTask('ui-x')
  const got = readLastTask()
  assert.ok(got && got.id === 'ui-x')
  assert.ok(got.startedAt > 0)
  globalThis.sessionStorage.setItem('wm.lastTask', JSON.stringify({ id: 'ui-y', startedAt: 'x' }))
  assert.deepEqual(readLastTask(), { id: 'ui-y', startedAt: 0 })
})

test('恢复点：clear 之后读不到（"新对话"要能真的清掉）', () => {
  withFakeStorage()
  saveLastTask('ui-z')
  clearLastTask()
  assert.equal(readLastTask(), null)
})

test('没有 sessionStorage（隐私模式/被禁用）时不抛异常', () => {
  const had = Object.prototype.hasOwnProperty.call(globalThis, 'sessionStorage')
  const prev = globalThis.sessionStorage
  delete globalThis.sessionStorage
  try {
    saveLastTask('ui-no-store')
    assert.equal(readLastTask(), null)
    clearLastTask()
  } finally {
    if (had) globalThis.sessionStorage = prev
  }
})

test('只有未终态才恢复跟踪：运行中/排队/等确认要恢复，终态不恢复', () => {
  for (const s of ['RUNNING', 'PENDING', 'QUEUED', 'AWAITING_CONFIRM', 'running']) {
    assert.equal(shouldResume(s), true, s)
  }
  for (const s of ['SUCCESS', 'SUCCESS_WITH_ISSUES', 'FAILED', 'CANCELLED', '', null, undefined, 'unknown']) {
    assert.equal(shouldResume(s), false, String(s))
  }
})
