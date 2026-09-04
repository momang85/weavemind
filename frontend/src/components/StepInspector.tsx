import { useState } from 'react'
import type { TaskNode } from '../stores/types'
import { X, ChevronDown, ChevronUp } from 'lucide-react'

const roleColors: Record<string, string> = {
  planner: '#38bdf8', critic: '#c084fc', worker: '#6ee7b7',
  memory: '#fbbf24', replan: '#fb923c', error: '#fca5a5',
}

function statusColor(status: string) {
  if (status === 'success') return 'text-emerald-400'
  if (status === 'failed') return 'text-red-400'
  if (status === 'running') return 'text-cyan-400'
  if (status === 'skipped') return 'text-amber-400'
  return 'text-slate-400'
}

function formatResult(result: any): string {
  if (result === null || result === undefined) return ''
  if (typeof result === 'string') return result
  try {
    return JSON.stringify(result, null, 2)
  } catch {
    return String(result)
  }
}

/** 长文本折叠：超过阈值默认收起，点击展开/收起。 */
function CollapsibleText({ text, limit = 200, className = '' }: { text: string; limit?: number; className?: string }) {
  const [open, setOpen] = useState(false)
  const long = text.length > limit
  return (
    <div>
      <div className={`whitespace-pre-wrap break-all ${className}`}>
        {long && !open ? text.slice(0, limit) + '…' : text}
      </div>
      {long && (
        <button onClick={() => setOpen(!open)}
          className="mt-1 flex items-center gap-1 text-[10px] text-cyan-400 hover:text-cyan-300">
          {open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
          {open ? '收起' : `展开（${text.length} 字符）`}
        </button>
      )}
    </div>
  )
}

export default function StepInspector({ node, onClose }: { node: TaskNode | null; onClose: () => void }) {
  if (!node) return null
  const meta = (node as any).metadata || {}
  const logs = (node as any).logs || []
  const resultText = formatResult(node.result)

  return (
    <div className="fixed right-0 top-0 w-96 h-full bg-gray-900 border-l border-gray-700 p-4 overflow-y-auto z-50 shadow-2xl">
      <div className="flex justify-between items-center mb-4">
        <h3 className="text-lg font-bold text-white">Step Details</h3>
        <button onClick={onClose} className="text-gray-400 hover:text-white text-xl" aria-label="Close">
          <X className="w-5 h-5" />
        </button>
      </div>

      {meta.agent_hatched && (
        <div className="mb-3 px-2 py-1 bg-amber-500/20 text-amber-400 text-xs rounded-full inline-block">New Species</div>
      )}

      {/* 基本信息 */}
      <div className="mb-4 space-y-3 text-sm">
        {node.step_id && (
          <div>
            <div className="text-gray-400">Step ID</div>
            <div className="text-white font-mono text-xs break-all">{node.step_id}</div>
          </div>
        )}
        <div>
          <div className="text-gray-400">Capability</div>
          <div className="text-white font-mono text-sm">{node.capability || 'goal'}</div>
        </div>
        {node.agent_id && (
          <div>
            <div className="text-gray-400">Agent</div>
            <div className="text-white font-mono text-sm">{node.agent_id}</div>
          </div>
        )}
        <div>
          <div className="text-gray-400">Status</div>
          <div className={`font-mono text-sm font-semibold ${statusColor(node.status)}`}>{node.status}</div>
        </div>
        {(node.instruction || (node.name && node.id !== 'root')) && (
          <div>
            <div className="text-gray-400">Instruction</div>
            <CollapsibleText text={node.instruction || node.name || ''}
              limit={200} className="text-gray-200 text-xs leading-relaxed" />
          </div>
        )}
      </div>

      {/* 执行结果（真实数据） */}
      {resultText && (
        <div className="mb-4">
          <div className="text-gray-400 text-sm mb-1">Result</div>
          {node.result && node.result.replanned && (
            <div className="mb-2 px-2 py-1 bg-amber-500/10 text-amber-400 text-xs rounded">
              Replanned via alternative step
            </div>
          )}
          <pre className="text-xs text-emerald-300 bg-gray-800 rounded-lg p-3 max-h-72 overflow-y-auto whitespace-pre-wrap break-all">
            {resultText.slice(0, 2000)}
          </pre>
          {resultText.length > 2000 && (
            <div className="text-[10px] text-slate-500 mt-1">结果过长，已截断前 2000 字符（完整结果见日志）</div>
          )}
        </div>
      )}

      {/* 元数据轨迹（demo/未来数据） */}
      {(meta.planner_reasoning || meta.memory_injected || meta.critic_score || node.replanHistory) && (
        <div className="mb-4">
          <div className="text-gray-400 text-sm mb-2">Decision Trace</div>
          <div className="space-y-3">
            {meta.planner_reasoning && <Bubble role="planner" text={meta.planner_reasoning} />}
            {meta.memory_injected && <Bubble role="memory" text={'Memory: ' + meta.memory_injected} />}
            {meta.critic_score && <Bubble role="critic" text={'Critic score: ' + meta.critic_score} />}
            {node.replanHistory && node.replanHistory.length > 0 && (
              <div className="text-xs text-gray-500">
                Replan attempts: {node.replanHistory.length}
              </div>
            )}
            {logs && logs.map((l: any, i: number) => <Bubble key={i} role={l.type || 'worker'} text={l.message || l} />)}
          </div>
        </div>
      )}
    </div>
  )
}

function Bubble({ role, text }: { role: string; text: string }) {
  const color = roleColors[role] || '#94a3b8'
  return (
    <div className="flex items-start gap-2">
      <div className="w-2 h-2 mt-1.5 rounded-full flex-shrink-0" style={{ background: color }} />
      <div className="text-sm text-gray-300 bg-gray-800 rounded-lg px-3 py-2 flex-1">{text.slice(0, 300)}</div>
    </div>
  )
}
