// P2-4b：执行计划里步骤描述的折叠（纯函数，可在 Node 里直接断言）。
//
// 实机反例（2026-10-21 真机复验 README P2-4b）：计划区 7 个能力步骤的说明**全都在重复
// 同一条任务目标**——因为后端把 `任务目标/用户目标/原始指令/研究契约` 拼成 instruction，
// 每步的前半段完全一样，前端又取整段 instruction 当行名，于是 7 行看起来一模一样
// （真正区分各步的「原始指令」段落被挤到看不见的地方）。
// 处理：把各步共用的前缀折叠成**一处**显示，行名改成「能力动作：该步关键动作」；
// 完整原文仍留在 title 里可悬停查看，不做信息删除。

const CAPABILITY_ACTION: Record<string, string> = {
  web_search: '检索公开来源',
  web_fetch: '抓取页面正文',
  content_summary: '摘要与要点',
  content_summarizer: '摘要与要点',
  data_loader: '载入结构化数据',
  data_analyzer: '确定性分析/复算',
  financial_analysis: '财务复算',
  code_execution: '沙箱执行代码',
  chart_generator: '生成图表',
  chart: '生成图表',
  report_generator: '装配正文',
  package: '打包交付物',
  file_io: '读写工作区文件',
  model_trainer: '训练/拟合',
  critic: '计划评审',
  embedding: '向量化检索',
  material_ingest: '摄入披露材料',
}

/** 能力 → 这一步实际要做的事（读不到能力名时如实写能力名，不编）。 */
export function capabilityAction(capability?: unknown): string {
  const c = String(capability ?? '').trim()
  if (!c) return '执行步骤'
  return CAPABILITY_ACTION[c.toLowerCase()] || `执行 ${c}`
}

/** 逐字符公共前缀（大小写敏感，不做模糊匹配）。 */
export function commonPrefix(items: string[]): string {
  const xs = (items || []).map(x => String(x ?? ''))
  if (!xs.length) return ''
  let p = xs[0]
  for (const x of xs.slice(1)) {
    let i = 0
    while (i < p.length && i < x.length && p[i] === x[i]) i++
    p = p.slice(0, i)
    if (!p) break
  }
  return p
}

const BOUNDARY = /[\n；;。]/
const MIN_SHARED = 24

/** 把公共前缀截到最后一个语义边界，避免把半句话当"共用说明"。 */
function cutAtBoundary(prefix: string): string {
  for (let i = prefix.length - 1; i >= MIN_SHARED; i--) {
    if (BOUNDARY.test(prefix[i])) return prefix.slice(0, i + 1)
  }
  return prefix.length >= MIN_SHARED ? prefix : ''
}

/** 该步自己的关键动作：去掉共用前缀后取第一行/第一句，长文按 need 截断（完整原文在 description）。 */
export function stepActionText(rest: string, need = 60): string {
  let t = String(rest ?? '')
    .replace(/^\s*原始指令\s*[:：]?/, '')
    .replace(/\s+/g, ' ')
    .trim()
  if (!t) return ''
  const m = /^(.{0,400}?)[。；;]/.exec(t)
  if (m) t = m[1]
  return t.length > need ? `${t.slice(0, need)}…` : t
}

export interface PlanChildInput {
  id?: string
  capability?: string
  name?: string
  description?: string
}

export interface PlanChildView {
  /** 行名：能力动作 + 该步关键动作（共用说明已折叠） */
  name: string
  /** 悬停可看的完整原文 */
  description: string
  /** 能力动作（短标签） */
  action: string
  /** 该步是否发生了折叠（共用说明被抽走） */
  folded: boolean
}

export interface PlanFoldResult {
  children: PlanChildView[]
  /** 被折叠的共用说明（多处重复 → 只显示一次） */
  shared: { text: string; count: number }[]
}

/** 折叠计划里重复的步骤说明：共用前缀只显示一次，行名换成能力动作 + 该步关键动作。 */
export function foldPlanDescriptions(children: PlanChildInput[]): PlanFoldResult {
  const items = Array.isArray(children) ? children : []
  const names = items.map(c => String(c?.name ?? '').trim())
  const filled = names.filter(Boolean)
  const prefix = filled.length >= 2 ? cutAtBoundary(commonPrefix(filled)) : ''
  const folded = !!prefix
  const shared = folded
    ? [{ text: prefix.trim(), count: filled.filter(n => n.startsWith(prefix)).length }]
    : []
  return {
    children: items.map((c, i) => {
      const original = String(c?.name ?? '').trim()
      const action = capabilityAction(c?.capability)
      const rest = folded && original.startsWith(prefix) ? original.slice(prefix.length) : original
      const key = folded ? stepActionText(rest) : ''
      return {
        name: folded ? (key ? `${action}：${key}` : action) : (original || action),
        description: original || String(c?.description ?? '') || action,
        action,
        folded: folded && names[i].startsWith(prefix),
      }
    }),
    shared,
  }
}
