// S2 行为测试：设置页「需要处理」区块不再直出被截断的原始 JSON——要中文结论、处理建议、
// 相同原因合并、原文放详情且如实标注"服务端已截断"。
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  groupDegradedRequirements, healthReasonView, requirementFieldLabel,
} from '../src/lib/depHealth.ts'

// 2026-10-22 真机 /api/config/requirements 的 health_reason（health_registry.py:317 截断到 120 字）
const RAW_LIVE = 'HTTP 400: {"code":"MODEL_CAPABILITY_NOT_SUPPORTED","message":"当前模型不支持该能力：embeddings",'
  + '"data":{"capability":"embeddings"},'
// 复核截图（recheck/settings.txt:25-27）当时页面上的形态：带 "Embedding API " 前缀、截在 "capability":
const RAW_SCREENSHOT = 'Embedding API HTTP 400: {"code":"MODEL_CAPABILITY_NOT_SUPPORTED",'
  + '"message":"当前模型不支持该能力：embeddings","data":{"capability":'

test('上游 400 报文解析出中文结论与错误码（真机原文）', () => {
  const v = healthReasonView(RAW_LIVE)
  assert.equal(v.httpStatus, 400)
  assert.equal(v.code, 'MODEL_CAPABILITY_NOT_SUPPORTED')
  assert.equal(v.message, '当前模型不支持该能力：embeddings')
  assert.equal(v.capability, 'embeddings')
  assert.equal(v.conclusion, '上游返回 HTTP 400：当前模型不支持该能力：embeddings（错误码 MODEL_CAPABILITY_NOT_SUPPORTED）')
  assert.match(v.advice, /embeddings/)
  assert.match(v.advice, /测试连通性/)
  assert.match(v.advice, /关键词兜底/)
})

test('复核对不上：截断后仍能解析（带前缀的旧形态同样可用）', () => {
  const v = healthReasonView(RAW_SCREENSHOT)
  assert.equal(v.httpStatus, 400)
  assert.equal(v.code, 'MODEL_CAPABILITY_NOT_SUPPORTED')
  assert.equal(v.message, '当前模型不支持该能力：embeddings')
  // capability 落在被截掉的尾部时取不到——不猜、不编造
  assert.equal(v.capability, '')
  assert.match(v.conclusion, /当前模型不支持该能力：embeddings/)
})

test('原文不完整要显式说明"服务端已截断"，且前端不自行截断、不加省略号', () => {
  const v = healthReasonView(RAW_LIVE)
  assert.equal(v.raw, RAW_LIVE, '原文原样保留（前端不 slice）')
  assert.equal(v.rawIncomplete, true)
  assert.match(v.rawNote, /截断/)
  assert.match(v.rawNote, /不补全、不猜测/)
  assert.ok(!v.raw.endsWith('…'), '不再用省略号掩盖断口')
  // 旧实现会渲染出长度 120 的断口 JSON（引号/括号不闭合），这是被修掉的现象
  assert.ok(!/^\{/.test(v.raw) || v.rawIncomplete)
})

test('相同原因合并为一条，并列出全部受影响字段（真机：3 字段 1 原因）', () => {
  const items = [
    { path: 'embedding.api_key', label: 'API Key', reason: RAW_LIVE },
    { path: 'embedding.base_url', label: 'Base URL', reason: RAW_LIVE },
    { path: 'embedding.model', label: '模型', reason: RAW_LIVE },
  ]
  const groups = groupDegradedRequirements(items)
  assert.equal(groups.length, 1, '3 条重复项合并成 1 个原因，不再让用户以为是 3 个故障')
  assert.equal(groups[0].fields.length, 3)
  assert.deepEqual(groups[0].fields, [
    'API Key（embedding.api_key）',
    'Base URL（embedding.base_url）',
    '模型（embedding.model）',
  ])
  assert.equal(groups[0].paths.length, 3)
})

test('不同原因不合并；字段写法统一', () => {
  const groups = groupDegradedRequirements([
    { path: 'llm.api_key', label: 'API Key', reason: 'HTTP 401: unauthorized' },
    { path: 'embedding.model', label: '模型', reason: RAW_LIVE },
  ])
  assert.equal(groups.length, 2)
  assert.equal(requirementFieldLabel('模型', 'embedding.model'), '模型（embedding.model）')
  assert.equal(requirementFieldLabel('', 'embedding.model'), 'embedding.model')
  assert.equal(requirementFieldLabel('模型', ''), '模型')
})

test('按错误类型给可操作建议（401/402/404/5xx/超时）', () => {
  assert.match(healthReasonView('HTTP 401: unauthorized').advice, /API Key/)
  assert.match(healthReasonView('HTTP 402: insufficient balance').advice, /额度不足/)
  assert.match(healthReasonView('HTTP 404: not found').advice, /Base URL/)
  assert.match(healthReasonView('HTTP 503: bad gateway').advice, /上游服务异常/)
  assert.match(healthReasonView('连接 timeout').advice, /网络不可达或超时/)
})

test('非 JSON 原因不编造：中文原样、空值说未取得', () => {
  assert.equal(healthReasonView('降级').conclusion, '降级')
  assert.match(healthReasonView('').conclusion, /未取得失败原因/)
  const long = '这是一句很长的中文原因说明'.repeat(20)
  const v = healthReasonView(long)
  assert.match(v.conclusion, /结论已截断/)
  assert.ok(v.conclusion.length <= 200)
})
