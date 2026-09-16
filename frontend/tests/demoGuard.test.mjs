// 演示守卫的真实行为测试（Node 内存 fetch 替身，不打真实接口）。
//
// 为什么不是"查前缀"：前缀清单对不对，不代表请求真的没发出去、也不代表退出演示后能恢复。
// 这里用计数替身断言**实际出网次数**，并覆盖架构复核点名的两条：
//   1) `POST /api/kill-worker` 与 `GET /api/memory/summary`（冷缓存 / ?refresh=1 会调付费模型）在演示下不出网；
//   2) `?demo` 进入后退出（setDemoActive(false)）必须真的不再拦截，再次开启要能恢复。

import assert from 'node:assert/strict'
import test from 'node:test'

const guard = await import('../src/lib/demoGuard.ts')

function setup(search = '') {
  const calls = []
  globalThis.window = {
    location: { search },
    fetch: async (input, init) => {
      calls.push({ url: String(input), method: (init && init.method) || 'GET' })
      return new Response(JSON.stringify({ ok: true }), { status: 200 })
    },
  }
  guard.resetDemoGuardForTest()
  guard.installDemoGuard()
  return calls
}

test('?demo 进入：终止 worker 与付费摘要 GET 都不出网，返回可读的 403 原因', async () => {
  const calls = setup('?demo')
  assert.equal(guard.demoActive(), true, 'URL 带 ?demo 时应处于演示模式')

  const kill = await window.fetch('/api/kill-worker', { method: 'POST' })
  assert.equal(kill.status, 403, '终止 worker 必须被拦（真实路由是 POST /api/kill-worker）')
  assert.equal((await kill.json()).demo_blocked, true)

  const cold = await window.fetch('/api/memory/summary')
  assert.equal(cold.status, 403, '记忆摘要 GET 冷缓存会调付费模型，必须被拦')

  const warm = await window.fetch('/api/memory/summary?refresh=1')
  assert.equal(warm.status, 403, '记忆摘要 GET ?refresh=1 必须被拦')

  assert.equal(calls.length, 0, `演示模式下不应有任何真实出网，实际发出 ${calls.length} 次：${JSON.stringify(calls)}`)
})

test('演示模式下只读接口仍走真实服务（不是把所有请求都拦掉）', async () => {
  const calls = setup('?demo')
  const res = await window.fetch('/api/status')
  assert.equal(res.status, 200)
  assert.deepEqual(calls, [{ url: '/api/status', method: 'GET' }])
})

test('退出演示后不再拦截，再次开启恢复拦截（?demo 只做初始化）', async () => {
  const calls = setup('?demo')

  // 退出：显式开关必须压过 URL 参数，否则"界面显示已退出、守卫仍在拦"
  guard.setDemoActive(false)
  assert.equal(guard.demoActive(), false, '退出后 demoActive() 必须为 false（不能继续被 ?demo 拽住）')

  const afterExit = await window.fetch('/api/kill-worker', { method: 'POST' })
  assert.equal(afterExit.status, 200, '退出演示后请求应真实发出')
  assert.equal(calls.length, 1)

  // 再次开启
  guard.setDemoActive(true)
  assert.equal(guard.demoActive(), true)
  const again = await window.fetch('/api/kill-worker', { method: 'POST' })
  assert.equal(again.status, 403)
  assert.equal(calls.length, 1, '再次开启后不应再出网')
})

test('未显式开关时按当前 URL 判定（初始进入路径）', async () => {
  globalThis.window = { location: { search: '?demo' }, fetch: async () => new Response('{}') }
  guard.resetDemoGuardForTest()
  assert.equal(guard.demoActive(), true)
  globalThis.window.location.search = ''
  assert.equal(guard.demoActive(), false, 'URL 去掉 ?demo 后（未显式设置过）应回到关闭')
})

test('会话接口不被拦（否则用户退出不了演示）', () => {
  assert.equal(guard.demoBlockReason('/api/logout', 'POST', true), null)
  assert.equal(guard.demoBlockReason('/api/login', 'POST', true), null)
})
