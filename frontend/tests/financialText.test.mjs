import { test } from 'node:test'
import assert from 'node:assert/strict'
import { financialLabel } from '../src/lib/financialText.ts'

test('财务字段中文化，保留期间和口径', () => {
  assert.equal(financialLabel('accounts_receivable · 2024年'), '应收账款 · 2024年')
  assert.equal(financialLabel('inventory（合并）'), '存货（合并）')
  assert.equal(financialLabel('accounts_payable'), '应付账款')
})

test('净利润不擅自等同于归母净利润', () => {
  assert.equal(financialLabel('net_profit'), '净利润')
  assert.equal(financialLabel('net_profit_parent'), '归母净利润')
})

test('未知字段与原有中文、单位数字原样保留', () => {
  assert.equal(financialLabel('unmapped_metric · 2023年'), 'unmapped_metric · 2023年')
  assert.equal(financialLabel('营业收入 · 120 百万元'), '营业收入 · 120 百万元')
})
