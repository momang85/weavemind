// S1「门禁跳过（资料缺口）」的**显示层**判据与文案——判据与后端同源，只改“怎么呈现”。
//
// 后端事实（只读确认，实机 `agents.db` 任务 `ui-d59152ebb9` 步骤 2/2b/2c）：
// - 指令里没有 http(s) URL 时编排器**不派发抓取**，直接回
//   `{status:"FAILED", gate_skipped:true, elapsed_sec:0,
//     result:"无候选 URL（检索未产出可用来源），已跳过抓取；需补资料或换检索词后重试"}`
//   （orchestrator_v2.py:8990-8994 的 S1 门禁）；
// - 判据谓词 `_is_search_gap_skip`（orchestrator_v2.py:8536-8548）：结果字典的
//   `gate_skipped` 为真，**或**结果文本含「无候选 URL」（历史数据没有标记时按文本兜底）；
// - 这类步登记进 `optional_ids`：下游不连锁失败、终态不判红——代码注释原话
//   “检索没产出可用来源是资料缺口，不是能力失败”（orchestrator_v2.py:8973-8982）。
//
// 前端此前只认 `status`（`FAILED` → 红色失败），于是把资料缺口渲染成失败，
// 与后端语义不一致。本模块把这条判据搬到显示层。
//
// **语义红线**：这里只产出显示状态与文案。任务终态、机器验收、研究状态、人工复核
// 三种状态都不读这里，也不受这里影响；只有**原始状态为 FAILED 且命中判据**的步骤
// 才呈现为“跳过”，真正失败的步骤（不含判据）仍然按失败显示，行为一字不变。

/** 门禁跳过的用户口径名称：中性说法，不带“失败”字样。 */
export const GATE_SKIP_LABEL = '跳过（资料缺口）'

/** 结果文本兜底判据的关键词——与后端 `_is_search_gap_skip` 同一句。 */
const GAP_TEXT = '无候选 URL'

/**
 * 取步骤的**结果字典**，三种调用形状都认：
 * - 原始 API/checkpoint 步骤：`{step_id, capability, result: {status, gate_skipped, result, ...}}`
 *   （落库形状见 web_ui.py:527 的注释与 orchestrator_v2.py:4815-4820 的 `_with_result`）；
 * - 已映射的 `TaskNode`：`node.result` 就是那份结果（useTaskLive.ts 的 buildChildren）；
 * - 直接把结果字典本身传进来。
 *
 * 认不出来返回 `null`——判不出就判不出，不按“看起来像”猜。
 */
function resultOf(step: unknown): Record<string, unknown> | null {
  if (!step || typeof step !== 'object') return null
  const s = step as Record<string, unknown>
  const inner = s.result
  if (inner && typeof inner === 'object' && !Array.isArray(inner)) {
    return inner as Record<string, unknown>
  }
  // 结果字典本身：带 status，且有 gate_skipped / elapsed_sec 这类结果字段
  if (typeof s.status === 'string' && ('gate_skipped' in s || 'elapsed_sec' in s)) {
    return s
  }
  return null
}

/** 这一步是不是“检索没产出可用来源”的门禁跳过（与后端谓词同源）。 */
export function isSearchGapSkip(step: unknown): boolean {
  const res = resultOf(step)
  if (!res) return false
  if (res.gate_skipped) return true
  return String(res.result ?? '').includes(GAP_TEXT)
}

/** 步骤的**原始**状态（小写）：先看嵌套结果，再退步骤顶层；判不出按 `pending`。 */
export function rawStepStatus(step: unknown): string {
  const res = resultOf(step)
  if (res && typeof res.status === 'string' && res.status) return res.status.toLowerCase()
  const s = (step ?? {}) as Record<string, unknown>
  if (typeof s.status === 'string' && s.status) return s.status.toLowerCase()
  return 'pending'
}

/**
 * 计划树/历史回放用的**显示状态**（与 `TaskNode['status']` 同一套词表）。
 *
 * 只在「原始状态 = failed 且命中门禁判据」时改写为 `skipped`；其余状态原样返回，
 * 因此真失败仍然 `failed`（红色行为不变），`success`/`running` 也不会被这条判据染色。
 * 对已映射过的 `TaskNode` 再算一次仍得同一结果（幂等）。
 */
export function stepDisplayStatus(step: unknown): string {
  const raw = rawStepStatus(step)
  if (raw === 'failed' && isSearchGapSkip(step)) return 'skipped'
  return raw
}

/** 是否给这个节点挂「跳过（资料缺口）」的专门文案（显示状态已是跳过且判据命中）。 */
export function isGateSkipDisplay(displayStatus: string, step: unknown): boolean {
  return displayStatus === 'skipped' && isSearchGapSkip(step)
}

/** 门禁跳过的原因文本（后端原文）；取不到时给一句不含猜测的说明。 */
export function gateSkipReason(step: unknown): string {
  const res = resultOf(step)
  const text = String(res?.result ?? '').trim()
  return text || '检索未产出可用来源（无候选 URL），已跳过抓取'
}

/**
 * 步骤完成日志的显示口径（实时动态里那一行）。
 *
 * 门禁跳过不进红色 `error` 流——那是“失败”的颜色，与后端语义不符；真失败维持
 * `error` + “Failed: ”，成功维持 `dispatch` + “Completed: ”。
 */
export function stepLogStyle(
  step: unknown,
  rawStatus: string,
): { type: 'dispatch' | 'error' | 'info'; prefix: string } {
  if (rawStatus === 'success') return { type: 'dispatch', prefix: 'Completed: ' }
  if (rawStatus === 'failed' && isSearchGapSkip(step)) {
    return { type: 'info', prefix: `${GATE_SKIP_LABEL}: ` }
  }
  return { type: 'error', prefix: 'Failed: ' }
}
