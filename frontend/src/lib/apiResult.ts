// 统一判定 API 结果：**任何非 2xx 都不得显示成功**。
//
// 背景（架构复核 01:05）：演示模式下 fetch 层会直接返回 403 + `demo_blocked`，
// 而 Memory 的删除/演化/审批处理函数不检查 `res.ok`，于是"被拦截"照样提示
// "删除完成""进化已触发"——用户以为真的做了。这里把判定收敛成一个纯函数，
// 让真实行为可测（Node 里直接用假 Response 断言，不依赖 DOM）。
//
// 判定口径：
//   403 + demo_blocked → 演示拦截（提示里带服务端给的原因）
//   其他非 2xx        → 失败（带 HTTP 状态码）
//   2xx               → 成功（调用方给的文案）

export interface ApiOutcome {
  ok: boolean
  /** 演示模式拦截（与真实失败区分：界面可以据此用"提示"而非"报错"的语气） */
  blocked: boolean
  message: string
  status: number
}

export function apiOutcome(
  status: number,
  payload: unknown,
  okMessage: string,
  failMessage: string,
): ApiOutcome {
  const body = (payload && typeof payload === 'object' ? payload : {}) as Record<string, unknown>
  const reason = typeof body.error === 'string' ? body.error : ''
  if (status >= 200 && status < 300) {
    return { ok: true, blocked: false, message: okMessage, status }
  }
  if (status === 403 && body.demo_blocked === true) {
    return { ok: false, blocked: true, message: reason || '演示模式：该操作已被拦截。', status }
  }
  return {
    ok: false,
    blocked: false,
    message: `${failMessage}（HTTP ${status}${reason ? '：' + reason : ''}）`,
    status,
  }
}

/** 读 JSON（失败不抛，交给 apiOutcome 判定），再判定结果。 */
export async function readApiOutcome(
  res: Response,
  okMessage: string,
  failMessage: string,
): Promise<ApiOutcome> {
  let payload: unknown = null
  try {
    payload = await res.json()
  } catch {
    payload = null
  }
  return apiOutcome(res.status, payload, okMessage, failMessage)
}
