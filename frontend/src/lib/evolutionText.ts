// S4（2026-10-22 真机核查）：记忆页「进化锦标赛回放」的轮次摘要是后端拼好的
// `key=value` + 英文布尔直出，实机原文（recheck/memory.txt:404,409,414,419）：
//   `演化完成: 胜者=baseline-v1-gen1-A, 稳定=True, 已部署=True`
//   `演化完成: 胜者=baseline-v1-gen1-C, 稳定=True, 已部署=False`
// 同屏还有 `已部署=False` 与 `True` 混排；评分列表用 `JSON.stringify(rk.scores)`
// 直出花括号与引号。这里统一改成中文句子：`True/False` → 「是/否」，
// `key=value` → 「key 值」/「key：值」，评分列表不再用 JSON 括号。

const BOOL_TEXT: Record<string, string> = {
  true: '是',
  false: '否',
  none: '未记录',
  null: '未记录',
}

/** 单个值 → 中文：布尔写「是/否」，None/null 写「未记录」，其余原样（不编造）。 */
export function boolishValueText(value: string): string {
  const v = value.trim()
  const hit = BOOL_TEXT[v.toLowerCase()]
  return hit ?? v
}

/**
 * 轮次摘要本地化：`演化完成: 胜者=baseline-v1-gen1-A, 稳定=True, 已部署=False`
 * → `演化完成：胜者 baseline-v1-gen1-A；稳定：是；已部署：否`
 *
 * 规则：①首个「: / ：」前是无 `=` 的标题段，保留；②其余按逗号/分号拆成
 * `key=value` 片段，值是英文布尔才翻译，其余原样；③没有 `=` 的片段原样保留。
 * 摘要缺失时返回空串，由调用方决定占位（不编造"进化完成"）。
 */
export function localizeEvolutionSummary(summary?: string | null): string {
  const raw = String(summary ?? '').trim()
  if (!raw) return ''
  const headMatch = /^([^:：=]+)[:：]\s*(.+)$/.exec(raw)
  const head = headMatch ? headMatch[1].trim() : ''
  const body = headMatch ? headMatch[2] : raw
  const parts = body
    .split(/[,，;；]+/)
    .map(part => part.trim())
    .filter(Boolean)
    .map(part => {
      const kv = /^([^=]+)=(.*)$/.exec(part)
      if (!kv) return boolishValueText(part)
      const key = kv[1].trim()
      const value = boolishValueText(kv[2])
      // 值是布尔时读作「key：是/否」；其余（如胜者名）读作「key 值」，更接近中文句子。
      return BOOL_TEXT[kv[2].trim().toLowerCase()] !== undefined
        ? `${key}：${value}`
        : `${key} ${value}`
    })
  if (!parts.length) return head || raw
  return head ? `${head}：${parts.join('；')}` : parts.join('；')
}

/**
 * 评分映射 → 中文列举（不带 JSON 括号与引号）：
 *   `{"baseline-v1-gen1-A":8.5,"baseline-v1-gen1-B":6.0}` → `baseline-v1-gen1-A 8.5、baseline-v1-gen1-B 6`
 * 非数值原样展示；空映射返回空串（调用方决定占位，不编造 0 分）。
 */
export function scoreMapText(scores?: Record<string, unknown> | null, maxItems = 6): string {
  if (!scores) return ''
  const items = Object.entries(scores)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${k} ${typeof v === 'number' ? String(Number(v.toFixed(2))) : String(v)}`)
  if (!items.length) return ''
  const shown = items.slice(0, maxItems).join('、')
  return items.length > maxItems ? `${shown} 等 ${items.length} 项` : shown
}
