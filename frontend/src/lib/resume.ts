/**
 * U90-B：「继续执行」（人工恢复失败任务）的前端统一口径。
 *
 * 后端在列表（/tasks）、会话消息（/api/conversations/<id>）、任务详情（/task/<id>）
 * 里都回 `resume` 视图（web_ui._resume_view）：只有 FAILED/CRASHED 才
 * `available=true`（与 POST /api/task/<id>/resume 的 409 规则同一份判定）。
 * 前端只按这里显隐按钮与文案，不再各自猜状态——SUCCESS/CANCELLED 等不可逆
 * 终态必须显示"为什么不能继续"，而不是悄悄给出按钮。
 */

export type ResumeView = {
  available?: boolean
  status?: string
  total?: number
  completed?: number
  failed_step?: string
  failure?: string
  reason?: string
  label?: string
}

/** 按钮显隐：只有后端判"可续跑"的任务才显示（未知/缺失一律不显示）。 */
export function resumeVisible(r?: ResumeView | null): boolean {
  return !!(r && typeof r === 'object' && r.available === true)
}

/** 按钮文案：后端给 label（带"跳过 n/m 步"），缺失时按完成步数兜底。 */
export function resumeLabel(r?: ResumeView | null): string {
  if (!resumeVisible(r)) return ''
  if (r!.label) return String(r!.label)
  const c = r!.completed
  // 明确"没有可复用步骤"才说重新接管；字段缺失（旧后端）用中性文案
  if (typeof c === 'number' && c <= 0) return '重新接管执行'
  return '继续执行'
}

/** 不能继续时的解释（SUCCESS/CANCELLED/RUNNING/未知）：一句话说清原因。 */
export function resumeBlockedReason(r?: ResumeView | null): string {
  if (!r || typeof r !== 'object' || r.available) return ''
  return String(r.reason || '')
}

/** 同一任务的恢复请求在途标记（双击/重复点击的**前端闸**；服务端另有 CAS）。 */
const inFlight = new Set<string>()
export function resumeInFlight(taskId: string): boolean {
  return inFlight.has(taskId)
}

/**
 * U90-R3：恢复回执从「一句话」升级为**三态**（架构师裁决 2026-10-20）。
 *
 * 旧实现把成功回执、失败说明、在途提示都塞进同一个字符串并禁用按钮——
 * 503（如 Redis 未连接）之后按钮永久失效，失败不可再次操作。
 * 现在页面按 `phase` 渲染：
 *   - pending：请求在途，按钮禁用（双击去重的用户可见面）；
 *   - ok：已请求恢复/确认已在运行，按钮禁用（列表刷新会移除）；
 *   - failed：请求失败但**可再试**，按钮保持可用，失败原因作为按钮旁说明；
 *   - timeout：请求超时——已查询真实任务状态后给出裁决（见 requestResume）。
 */
export type ResumePhase = 'pending' | 'ok' | 'failed' | 'timeout'

export type ResumeResult = {
  ok: boolean
  phase: ResumePhase
  message: string
  /** 失败后是否可以再次点击重试（false：不可恢复终态或已确认被接管）。 */
  retryable: boolean
  /** 本次点击是否确认任务已被接管（含超时后查到 RUNNING 的情形）。 */
  resumed?: boolean
  /** 超时/失败后查询到的真实任务状态（未能查到为空串）。 */
  taskStatus?: string
}

/** 页面持有的逐任务回执（点过之后按钮态由此推导，不再用字符串真值判断）。 */
export type ResumeNote = {
  phase: ResumePhase
  message: string
  retryable: boolean
  resumed?: boolean
}

export function noteFromResult(r: ResumeResult): ResumeNote {
  return { phase: r.phase, message: r.message, retryable: r.retryable, resumed: r.resumed }
}

/** 点击第一步：把该任务置为 pending（保持其余字段为空）。 */
export function beginResumeNote(notes: Record<string, ResumeNote>, tid: string)
  : Record<string, ResumeNote> {
  return { ...notes, [tid]: { phase: 'pending', message: '正在请求恢复…', retryable: false } }
}

/** 拿到回执后写入/更新该任务的回执（失败且可重试 → 按钮保持可用）。 */
export function applyResumeResult(notes: Record<string, ResumeNote>, tid: string,
  r: ResumeResult): Record<string, ResumeNote> {
  return { ...notes, [tid]: noteFromResult(r) }
}

