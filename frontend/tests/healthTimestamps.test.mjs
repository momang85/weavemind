import test from 'node:test'
import assert from 'node:assert/strict'
import { readableEventMessage, localizeTimestamps } from '../src/lib/healthEvents.ts'

// 实机样本：健康页事件时间线里出现过的原始事件体（2026-10-21 22:01）。
const GUARDIAN_HEARTBEAT =
  '{"service": "worker_guardian", "timestamp": "2026-10-21T14:01:49.686895+00:00", "workers_tracked": 11, "workers_healthy": 11}'

test('带 +00:00 的事件体时间戳换算成本地墙钟，不再出现裸 ISO', () => {
  const out = readableEventMessage(GUARDIAN_HEARTBEAT)
  assert.ok(!/\d{4}-\d{2}-\d{2}T/.test(out), `仍残留裸 ISO：${out}`)
  assert.ok(!out.includes('+00:00'), `仍残留 UTC 偏移：${out}`)
  assert.match(out, /\d{2}-\d{2} \d{2}:\d{2}:\d{2}/)
  // 与同一行的行头口径一致：本地 8 时区下应为 22:01:49，而不是 14:01:49。
  const m = out.match(/(\d{2}):(\d{2}):(\d{2})/)
  const expected = new Date('2026-10-21T14:01:49.686Z')
  assert.equal(m[1], String(expected.getHours()).padStart(2, '0'))
  assert.equal(m[2], String(expected.getMinutes()).padStart(2, '0'))
})

test('被截断的事件体：既能归一，又保留"已截断"提示', () => {
  const raw = GUARDIAN_HEARTBEAT.slice(0, GUARDIAN_HEARTBEAT.length - 1)
  const out = readableEventMessage(raw)
  assert.ok(out.includes('后端已截断'), out)
  assert.ok(!out.includes('+00:00'), out)
})

test('裸串（本仓 strftime 写入 = 本地墙钟）只改格式，不做时区换算', () => {
  assert.equal(localizeTimestamps('packaged_at 2026-10-21T20:15:00'), 'packaged_at 10-21 20:15:00')
  assert.equal(localizeTimestamps('fetched_at 2026-10-21 20:15:00'), 'fetched_at 10-21 20:15:00')
})

test('Z 后缀与已带偏移的串都正确归一（断言与运行环境时区无关）', () => {
  // 两种写法表达同一时刻，归一端点后必须相同
  assert.equal(localizeTimestamps('2026-10-21T14:00:00Z'), localizeTimestamps('2026-10-21T14:00:00+00:00'))
  // 不写死"22:00:00"：CI 跑在 UTC，本地是 UTC+8，写死就只能在东八区通过。
  // 正确口径是"按本机时区换算"，所以期望值也从 Date 推导。
  const expected = new Date('2026-10-21T22:00:00+08:00')
  const hh = String(expected.getHours()).padStart(2, '0')
  const mm = String(expected.getMinutes()).padStart(2, '0')
  assert.match(localizeTimestamps('2026-10-21T22:00:00+08:00'),
    new RegExp(`10-21 ${hh}:${mm}:00`))
  // 带本地偏移的串换算后与同刻的 Z 串一致（同一时刻同一显示）
  assert.equal(localizeTimestamps('2026-10-21T22:00:00+08:00'),
    localizeTimestamps('2026-10-21T14:00:00Z'))
})

test('不可解析的串原样保留，不编造', () => {
  assert.equal(localizeTimestamps('2026-13-45T99:99:99Z'), '2026-13-45T99:99:99Z')
  assert.equal(localizeTimestamps('无时间'), '无时间')
})

test('普通文本与空值不受影响', () => {
  assert.equal(localizeTimestamps(''), '')
  assert.equal(localizeTimestamps('跟踪 worker 数 11'), '跟踪 worker 数 11')
  assert.equal(readableEventMessage(''), '')
})
