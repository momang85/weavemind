import { useState, memo } from 'react'
import { TaskNode } from '../stores/types'
import {
  CheckCircle2, XCircle, Circle, Loader2, AlertTriangle,
  ChevronRight, ChevronDown, RefreshCw, GripVertical
} from 'lucide-react'

const statusConfig = {
  success:  { icon: CheckCircle2,   line: 'bg-emerald-400',  dot: 'bg-emerald-400',  text: 'text-emerald-300', bg: 'border-emerald-500/20 bg-emerald-500/5' },
  failed:   { icon: XCircle,        line: 'bg-red-400',       dot: 'bg-red-400',       text: 'text-red-300',      bg: 'border-red-500/20 bg-red-500/5' },
  running:  { icon: Loader2,        line: 'bg-cyan-400',      dot: 'bg-cyan-400',       text: 'text-cyan-300',     bg: 'border-cyan-500/20 bg-cyan-500/5' },
  skipped:  { icon: AlertTriangle,  line: 'bg-amber-400',     dot: 'bg-amber-400',     text: 'text-amber-300',    bg: 'border-amber-500/20 bg-amber-500/5' },
  pending:  { icon: Circle,         line: 'bg-slate-700',     dot: 'bg-slate-600',     text: 'text-slate-500',    bg: 'border-slate-800' },
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
        className="ml-1 p-0.5 hover:bg-slate-700 rounded transition-colors"
        title="View replan history">
        <RefreshCw className="w-3.5 h-3.5 text-amber-400" />
      </button>
      {open && (
        <div className="absolute left-6 top-0 z-20 w-72 bg-slate-800 border border-slate-700 rounded-lg shadow-2xl p-3 text-xs">
          <div className="flex items-center justify-between mb-2">
            <span className="text-amber-400 font-semibold">重规划历史</span>
            <button onClick={() => setOpen(false)} className="text-slate-500 hover:text-slate-300">✕</button>
          </div>
          {history.map((attempt, i) => (
            <div key={i} className="mb-2 last:mb-0">
              <div className="text-slate-500 mb-1">Attempt {i + 1}</div>
              {attempt.map(s => (
                <div key={s.id} className="flex items-center gap-2 py-0.5 ml-2">
                  <StatusIcon status={s.status} />
                  <span className="text-slate-400">{s.name}</span>
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
  node, depth = 0, maxVisible = 2, onSelect, editable = false, onMove, onDelete
}: {
  node: TaskNode; depth: number; maxVisible: number; onSelect?: (node: TaskNode) => void
  editable?: boolean; onMove?: (id: string, dir: -1 | 1) => void; onDelete?: (id: string) => void
}) {
  const [expanded, setExpanded] = useState(depth < maxVisible)
  const hasChildren = node.children.length > 0
  const cfg = statusConfig[node.status]
  const isRoot = depth === 0

  return (
    <div className="relative">
      <div
        onClick={() => onSelect?.(node)}
        className={`flex items-center gap-2 py-1.5 group cursor-pointer ${isRoot ? 'mb-2' : ''}`}>
        {hasChildren ? (
          <button onClick={e => { e.stopPropagation(); setExpanded(!expanded) }}
            className="p-0.5 hover:bg-slate-700 rounded transition-colors shrink-0">
            {expanded ? <ChevronDown className="w-3.5 h-3.5 text-slate-500" /> :
                        <ChevronRight className="w-3.5 h-3.5 text-slate-500" />}
          </button>
        ) : (
          <GripVertical className="w-3.5 h-3.5 text-slate-700 shrink-0 opacity-0 group-hover:opacity-50 transition-opacity" />
        )}

        <div className={`shrink-0 w-2.5 h-2.5 rounded-full ${cfg.dot}
          ${node.status === 'running' ? 'animate-pulse shadow-[0_0_8px] shadow-cyan-400/50' : ''}`} />

        <StatusIcon status={node.status} />

        <span className="text-[10px] font-mono text-slate-600 bg-slate-800/60 px-1.5 py-0.5 rounded shrink-0">
          {node.capability || 'goal'}
        </span>

        {!!node.iteration && (
          <span className="text-[10px] font-mono text-violet-400 bg-violet-500/10 px-1.5 py-0.5 rounded shrink-0">
            第{node.iteration}轮
          </span>
        )}

        <span className={`truncate text-sm ${isRoot ? 'text-slate-100 font-semibold' : cfg.text}`}>
          {node.name}
        </span>

        {node.agent_id && (
          <span className="text-[10px] text-slate-500 bg-slate-800/40 px-1.5 py-0.5 rounded-full shrink-0 ml-auto">
            {node.agent_id.replace('_', ' ')}
          </span>
        )}

        {editable && depth >= 1 && (
          <span className="flex items-center gap-0.5 shrink-0">
            <button onClick={e => { e.stopPropagation(); onMove?.(node.id, -1) }} title="上移"
              className="w-5 h-5 text-[10px] text-slate-500 hover:text-cyan-400 hover:bg-slate-800 rounded transition-colors">↑</button>
            <button onClick={e => { e.stopPropagation(); onMove?.(node.id, 1) }} title="下移"
              className="w-5 h-5 text-[10px] text-slate-500 hover:text-cyan-400 hover:bg-slate-800 rounded transition-colors">↓</button>
            <button onClick={e => { e.stopPropagation(); onDelete?.(node.id) }} title="删除"
              className="w-5 h-5 text-[10px] text-slate-500 hover:text-red-400 hover:bg-red-500/10 rounded transition-colors">×</button>
          </span>
        )}

        {node.replanHistory && node.replanHistory.length > 0 && (
          <ReplanPopover history={node.replanHistory} />
        )}
      </div>

      {hasChildren && expanded && (
        <div className="relative" style={{ marginLeft: 16 }}>
          <div className="absolute left-[2px] top-0 bottom-0 w-px bg-slate-700" />
          {node.children.map((child, i) => (
            <div key={child.id} className="relative" style={{ paddingLeft: 20 }}>
              <svg className="absolute left-[3px] top-4 w-[17px] h-px" style={{ overflow: 'visible' }}>
                <line x1="0" y1="0" x2="16" y2="0"
                  className={`${child.status === 'failed' ? 'stroke-red-400' :
                    child.status === 'running' ? 'stroke-cyan-400' :
                    child.status === 'success' ? 'stroke-emerald-400' : 'stroke-slate-700'}`}
                  strokeWidth="1" />
              </svg>
              {i === node.children.length - 1 && (
                <div className="absolute left-[3px] top-0 w-px bg-slate-900"
                     style={{ height: 'calc(1rem + 2px)' }} />
              )}
              <TreeNode node={child} depth={depth + 1} maxVisible={maxVisible} onSelect={onSelect}
                editable={editable} onMove={onMove} onDelete={onDelete} />
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
      <div className="flex flex-col items-center justify-center h-64 text-slate-600 text-sm">
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
        <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
          <div className="h-full bg-gradient-to-r from-emerald-400 to-cyan-400 rounded-full transition-all duration-700 ease-out"
               style={{ width: `${pct}%` }} />
        </div>
        <span className="text-xs text-slate-500 font-mono tabular-nums whitespace-nowrap">
          {stats.success + stats.skipped}/{stats.total}
        </span>
      </div>

      <div className="pl-1">
        <TreeNode node={root} depth={0} maxVisible={2} onSelect={onSelect}
          editable={editable} onMove={onMove} onDelete={onDelete} />
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 pt-3 border-t border-slate-800 text-[11px]">
        <span className="flex items-center gap-1 text-emerald-400"><span className="w-2 h-2 rounded-full bg-emerald-400" /> {stats.success} success</span>
        <span className="flex items-center gap-1 text-red-400"><span className="w-2 h-2 rounded-full bg-red-400" /> {stats.failed} failed</span>
        {stats.running > 0 && <span className="flex items-center gap-1 text-cyan-400"><span className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse" /> {stats.running} running</span>}
        {stats.skipped > 0 && <span className="flex items-center gap-1 text-amber-400"><span className="w-2 h-2 rounded-full bg-amber-400" /> {stats.skipped} skipped</span>}
        {stats.pending > 0 && <span className="flex items-center gap-1 text-slate-500"><span className="w-2 h-2 rounded-full bg-slate-600" /> {stats.pending} pending</span>}
      </div>
    </div>
  )
})
