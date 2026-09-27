/** C3/H3b：提交幂等键的分配规则（纯函数，便于离线行为测试）。
 *
 * 背景（原指令 §3.4）：网页每次提交都生成新任务，双击或失败重试会变成**第二次真实执行**。
 * 服务端按 `idempotency_key` 去重，但键必须由客户端正确分配，规则是：
 *
 * - **同一次提交意图**（提交内容相同、且上一次尝试还没被接收）→ 复用同一个键；
 * - **成功之后**再提交，或**改了内容**再提交 → 换新键（那是用户主动再跑一次，不是重复）。
 *
 * 键本身由调用方注入（`makeKey`），本模块只决定"复用还是新建"，
 * 因此可以脱离浏览器与 React 测试。
 */

export type PendingSubmission = { sig: string; key: string } | null

export type SubmissionInput = {
  goal: string
  project?: string
  conversationId?: string
  confirmMode?: boolean
  templateName?: string
  context?: string
  fields?: unknown
}

/** 提交意图签名：内容一致即视为"同一次提交的重复请求"。 */
export function submissionSignature(input: SubmissionInput): string {
  return JSON.stringify({
    g: String(input.goal ?? '').trim(),
    project: String(input.project ?? '').trim(),
    conv: String(input.conversationId ?? ''),
    confirm: Boolean(input.confirmMode),
    tpl: String(input.templateName ?? ''),
    ctx: String(input.context ?? '').trim(),
    fields: input.fields ?? null,
  })
}

/** 决定本次提交用哪个键：与上次未完成的提交同内容则复用，否则新建。 */
export function resolveSubmissionKey(
  pending: PendingSubmission,
  input: SubmissionInput,
  makeKey: () => string,
): { key: string; pending: PendingSubmission } {
  const sig = submissionSignature(input)
  if (pending && pending.sig === sig && pending.key) {
    return { key: pending.key, pending }
  }
  const key = makeKey()
  return { key, pending: { sig, key } }
}
