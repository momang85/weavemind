// 运行中任务刷新后恢复跟踪。
//
// 此前 `currentTaskId` 只在内存 store 里：刷新页面后前端不再跟踪该任务——后端日志还在、
// 页面却空着（实测运行中重载后日志区 0 行）。这里把"最后一次提交的任务"记进
// **sessionStorage**（按标签页，关标签即清），刷新后凭它重新订阅 SSE/轮询。
//
// 只恢复**未终态**的任务：任务结束后刷新回到空白控制台是原有行为，不顺手改掉。

const KEY = 'wm.lastTask'

export type LastTask = { id: string; startedAt: number }

function _store(): Storage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage
  } catch {
    return null   // 隐私模式/被禁用时静默降级（恢复不了跟踪，但别把页面弄崩）
  }
}

export function saveLastTask(id: string, startedAt = Date.now()): void {
  if (!id) return
  try {
    _store()?.setItem(KEY, JSON.stringify({ id, startedAt }))
  } catch { /* 配额/隐私模式：不影响任务本身 */ }
}

export function readLastTask(): LastTask | null {
  try {
    const raw = _store()?.getItem(KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    const id = String(parsed?.id || '')
    if (!id) return null
    const startedAt = Number(parsed?.startedAt)
    return { id, startedAt: Number.isFinite(startedAt) && startedAt > 0 ? startedAt : 0 }
  } catch {
    return null   // 脏数据当作没有，不猜
  }
}

export function clearLastTask(): void {
  try {
    _store()?.removeItem(KEY)
  } catch { /* ignore */ }
}

/** 这些状态下刷新要恢复跟踪（含"等确认"这种暂停态）。终态一律不恢复。 */
export function shouldResume(status: string | null | undefined): boolean {
  const s = String(status || '').toUpperCase()
  return s === 'RUNNING' || s === 'PENDING' || s === 'QUEUED' || s === 'AWAITING_CONFIRM'
}
