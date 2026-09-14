// 任务状态的**唯一语义实现**：颜色、中文名、图标全部从这里取。
//
// 背景（实测）：同一状态在不同页面各写一套映射——ConsoleSideTabs 用 10px 徽章、
// History 用 12px 徽章、Agents 又有一套 statusDot/statusLabel，同一个"运行中"出现
// 三种颜色；而且徽章直接显示后端英文枚举（SUCCESS_WITH_ISSUES），中文用户看不懂。
// 新增状态时只改这里。

export type StatusKind = 'running' | 'success' | 'issue' | 'failure' | 'queued' | 'cancelled' | 'unknown'

const KIND_BY_STATUS: Record<string, StatusKind> = {
  RUNNING: 'running',
  SUCCESS: 'success',
  SUCCESS_WITH_ISSUES: 'issue',
  FAILED: 'failure',
  QUEUED: 'queued',
  PENDING: 'queued',
  CANCELLED: 'cancelled',
  CANCELED: 'cancelled',
}

/** 后端状态枚举 → 语义档位。 */
export function statusKind(raw?: string): StatusKind {
  return KIND_BY_STATUS[String(raw || '').toUpperCase()] || 'unknown'
}

/** 语义档位 → 中文名（面向用户，不再直接显示英文枚举）。 */
const LABEL: Record<StatusKind, string> = {
  running: '运行中',
  success: '成功',
  issue: '有缺口',
  failure: '失败',
  queued: '排队中',
  cancelled: '已取消',
  unknown: '未知',
}

export function statusLabel(raw?: string): string {
  return LABEL[statusKind(raw)]
}

/** 语义档位 → 徽章类名（同语义只有一种颜色，四态用同一套透明度）。 */
export function statusBadgeClass(raw?: string): string {
  const base = 'px-2 py-0.5 rounded-full text-xs font-semibold whitespace-nowrap'
  switch (statusKind(raw)) {
    case 'success': return `${base} bg-emerald-500/15 text-emerald-400 border border-emerald-500/25`
    case 'issue': return `${base} bg-amber-500/15 text-amber-400 border border-amber-500/25`
    case 'failure': return `${base} bg-red-500/15 text-red-400 border border-red-500/25`
    case 'running': return `${base} bg-cyan-500/15 text-cyan-400 border border-cyan-500/25`
    case 'cancelled': return `${base} bg-slate-500/15 text-slate-400 border border-slate-500/25`
    default: return `${base} bg-slate-500/15 text-slate-400 border border-slate-600/30`
  }
}

/** 语义档位 → 圆点类名（Agent 列表等紧凑场景）。 */
export function statusDotClass(raw?: string): string {
  switch (statusKind(raw)) {
    case 'success': return 'w-2 h-2 rounded-full bg-emerald-400'
    case 'issue': return 'w-2 h-2 rounded-full bg-amber-400'
    case 'failure': return 'w-2 h-2 rounded-full bg-red-400'
    case 'running': return 'w-2 h-2 rounded-full bg-cyan-400 animate-pulse'
    case 'cancelled': return 'w-2 h-2 rounded-full bg-slate-500'
    default: return 'w-2 h-2 rounded-full bg-slate-600'
  }
}

/** 是否"已完成"（终态）——决定是否展示报告/重跑等操作。 */
export function isTerminal(raw?: string): boolean {
  return ['success', 'issue', 'failure', 'cancelled'].includes(statusKind(raw))
}

/** 完成度描述：给"有缺口"这类状态一句人话（徽章之外的第二层信息）。 */
export function statusHint(raw?: string): string {
  switch (statusKind(raw)) {
    case 'issue': return '已完成交付，但存在未通过的验收项'
    case 'failure': return '未完成交付，请查看失败原因'
    case 'cancelled': return '已被手动停止'
    case 'running': return '正在执行，可查看实时动态'
    case 'queued': return '已受理，等待执行'
    default: return ''
  }
}
