// S6 / G2 行为测试：时间一律本地时区 + 显式标注时区含义；设置页的角色与端点状态不再英文直出。
// 数据取自 2026-10-22 真机页面文本（recheck/settings.txt、recheck/memory.txt）。
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { formatLocalTimeLabel, formatLocalTimeShort } from '../src/lib/timeText.ts'
import { balanceStatusText, roleLabel, scheduledResultLabel } from '../src/lib/settingsText.ts'

const UTC_MICROS = '2026-08-21T05:38:35.970506+00:00'   // 真机：设置页「创建于 …」
const NAIVE_ISO = '2026-07-23T07:56'                    // 真机：记忆页条目时间（ISO 无时区）

test('UTC 微秒串不再直出：换算本地时间并标注"（本地时间）"', () => {
  const out = formatLocalTimeLabel(UTC_MICROS)
  assert.match(out, /（本地时间）$/)
  assert.ok(!out.includes('+00:00'), out)
  assert.ok(!out.includes('.'), out)          // 不再显示 6 位微秒
  assert.ok(!/T/.test(out), out)
  assert.ok(!out.includes('Invalid Date'), out)
  // 与 Date 的本地表示一致（同一实现路径，跨机器稳定）
  assert.equal(out, `${new Date(UTC_MICROS).toLocaleString()}（本地时间）`)
  // 非 UTC 机器上，UTC 墙钟 05:38 不得被当成本地时间显示
  if (new Date().getTimezoneOffset() !== 0) {
    assert.ok(!out.includes('5:38') && !out.includes('05:38'), `UTC 时间被当本地直出：${out}`)
  }
})

test('记忆页 ISO 无时区串：不再 slice(0,16)，改为本地时间', () => {
  const out = formatLocalTimeShort(NAIVE_ISO)
  assert.equal(out, '2026-07-23 07:56（本地时间）')
  assert.ok(!out.includes('T'), out)
  // 回归：旧实现渲染出的就是这个无时区 ISO 残片
  assert.equal(String(NAIVE_ISO).slice(0, 16), '2026-07-23T07:56')
})

test('带偏移的串在紧凑格式里同样换算本地（不误读成 UTC）', () => {
  const out = formatLocalTimeShort(UTC_MICROS)
  const d = new Date(UTC_MICROS)
  const p = n => String(n).padStart(2, '0')
  assert.equal(out, `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}（本地时间）`)
})

test('取不到时间说"时间未知"，不再用 - / — 顶数', () => {
  for (const f of [formatLocalTimeLabel, formatLocalTimeShort]) {
    assert.equal(f(undefined), '时间未知')
    assert.equal(f(''), '时间未知')
    assert.equal(f('不是时间'), '时间未知')
  }
})

test('后端已给 HH:MM:SS 的本地时钟串：保留原样并注明本地时间，不谎报未知', () => {
  assert.equal(formatLocalTimeShort('21:29:15'), '21:29:15（本地时间）')
  assert.equal(formatLocalTimeLabel('21:29:15'), '21:29:15（本地时间）')
})

test('角色枚举不再英文直出（真机：admin / viewer）', () => {
  assert.equal(roleLabel('admin'), '管理员')
  assert.equal(roleLabel('viewer'), '只读用户')
  assert.match(roleLabel(undefined), /未标注角色/)
  assert.match(roleLabel('ops'), /未知角色（原文：ops）/)
})

test('定时任务「上次」结果不再直出 submitted（真机 recheck/settings.txt:422）', () => {
  // 真机原文：`上次：2026/10/21 16:00:16（本地时间） · submitted · sched-f6e5440b8b`
  assert.equal(scheduledResultLabel('submitted'), '已提交入队')
  assert.equal(scheduledResultLabel('error'), '提交失败')
  assert.match(scheduledResultLabel(''), /无记录/)
  assert.match(scheduledResultLabel(undefined), /无记录/)
  assert.match(scheduledResultLabel('weird'), /未知结果（原文：weird）/)
})

test('端点状态不再直出 ok / unreachable（真机 settings.txt:28）', () => {
  assert.equal(balanceStatusText(true, 'ok'), '正常')
  assert.equal(balanceStatusText(false, 'unreachable'), '不可达')
  assert.equal(balanceStatusText(false, 'insufficient_balance'), '额度不足')
  assert.equal(balanceStatusText(false, 'unauthorized'), '密钥无效或无权限')
  assert.equal(balanceStatusText(false, ''), '未回传原因')
  assert.equal(balanceStatusText(true, ''), '正常')
  // 含中文的后端文案原样保留（本就是给人读的）
  assert.equal(balanceStatusText(false, '上游返回 429：请求过快'), '上游返回 429：请求过快')
  // 认不出的英文不硬翻成中文词，保留原文并说明未识别（不编造含义）
  const weird = balanceStatusText(false, 'weird_state_xyz')
  assert.match(weird, /状态未识别/)
  assert.match(weird, /weird_state_xyz/)
})

test('后端给了带域名的整句原因就原样展示，不再被枚举覆盖（2026-10-22 新人实例）', () => {
  // 实测：tierflow 的 key 被填进模板预填的 SiliconFlow 端点 → 401。旧行为只显示
  // 「密钥无效或无权限」，人先去换 key；key 本身有效，错在端点与密钥不同源。
  const mismatch = balanceStatusText(
    false, 'unauthorized',
    'api.siliconflow.cn 返回 401：该接入点不认这把密钥——密钥与接入点必须是同一家服务商')
  assert.match(mismatch, /api\.siliconflow\.cn/, '必须点明是哪个端点在拒绝')
  assert.match(mismatch, /同一家服务商/, '必须给出「端点与密钥同源」这条判断')
  assert.doesNotMatch(mismatch, /^密钥无效或无权限$/, '不得退回到没有信息量的枚举文案')

  // 反向误判：模型名不被接入点支持（400/404）时，枚举是 unreachable，旧界面说「不可达」，
  // 人会去查网络与代理；这类失败的成因在模型名，必须说出来。
  const modelWrong = balanceStatusText(
    false, 'unreachable',
    'tierflow.cn 返回 400：请求被拒，模型名（当前模型名：tierflow_pro）可能不被该接入点支持')
  assert.match(modelWrong, /模型名/, '模型名错不得被说成网络问题')
  assert.match(modelWrong, /tierflow\.cn/)

  // 真的连不上时仍说连不上（别把这条修成"所有失败都像配错"）
  assert.match(balanceStatusText(false, 'unreachable', '连不上 api.x.com：timed out（网络/代理/DNS）'),
    /连不上/)

  // ok 恒为「正常」；没有 message 时行为与之前完全一致（老页面/老缓存不回归）
  assert.equal(balanceStatusText(true, 'ok', '随便一句'), '正常')
  assert.equal(balanceStatusText(false, 'unreachable'), '不可达')
  assert.equal(balanceStatusText(false, 'unauthorized'), '密钥无效或无权限')
})
