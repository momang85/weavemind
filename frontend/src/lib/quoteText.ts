/**
 * 面板里引用正文句子的清洗与截断。
 *
 * 实机反例（2026-10-22，任务 ui-b23642e5de 的「修改与重验 → 未采用来源」）：
 *   · 未采用来源：2024 年营业收入 **1157.80 亿元**，较 2023 年 **1261.79 亿元**
 *     下降 **8.24%*——8.24%：同句出现多个年度且数值未紧跟年份，期间归属不明确；…
 *
 * 两个问题叠在一起：
 *   ① 正文是 Markdown，被引到纯文本面板后 `**` 原样显示；
 *   ② 引用按 `slice(0, 60)` 硬切，正好落在 `**` 中间 → 留下半个标记 `*`，
 *      读者会以为是笔误或数据缺失。
 *
 * 因此：先剥掉行内标记，再按**可见字符**截断并补省略号；绝不在标记中间断句。
 */

/** 去掉行内 Markdown 标记（保留文字本身）：**粗**、*斜*、`代码`、~~删除~~、[文字](链接)。 */
export function stripInlineMarkdown(input: string): string {
  let s = String(input ?? '')
  s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')        // 图片 → alt
  s = s.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')          // 链接 → 文字
  s = s.replace(/\*\*\*(.+?)\*\*\*/g, '$1')              // ***粗斜***
  s = s.replace(/\*\*(.+?)\*\*/g, '$1')                  // **粗**
  s = s.replace(/(^|[\s（(])\*(?!\s)(.+?)(?<!\s)\*(?=$|[\s）)，。,；;：:])/g, '$1$2') // *斜*
  s = s.replace(/__(.+?)__/g, '$1')
  s = s.replace(/~~(.+?)~~/g, '$1')
  s = s.replace(/`([^`]*)`/g, '$1')
  // 收尾清扫：上面任何一步失败都会留下孤立标记，宁可去掉也不能留在面板上
  s = s.replace(/\*{1,3}/g, '').replace(/`/g, '')
  return s
}

/** 面板引用：剥标记 → 压缩空白 → 按可见长度截断并补 `…`。 */
export function quoteForPanel(input: string, limit = 60): string {
  const s = stripInlineMarkdown(input).replace(/\s+/g, ' ').trim()
  if (s.length <= limit) return s
  const cut = s.slice(0, limit)
  // 截断点落在数字/百分号中间时回退到最近的分隔符，避免把数值切半
  const m = cut.match(/^(.*[\s，。；、,;])[^\s，。；、,;]*$/)
  const body = (m && m[1].length >= limit * 0.6 ? m[1] : cut).replace(/[\s，。；、,;：:]+$/, '')
  return `${body}…`
}
