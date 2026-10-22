import { useState, memo, useMemo } from 'react'
import { TaskNode } from '../stores/types'
import {
  CheckCircle2, XCircle, Circle, Loader2, AlertTriangle,
  ChevronRight, ChevronDown, RefreshCw, GripVertical
} from 'lucide-react'
import { stepStatusMeta } from '../lib/statusMeta'
import { gateSkipReason, isGateSkipDisplay, GATE_SKIP_LABEL } from '../lib/stepSkip'
import { foldPlanDescriptions } from '../lib/planText'

// 步骤状态：颜色来自 lib/statusMeta（唯一实现），图标仍由本组件选择（图标是 UI 关注点）
const statusConfig = {
  success:  { icon: CheckCircle2,   ...stepStatusMeta('success') },
  failed:   { icon: XCircle,        ...stepStatusMeta('failed') },
  running:  { icon: Loader2,        ...stepStatusMeta('running') },
  skipped:  { icon: AlertTriangle,  ...stepStatusMeta('skipped') },
  pending:  { icon: Circle,         ...stepStatusMeta('pending') },
} as const

function StatusIcon({ status }: { status: TaskNode['status'] }) {
  const cfg = statusConfig[status]
  const Icon = cfg.icon
  const spin = status === 'running' ? 'animate-spin' : ''
  return <Icon className={`w-4 h-4 ${cfg.text} ${spin} shrink-0`} />
}