/** 按钮渲染口径：pending/成功后禁用；失败且可重试时保持可点（文案变"重试"）。 */
export function resumeButtonState(note?: ResumeNote, view?: ResumeView | null): {
  disabled: boolean
  label: string
  hint: string
} {
  if (!note) {
    return { disabled: false, label: resumeLabel(view), hint: '' }
  }
  if (note.phase === 'pending') {
    return { disabled: true, label: '正在请求恢复…', hint: note.message }
  }
  if (note.phase === 'ok') {
    return { disabled: true, label: '已请求恢复', hint: note.message }
  }
  // failed / timeout：服务端判定可重试（retryable）→ 按钮保持可用，明说可再试
  if (note.retryable) {
    return {
      disabled: false,
      label: note.phase === 'timeout' ? '恢复超时，点击重试' : '重试继续执行',
      hint: note.message,
    }
  }
  return { disabled: true, label: '暂不可继续', hint: note.message }
}

/** 任务详情/状态接口的真实状态读取（超时后的防重复恢复查询用）。 */
async function fetchTaskStatus(taskId: string, fetchImpl: typeof fetch): Promise<string> {
  try {
    const res = await fetchImpl(`/task/${encodeURIComponent(taskId)}`)
    const data = await res.json()
    return String(data?.status || '').toUpperCase()
  } catch {
    return ''
  }
}

const RESUMED_STATUSES = new Set(['RUNNING', 'QUEUED', 'PENDING', 'QRUNNING'])

/**
 * 请求恢复一个失败任务：点击反馈 + 三态回执 + 双击去重 + 有界超时。
 *
 * - 在途中的同任务请求直接拒绝（不发第二次 fetch）；
 * - POST 带**有界超时**（默认 10s；服务端 publish 侧 socket_timeout=3s 更短，
 *   正常应先于前端超时返回）：超时后**先查询真实任务状态**再裁决——
 *   查到 RUNNING 等运行态 = 任务确实已被接管（按成功回执处理，不重复恢复）；
 *   查到仍是 FAILED/CRASHED = 未接管，回执「可重试」；查不到则明说未确认，
 *   仍允许重试（服务端 CAS + 状态门槛保证重试不会造成双重接管）；
 * - 服务端 409/503/500 的 `retryable` 字段优先（409 终态不可重试；
 *   503 发布失败已回滚 → 可重试），原文透传不吞成通用失败。
 */
export async function requestResume(
  taskId: string,
  fetchImpl: typeof fetch = fetch,
  opts: { timeoutMs?: number; statusImpl?: (tid: string) => Promise<string> } = {},
): Promise<ResumeResult> {
  if (!taskId) return { ok: false, phase: 'failed', message: '缺少任务 id，未发送恢复请求', retryable: false }
  if (inFlight.has(taskId)) {
    return { ok: false, phase: 'pending', message: '恢复请求已在进行中，请勿重复点击', retryable: false }
  }
  inFlight.add(taskId)
  const timeoutMs = opts.timeoutMs ?? 10_000
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  const statusImpl = opts.statusImpl ?? ((t: string) => fetchTaskStatus(t, fetchImpl))
  try {
    let res: Response
    try {
      res = await fetchImpl(`/api/task/${encodeURIComponent(taskId)}/resume`, {
        method: 'POST',
        signal: ctrl.signal,
      })
    } catch (e: any) {
      if (!ctrl.signal.aborted) {
        return { ok: false, phase: 'failed', message: `恢复请求发送失败：${String(e?.message || e)}`, retryable: true }
      }
      // 有界超时：先查真实任务状态，防止"以为失败其实已恢复"的重复恢复
      const st = await statusImpl(taskId)
      if (RESUMED_STATUSES.has(st)) {
        return {
          ok: true, phase: 'ok', retryable: false, resumed: true, taskStatus: st,
          message: `恢复请求超时，但查询到任务已在运行（${st}）——不会重复恢复`,
        }
      }
      const confirmed = st ? `任务仍是 ${st}` : '未能确认任务当前状态'
      return {
        ok: false, phase: 'timeout', retryable: true, taskStatus: st,
        message: `恢复请求超时（${Math.round(timeoutMs / 1000)}s），${confirmed}；可再次点击重试（服务端有状态门槛，不会双重接管）`,
      }
    }
    let data: any = null
    try { data = await res.json() } catch { /* 非 JSON 响应按原文处理 */ }
    if (!res.ok) {
      const serverRetryable = typeof data?.retryable === 'boolean' ? data.retryable : true
      const serverStatus = String(data?.status || '')
      const serverResumed = data?.resumed === true || RESUMED_STATUSES.has(serverStatus.toUpperCase())
      return {
        ok: false,
        phase: 'failed',
        message: String(data?.error || `恢复失败（HTTP ${res.status}）`) +
          (serverResumed ? '（任务已在运行，刷新即可看到进展）' : ''),
        retryable: serverRetryable && !serverResumed,
        resumed: serverResumed,
        taskStatus: serverStatus,
      }
    }
    return {
      ok: true,
      phase: 'ok',
      retryable: false,
      resumed: true,
      message: String(data?.note || '已请求恢复：编排器将跳过已完成步骤，只跑未完步骤'),
    }
  } finally {
    clearTimeout(timer)
    inFlight.delete(taskId)
  }
}
