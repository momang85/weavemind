// C/S2 行为测试：时间与预算的显示口径（未知不当 0、非法值不显示 "Invalid Date"）。
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { formatBudgetLabel, formatDuration, formatLocalTime } from '../src/lib/format.ts'

test('非法/缺失时间显示“时间未知”', () => {
  assert.equal(formatLocalTime(undefined), '时间未知')
  assert.equal(formatLocalTime(''), '时间未知')
  assert.equal(formatLocalTime('这不是时间'), '时间未知')
  assert.equal(formatLocalTime('2024-13-45T99:99:99'), '时间未知')
})

test('合法时间按本地时区显示（含日期与时间）', () => {
  const out = formatLocalTime('2024-05-06T07:08:09Z')
  assert.notEqual(out, '时间未知')
  assert.match(out, /\d/)
  assert.ok(!out.includes('Invalid Date'), out)
})

test('预算：0 / 缺省 = 不限，而不是“用尽”', () => {
  assert.equal(formatBudgetLabel(0, 0), '不限')
  assert.equal(formatBudgetLabel(undefined, undefined), '不限')
  assert.equal(formatBudgetLabel(null, null), '不限')
})

test('预算：限额已知但剩余未知时说未知', () => {
  assert.equal(formatBudgetLabel(40, null), '限额 40（剩余未知）')
  assert.equal(formatBudgetLabel(40, 12), '剩余 12/40')
})

test('时长：未知/未完成不给 0 秒', () => {
  assert.equal(formatDuration(null), '—')
  assert.equal(formatDuration(0), '—')
  assert.equal(formatDuration(90), '1 分 30 秒')
})
