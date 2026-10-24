import test from 'node:test'
import assert from 'node:assert/strict'
import { stripInlineMarkdown, quoteForPanel } from '../src/lib/quoteText.ts'

// 实机原文（任务 ui-b23642e5de「未采用来源」第一条，含被切坏的标记）
const REAL =
  '2024 年营业收入 **1157.80 亿元**，较 2023 年 **1261.79 亿元**下降 **8.24%*'

test('剥掉粗体标记，文字与数字保留', () => {
  assert.equal(stripInlineMarkdown('**1157.80 亿元**'), '1157.80 亿元')
  assert.equal(stripInlineMarkdown('较 **1261.79 亿元**下降'), '较 1261.79 亿元下降')
})

test('实机那句不再有任何 * 残留（含被切坏的半个标记）', () => {
  const out = stripInlineMarkdown(REAL)
  assert.ok(!out.includes('*'), `仍残留星号：${out}`)
  assert.ok(out.includes('1157.80 亿元') && out.includes('1261.79 亿元'))
})

test('引用截断后不出现半个标记，且带省略号', () => {
  const out = quoteForPanel(REAL, 40)
  assert.ok(!out.includes('*'), out)
  assert.ok(out.endsWith('…'), out)
  assert.ok(out.length <= 41, `过长：${out.length}`)
})

test('短句不加省略号', () => {
  assert.equal(quoteForPanel('两期读数只能说明这两期的变化', 40), '两期读数只能说明这两期的变化')
})

test('截断不把数值切半：优先收在标点处', () => {
  const out = quoteForPanel('毛利变化 -18.76 亿元、毛利线以下净额变化 -1.00 亿元，据此判断结构', 24)
  assert.ok(!/\d\.\d\d…$/.test(out), `把数值切半了：${out}`)
})

test('链接与行内代码只留文字', () => {
  assert.equal(stripInlineMarkdown('见 [年报](https://x/y) 的 `营业收入` 项'), '见 年报 的 营业收入 项')
})

test('斜体与删除线不误伤乘号文本', () => {
  assert.equal(stripInlineMarkdown('*斜体* 与 ~~删除~~'), '斜体 与 删除')
  assert.equal(stripInlineMarkdown('营收 * 利润'), '营收  利润')
})

test('空值与普通文本原样', () => {
  assert.equal(quoteForPanel(''), '')
  assert.equal(quoteForPanel('无标记的一句话'), '无标记的一句话')
})
