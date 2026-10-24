// P1-7：贯通测试小计的显示口径（纯函数，只改展示文本，不改验收语义）。
//
// 实机反例（2026-10-21 真机复验 README P1-7）：报告里
//   `结果: 1/1 项通过`
// 同一格内却是三条 `SKIP`（主体提及频率图 / 来源计数分布图 / 市场规模图 全部跳过）。
// 后端把"声明总数"原样打出（orchestrator_v2.py:6467-6469 的 `**结果**：{passed}/{len} 项通过`），
// 前端照抄 → 读起来像"通过"。
// 这里把小计行重写成 **通过 / 跳过 / 失败** 各自计数；没有跳过/失败时原文不动。

export interface E2eCounts { skipped: number; failed: number }

const SKIP_TOKEN = /\bSKIP\b/
const FAIL_TOKEN = /\bFAIL(?:ED)?\b/
const SKIP_TOKEN_G = /\bSKIP\b/g
const FAIL_TOKEN_G = /\bFAIL(?:ED)?\b/g
/** `结果：1/1 项通过` / `**结果**：1/1 项通过`（半角冒号也兼容） */
const TALLY_RE = /(\*{0,2}结果\*{0,2}\s*[:：]\s*)(\d+)\s*\/\s*(\d+)\s*项通过/

export function countOutcomes(text: string): E2eCounts {
  const skipped = (String(text).match(SKIP_TOKEN_G) || []).length
  const failed = (String(text).match(FAIL_TOKEN_G) || []).length
  return { skipped, failed }
}

/** 小计行命中的那一段"证据窗口"：先看同一行；没有标记才往后扩到空行/标题（上限 40 行）。 */
function windowText(lines: string[], i: number, startCol: number): string {
  const same = lines[i].slice(startCol)
  if (SKIP_TOKEN.test(same) || FAIL_TOKEN.test(same)) return same
  const parts = [same]
  for (let j = i + 1; j < lines.length && j <= i + 40; j++) {
    const t = lines[j].trim()
    if (!t || /^#{1,6}\s/.test(t)) break
    parts.push(t)
  }
  return parts.join('\n')
}

/**
 * 把 `结果：N/M 项通过` 重写成 `结果：P 项通过 · S 项跳过（· F 项失败）`。
 * 规则：
 *  - SKIP/FAIL 出现在该行（或紧随其后的同节文本）→ 从声明总数里扣掉，**不算通过**；
 *  - 没有跳过/失败 → 返回原文（不做任何无意义的改写）。
 */
export function rewriteE2eVerdict(md: string): string {
  const src = String(md ?? '')
  const lines = src.split('\n')
  let changed = false
  const out = lines.map((line, i) => {
    const m = TALLY_RE.exec(line)
    if (!m || m.index === undefined) return line
    const declared = Number(m[3])
    if (!declared) return line
    const { skipped, failed } = countOutcomes(windowText(lines, i, m.index + m[0].length))
    if (!skipped && !failed) return line
    const passed = Math.max(0, declared - skipped - failed)
    const parts = [`${passed} 项通过`]
    if (skipped) parts.push(`${skipped} 项跳过`)
    if (failed) parts.push(`${failed} 项失败`)
    changed = true
    return line.slice(0, m.index) + m[1] + parts.join(' · ') + line.slice(m.index + m[0].length)
  })
  return changed ? out.join('\n') : src
}