function ReplanPopover({ history }: { history: TaskNode[][] }) {
  const [open, setOpen] = useState(false)
  return (
    <span className="relative">
      <button onClick={e => { e.stopPropagation(); setOpen(!open) }}
        className="ml-1 p-0.5 hover:bg-line rounded transition-colors"
        title="查看重规划历史">
        <RefreshCw className="w-3.5 h-3.5 text-amber-700" />
      </button>
      {open && (
        <div className="absolute left-6 top-0 z-20 w-72 bg-surface-raised border border-line-strong rounded-lg shadow-2xl p-3 text-xs">
          <div className="flex items-center justify-between mb-2">
            <span className="text-amber-700 font-semibold">重规划历史</span>
            <button onClick={() => setOpen(false)} className="text-ink-faint hover:text-ink">✕</button>
          </div>
          {history.map((attempt, i) => (
            <div key={i} className="mb-2 last:mb-0">
              <div className="text-ink-faint mb-1">第 {i + 1} 次尝试</div>
              {attempt.map(s => (
                <div key={s.id} className="flex items-center gap-2 py-0.5 ml-2">
                  <StatusIcon status={s.status} />
                  <span className="text-ink-muted">{s.name}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </span>
  )
}

const TreeNode = memo(function TreeNode({
  node, depth = 0, maxVisible = 2, onSelect, editable = false, onMove, onDelete, label, hint
}: {
  node: TaskNode; depth: number; maxVisible: number; onSelect?: (node: TaskNode) => void
  editable?: boolean; onMove?: (id: string, dir: -1 | 1) => void; onDelete?: (id: string) => void
  /** P2-4b：折叠后的行名（能力动作：该步关键动作）；不传则显示原始 name */
  label?: string
  /** P2-4b：悬停可看的完整原文（被折叠的共用说明仍可查） */
  hint?: string
}) {
  const [expanded, setExpanded] = useState(depth < maxVisible)
  const hasChildren = node.children.length > 0
  const cfg = statusConfig[node.status]
  const isRoot = depth === 0
  // P2-4b：本层各步共用的说明只显示一处；行名换成「能力动作：该步关键动作」。
  // 完整原文保留在 title（hint）里，不做信息删除。
  const fold = useMemo(() => foldPlanDescriptions(node.children), [node.children])

  return (
    <div className="relative">
      <div
        onClick={() => onSelect?.(node)}
        className={`flex items-center gap-2 py-1.5 group cursor-pointer ${isRoot ? 'mb-2' : ''}`}>
        {hasChildren ? (
          <button onClick={e => { e.stopPropagation(); setExpanded(!expanded) }}
            className="p-0.5 hover:bg-line rounded transition-colors shrink-0">
            {expanded ? <ChevronDown className="w-3.5 h-3.5 text-ink-faint" /> :
                        <ChevronRight className="w-3.5 h-3.5 text-ink-faint" />}
          </button>
        ) : (
          <GripVertical className="w-3.5 h-3.5 text-ink shrink-0 opacity-0 group-hover:opacity-50 transition-opacity" />
        )}

        <div className={`shrink-0 w-2.5 h-2.5 rounded-full ${cfg.dot}
          ${node.status === 'running' ? 'animate-pulse shadow-[0_0_8px] shadow-cyan-400/50' : ''}`} />

        <StatusIcon status={node.status} />

        <span className="text-xs font-mono text-ink-faint bg-surface-raised/60 px-1.5 py-0.5 rounded shrink-0">
          {node.capability || 'goal'}
        </span>

        {!!node.iteration && (
          <span className="text-xs font-mono text-violet-700 bg-violet-500/10 px-1.5 py-0.5 rounded shrink-0">
            第{node.iteration}轮
          </span>
        )}
        {(node.step_id?.startsWith('fix-')) && (
          <span className="text-xs font-mono text-orange-700 bg-orange-500/10 px-1.5 py-0.5 rounded shrink-0">
            修复
          </span>
        )}
        {(node.step_id?.startsWith('alt-')) && (
          <span className="text-xs font-mono text-brand bg-brand/10 px-1.5 py-0.5 rounded shrink-0">
            备选
          </span>
        )}

        {/* S1 门禁跳过（资料缺口）：中性呈现，不是失败。原因悬停可看（后端原文）。 */}
        {isGateSkipDisplay(node.status, node) && (
          <span
            className="text-xs font-mono text-amber-700 bg-amber-500/10 border border-amber-500/20 px-1.5 py-0.5 rounded shrink-0"
            title={gateSkipReason(node)}
          >
            {GATE_SKIP_LABEL}
          </span>
        )}

        <span className={`truncate text-sm ${isRoot ? 'text-ink font-semibold' : cfg.text}`}
          title={hint || label || node.name}>
          {label || node.name}
        </span>

        {node.agent_id && (
          <span className="text-xs text-ink-faint bg-surface-raised/40 px-1.5 py-0.5 rounded-full shrink-0 ml-auto">
            {node.agent_id.replace('_', ' ')}
          </span>
        )}

        {editable && depth >= 1 && (
          <span className="flex items-center gap-0.5 shrink-0">
            <button onClick={e => { e.stopPropagation(); onMove?.(node.id, -1) }} title="上移"
              className="w-5 h-5 text-xs text-ink-faint hover:text-brand hover:bg-surface-raised rounded transition-colors">↑</button>
            <button onClick={e => { e.stopPropagation(); onMove?.(node.id, 1) }} title="下移"
              className="w-5 h-5 text-xs text-ink-faint hover:text-brand hover:bg-surface-raised rounded transition-colors">↓</button>
            <button onClick={e => { e.stopPropagation(); onDelete?.(node.id) }} title="删除"
              className="w-5 h-5 text-xs text-ink-faint hover:text-red-700 hover:bg-red-500/10 rounded transition-colors">×</button>
          </span>
        )}

        {node.replanHistory && node.replanHistory.length > 0 && (
          <ReplanPopover history={node.replanHistory} />
        )}
      </div>

      {hasChildren && expanded && (
        <div className="relative" style={{ marginLeft: 16 }}>
          <div className="absolute left-[2px] top-0 bottom-0 w-px bg-line" />
          {/* P2-4b：被折叠的共用说明在这里只显示一处（原文可悬停/换行看全） */}
          {fold.shared.map((s, i) => (
            <div key={'shared-' + i} style={{ paddingLeft: 20 }}
              className="text-xs text-ink-faint break-words max-h-12 overflow-y-auto pb-1"
              title={s.text}>
              共用说明（{s.count} 步相同，已折叠到这一处）：{s.text}
            </div>
          ))}
          {node.children.map((child, i) => (
            <div key={child.id} className="relative" style={{ paddingLeft: 20 }}>
              <svg className="absolute left-[3px] top-4 w-[17px] h-px" style={{ overflow: 'visible' }}>
                <line x1="0" y1="0" x2="16" y2="0"
                  className={`${child.status === 'failed' ? 'stroke-red-400' :
                    child.status === 'running' ? 'stroke-brand' :
                    child.status === 'success' ? 'stroke-emerald-400' : 'stroke-line-strong'}`}
                  strokeWidth="1" />
              </svg>
              {i === node.children.length - 1 && (
                <div className="absolute left-[3px] top-0 w-px bg-surface"
                     style={{ height: 'calc(1rem + 2px)' }} />
              )}
              <TreeNode node={child} depth={depth + 1} maxVisible={maxVisible} onSelect={onSelect}
                editable={editable} onMove={onMove} onDelete={onDelete}
                label={fold.children[i]?.name} hint={fold.children[i]?.description} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
})

export default memo(function TaskTreeView({
  root, onSelect, editable = false, onMove, onDelete
}: {
  root: TaskNode | null; onSelect?: (node: TaskNode) => void
  editable?: boolean; onMove?: (id: string, dir: -1 | 1) => void; onDelete?: (id: string) => void
}) {
  if (!root) {
    return (
      <div className="flex flex-col items-center justify-center h-64 text-ink-faint text-sm">
        <GripVertical className="w-8 h-8 mb-3 opacity-20" />
        <p>暂无执行计划</p>
        <p className="text-xs mt-1 opacity-60">提交任务开始</p>
      </div>
    )
  }

  const countStats = () => {
    const s = { success: 0, failed: 0, running: 0, pending: 0, skipped: 0, total: 0 }
    const walk = (n: TaskNode) => {
      s.total++
      if (n.status === 'success') s.success++
      else if (n.status === 'failed') s.failed++
      else if (n.status === 'running') s.running++
      else if (n.status === 'skipped') s.skipped++
      else s.pending++
      n.children.forEach(walk)
    }
    walk(root)
    return s
  }

  const stats = countStats()
  const pct = stats.total > 0 ? Math.round(((stats.success + stats.skipped) / stats.total) * 100) : 0

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <div className="flex-1 h-1.5 bg-surface-raised rounded-full overflow-hidden">
          <div className="h-full bg-brand rounded-full transition-all duration-700 ease-out"
               style={{ width: `${pct}%` }} />
        </div>
        <span className="text-xs text-ink-faint font-mono tabular-nums whitespace-nowrap">
          {stats.success + stats.skipped}/{stats.total}
        </span>
      </div>

      <div className="pl-1">
        <TreeNode node={root} depth={0} maxVisible={2} onSelect={onSelect}
          editable={editable} onMove={onMove} onDelete={onDelete} />
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 pt-3 border-t border-line text-xs">
        <span className="flex items-center gap-1 text-emerald-700"><span className="w-2 h-2 rounded-full bg-state-success" /> {stats.success} {stepStatusMeta('success').label}</span>
        <span className="flex items-center gap-1 text-red-700"><span className="w-2 h-2 rounded-full bg-state-failure" /> {stats.failed} {stepStatusMeta('failed').label}</span>
        {stats.running > 0 && <span className="flex items-center gap-1 text-brand"><span className="w-2 h-2 rounded-full bg-brand animate-pulse" /> {stats.running} {stepStatusMeta('running').label}</span>}
        {stats.skipped > 0 && <span className="flex items-center gap-1 text-amber-700"><span className="w-2 h-2 rounded-full bg-state-issue" /> {stats.skipped} {stepStatusMeta('skipped').label}</span>}
        {stats.pending > 0 && <span className="flex items-center gap-1 text-ink-faint"><span className="w-2 h-2 rounded-full bg-line-strong" /> {stats.pending} {stepStatusMeta('pending').label}</span>}
      </div>
    </div>
  )
})
