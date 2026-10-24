// P2-4：带 ?task=<id> 打开时，研究表单默认折叠（纯函数，可在 Node 里直接断言）。
//
// 实机反例（2026-10-21 真机复验 README P2-4）：深链 `?task=<id>` 打开一个**已有任务**，
// 第一屏仍然是完整的「研究一家公司」提交表单（公司/期间/口径/阅读重点…），
// 报告被挤到下面；而这条链接的意图就是"看这个任务"。
// 规则：已经打开某个任务（深链或本次会话刚提交的）→ 表单默认折叠；无任务 → 保持现状。

export interface ResearchFormFoldInput {
  /** URL 深链 ?task=<id> */
  deepTaskId?: string | null
  /** 当前正在跟踪的任务 id */
  currentTaskId?: string | null
}

export function shouldCollapseResearchForm(input: ResearchFormFoldInput): boolean {
  const deep = String(input.deepTaskId ?? '').trim()
  const cur = String(input.currentTaskId ?? '').trim()
  return !!(deep || cur)
}
