// 最小 UI 原语集：Card / StatCard / StatusBadge / Alert / EmptyState / Skeleton / SectionTitle。
//
// 背景（实测）：卡片容器、状态徽章、空态、告警横幅在同一项目里各写 3-5 份——
// 5 份手写统计卡、5 个重复的刷新按钮、约 10 处手写空态、4 种横幅写法，字号与圆角互不一致
// （全仓 101 处小于 12px 的字号多来自这些散写）。这里给出唯一实现：新代码一律复用，
// 字号最低 text-xs（12px），数值统一 tabular-nums（对齐、扫读更稳）。

import { ReactNode } from 'react'
import { AlertTriangle, Inbox, Info, Loader2, OctagonAlert } from 'lucide-react'
import { statusBadgeClass, statusHint, statusLabel } from '../lib/statusMeta'

// ── 容器 ──────────────────────────────────────────────────────────

export function Card({ children, className = '', padded = true }: {
  children: ReactNode; className?: string; padded?: boolean
}) {
  return (
    <div className={`bg-surface border border-line rounded-xl ${padded ? 'p-4 md:p-5' : ''} ${className}`}>
      {children}
    </div>
  )
}

export function SectionTitle({ icon, title, extra }: {
  icon?: ReactNode; title: string; extra?: ReactNode
}) {
  return (
    <div className="flex items-center gap-2 mb-3">
      {icon}
      <h2 className="text-sm font-medium text-ink-muted">{title}</h2>
      {extra && <div className="ml-auto text-xs text-ink-faint">{extra}</div>}
    </div>
  )
}

/** 指标卡：数值等宽对齐；`hint` 写口径/样本量（避免"看着像结论其实是 0 样本"）。 */
export function StatCard({ label, value, sub, hint, tone = 'default' }: {
  label: string; value: string; sub?: string; hint?: string
  tone?: 'default' | 'success' | 'issue' | 'failure'
}) {
  const color = tone === 'success' ? 'text-state-success'
    : tone === 'issue' ? 'text-state-issue'
    : tone === 'failure' ? 'text-state-failure' : 'text-ink'
  return (
    <div className="bg-surface border border-line rounded-xl p-4">
      <div className="text-xs text-ink-faint">{label}</div>
      <div className={`text-2xl font-semibold mt-1 tabular-nums ${color}`}>{value}</div>
      {sub && <div className="text-xs text-ink-faint mt-0.5">{sub}</div>}
      {hint && <div className="text-xs text-ink-faint/80 mt-1 leading-snug">{hint}</div>}
    </div>
  )
}

// ── 状态 ──────────────────────────────────────────────────────────

/** 任务状态徽章：颜色与中文名来自 lib/statusMeta（唯一实现），不再直接显示英文枚举。 */
export function StatusBadge({ status, withHint = false }: { status?: string; withHint?: boolean }) {
  const hint = statusHint(status)
  return (
    <span className="inline-flex items-center gap-2">
      <span className={statusBadgeClass(status)}>{statusLabel(status)}</span>
      {withHint && hint && <span className="text-xs text-slate-500">{hint}</span>}
    </span>
  )
}

// ── 反馈 ──────────────────────────────────────────────────────────

const ALERT_STYLE = {
  info: { cls: 'bg-cyan-500/10 border-cyan-500/25 text-cyan-300', Icon: Info },
  warn: { cls: 'bg-amber-500/10 border-amber-500/25 text-amber-300', Icon: AlertTriangle },
  danger: { cls: 'bg-red-500/10 border-red-500/25 text-red-300', Icon: OctagonAlert },
} as const

/** 统一告警/提示条：info / warn / danger 三档，替换散写的四种横幅。 */
export function Alert({ tone = 'info', title, children, action }: {
  tone?: keyof typeof ALERT_STYLE; title?: string; children?: ReactNode; action?: ReactNode
}) {
  const { cls, Icon } = ALERT_STYLE[tone]
  return (
    <div className={`border rounded-lg px-4 py-3 text-sm ${cls}`}>
      <div className="flex items-start gap-2">
        <Icon className="w-4 h-4 mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          {title && <div className="font-medium">{title}</div>}
          {children && <div className={title ? 'mt-1 opacity-90' : ''}>{children}</div>}
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </div>
    </div>
  )
}

/** 空态：必须说清"为什么没有"与"下一步做什么"，不再只写一句"暂无"。 */
export function EmptyState({ title, description, action, icon }: {
  title: string; description?: string; action?: ReactNode; icon?: ReactNode
}) {
  return (
    <div className="text-center py-10 px-4">
      <div className="inline-flex items-center justify-center w-10 h-10 rounded-full bg-surface-raised text-ink-faint mb-3">
        {icon || <Inbox className="w-5 h-5" />}
      </div>
      <div className="text-sm text-ink-muted">{title}</div>
      {description && <div className="text-xs text-ink-faint mt-1 leading-relaxed max-w-md mx-auto">{description}</div>}
      {action && <div className="mt-3 flex justify-center">{action}</div>}
    </div>
  )
}

/** 加载骨架：替代"加载中…"文字，形状与最终内容一致，避免布局跳动。 */
export function Skeleton({ className = 'h-4 w-full' }: { className?: string }) {
  return <div className={`animate-pulse rounded bg-surface-raised/70 ${className}`} />
}

export function LoadingBlock({ label = '加载中…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center py-10 text-ink-faint text-xs">
      <Loader2 className="w-4 h-4 animate-spin mr-2" /> {label}
    </div>
  )
}

/** 错误态：与空态区分——必须给出原因与重试入口（实测 History 曾把故障显示成"暂无记录"）。 */
export function ErrorState({ title = '加载失败', description, onRetry }: {
  title?: string; description?: string; onRetry?: () => void
}) {
  return (
    <Alert tone="danger" title={title}
      action={onRetry ? (
        <button onClick={onRetry}
          className="px-3 py-1.5 rounded-lg bg-red-500/15 hover:bg-red-500/25 text-red-300 text-xs transition-colors">
          重试
        </button>
      ) : undefined}>
      {description || '请检查后端服务是否可用。'}
    </Alert>
  )
}
