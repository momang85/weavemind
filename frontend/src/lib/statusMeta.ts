// 任务状态的**唯一语义实现**：颜色、中文名、图标全部从这里取。
//
// 背景（实测）：同一状态在不同页面各写一套映射——ConsoleSideTabs 用 10px 徽章、
// History 用 12px 徽章、Agents 又有一套 statusDot/statusLabel，同一个"运行中"出现
// 三种颜色；而且徽章直接显示后端英文枚举（SUCCESS_WITH_ISSUES），中文用户看不懂。
// 新增状态时只改这里。

export type StatusKind = 'running' | 'success' | 'issue' | 'failure' | 'queued' | 'cancelled' | 'skipped' | 'unknown'

const KIND_BY_STATUS: Record<string, StatusKind> = {
  RUNNING: 'running',
  SUCCESS: 'success',
  SUCCESS_WITH_ISSUES: 'issue',
  FAILED: 'failure',
  FAILURE: 'failure',
  QUEUED: 'queued',
  PENDING: 'queued',
  CANCELLED: 'cancelled',
  CANCELED: 'cancelled',
  SKIPPED: 'skipped',
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
  skipped: '已跳过',
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
    case 'skipped': return `${base} bg-amber-500/15 text-amber-400 border border-amber-500/25`
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
    case 'skipped': return 'w-2 h-2 rounded-full bg-amber-400'
    case 'failure': return 'w-2 h-2 rounded-full bg-red-400'
    case 'running': return 'w-2 h-2 rounded-full bg-cyan-400 animate-pulse'
    case 'cancelled': return 'w-2 h-2 rounded-full bg-slate-500'
    default: return 'w-2 h-2 rounded-full bg-slate-600'
  }
}

/** 步骤节点配色（图标由组件自选）：line=计划树连线、dot=状态点、text=文字、bg=卡片边框/底。 */
export function stepStatusMeta(raw?: string): {
  label: string; line: string; dot: string; text: string; bg: string
} {
  switch (statusKind(raw)) {
    case 'success':
      return { label: '成功', line: 'bg-emerald-400', dot: 'bg-emerald-400', text: 'text-emerald-300', bg: 'border-emerald-500/20 bg-emerald-500/5' }
    case 'failure':
      return { label: '失败', line: 'bg-red-400', dot: 'bg-red-400', text: 'text-red-300', bg: 'border-red-500/20 bg-red-500/5' }
    case 'running':
      return { label: '运行中', line: 'bg-cyan-400', dot: 'bg-cyan-400', text: 'text-cyan-300', bg: 'border-cyan-500/20 bg-cyan-500/5' }
    case 'skipped':
      return { label: '已跳过', line: 'bg-amber-400', dot: 'bg-amber-400', text: 'text-amber-300', bg: 'border-amber-500/20 bg-amber-500/5' }
    default:
      return { label: '待执行', line: 'bg-slate-700', dot: 'bg-slate-600', text: 'text-slate-500', bg: 'border-slate-800' }
  }
}

/** Agent 运行状态（registry 的状态串：offline / active:N/M / idle:0/M）。
 *  统一用青色表示"忙碌"——此前 Agents 页用蓝色、其他地方用青色，同一语义两种颜色。 */
export function agentStatusLabel(status?: string): string {
  const s = String(status || '')
  if (!s || s.startsWith('offline')) return '离线'
  if (s.includes('active')) return '忙碌'
  return '空闲'
}

export function agentStatusDotClass(status?: string): string {
  const s = String(status || '')
  if (!s || s.startsWith('offline')) return 'w-2 h-2 rounded-full bg-slate-500'
  if (s.includes('active')) return 'w-2.5 h-2.5 rounded-full bg-cyan-400 animate-pulse animate-pulse'
  return 'w-2 h-2 rounded-full bg-emerald-400'
}

/** 审计日志 / 定时任务等"结果"字段（ok / fail / error / denied）→ 中文与颜色。 */
export function resultLabel(raw?: string): string {
  const r = String(raw || '').trim().toLowerCase()
  if (r === 'ok' || r === 'success') return '成功'
  if (r === 'fail' || r === 'failed' || r === 'error') return '失败'
  if (r === 'denied') return '拒绝'
  return raw ? String(raw) : '—'
}

export function resultClass(raw?: string): string {
  const r = String(raw || '').trim().toLowerCase()
  if (r === 'ok' || r === 'success') return 'text-emerald-400'
  if (r === 'fail' || r === 'failed' || r === 'error') return 'text-red-400'
  if (r === 'denied') return 'text-amber-400'
  return 'text-slate-400'
}

/** 是否"已完成"（终态）——决定是否展示报告/重跑等操作。 */
export function isTerminal(raw?: string): boolean {
  return ['success', 'issue', 'failure', 'cancelled', 'skipped'].includes(statusKind(raw))
}

/** 完成度描述：给"有缺口"这类状态一句人话（徽章之外的第二层信息）。 */
export function statusHint(raw?: string): string {
  switch (statusKind(raw)) {
    case 'issue': return '已完成交付，但存在未通过的验收项'
    case 'failure': return '未完成交付，请查看失败原因'
    case 'cancelled': return '已被手动停止'
    case 'skipped': return '上游失败或依赖未满足，本步骤被跳过'
    case 'running': return '正在执行，可查看实时动态'
    case 'queued': return '已受理，等待执行'
    default: return ''
  }
}
