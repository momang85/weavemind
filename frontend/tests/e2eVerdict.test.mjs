// P1-7 行为测试：贯通测试小计的显示口径。
// 实机反例（docs/evidence/20261021-live-verify/README.md:200-204，截图 08-new-4.png）：
//   「贯通测试（整体可运行性）结果：1/1 项通过」
// 但同一格里三张图全是 SKIP：
//   `SKIP 主体提及频率图：有效实体不足或无区分度（0 条）`
//   `SKIP 来源计数分布图：来源计数全部相同或为空（3 个来源）`
//   `SKIP 市场规模图：有效结构化数据不足（当前 0 条）`
// SKIP 绝不能算作"通过"；没有跳过/失败时不得改写原文。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { countOutcomes, rewriteE2eVerdict } from '../src/lib/e2eVerdict.ts'

const SKIP_BLOCK = [
  '## 贯通测试（整体可运行性）',
  '**结果**：1/1 项通过',
  '- `make_charts.py`（py）：RENDERED …',
  '- SKIP 主体提及频率图：有效实体不足或无区分度（0 条）',
  '- SKIP 来源计数分布图：来源计数全部相同或为空（3 个来源）',
  '- SKIP 市场规模图：有效结构化数据不足（当前 0 条）',
].join('\n')

test('三条 SKIP 时不得再写「1/1 项通过」：通过/跳过分开计数，SKIP 不计入通过', () => {
  const out = rewriteE2eVerdict(SKIP_BLOCK)
  const head = out.split('\n')[1]
  assert.ok(!/1\/1 项通过/.test(head), head)
  assert.match(head, /0 项通过/, head)
  assert.match(head, /3 项跳过/, head)
  // 其余原文不动（只重写小计那一行）
  assert.ok(out.includes('SKIP 主体提及频率图'), '证据行被改动了')
  assert.ok(out.includes('SKIP 市场规模图'), '证据行被改动了')
})

test('后端已补 ⚠️ 说明时仍按同一口径重写（两种信号都看得见）', () => {
  const src = [
    '## 贯通测试（整体可运行性）',
    '**结果**：1/1 项通过（其中 1 项运行成功但产物被跳过 ⚠️，见下）',
    '- ⚠️ `make_charts.py`（py）：SKIP 主体提及频率图：有效实体不足或无区分度（0 条）',
  ].join('\n')
  const out = rewriteE2eVerdict(src)
  assert.match(out, /0 项通过 · 1 项跳过/, out)
  assert.ok(out.includes('产物被跳过 ⚠️'), '后端原有的 ⚠️ 说明被吞掉了')
})

test('没有 SKIP/FAIL 的通过行：原文返回，不做无意义改写', () => {
  const ok = [
    '## 贯通测试（整体可运行性）',
    '**结果**：1/1 项通过',
    '- ✅ `render_charts.py`（py）：RENDERED chart_1.png',
    'total=1 skipped=0',
  ].join('\n')
  assert.equal(rewriteE2eVerdict(ok), ok)
  // 没有小计行的正文同样原样返回
  assert.equal(rewriteE2eVerdict('# 报告\n没有任何小计'), '# 报告\n没有任何小计')
  assert.equal(rewriteE2eVerdict(''), '')
})

test('FAIL 与 SKIP 同时出现：三种计数都写出来', () => {
  const src = [
    '**结果**：3/3 项通过',
    '- FAIL `a.py`：exit 1',
    '- SKIP `b.py`：跳过',
  ].join('\n')
  const out = rewriteE2eVerdict(src)
  assert.match(out, /1 项通过 · 1 项跳过 · 1 项失败/, out)
  assert.equal(countOutcomes('- SKIP x\n- FAIL y\n- FAILED z').skipped, 1)
  assert.equal(countOutcomes('- SKIP x\n- FAIL y\n- FAILED z').failed, 2)
})

test('报告正文渲染前统一走 rewriteE2eVerdict（源码扫描）', () => {
  const code = readFileSync(
    fileURLToPath(new URL('../src/components/ReportViewer.tsx', import.meta.url)), 'utf8')
  assert.ok(/rewriteE2eVerdict\(/.test(code), 'ReportViewer 未对正文套用该口径')
})
