import { memo, useEffect, useRef, useState } from 'react'

import { useTaskStore } from '../stores/useTaskStore'
import { LogEntry } from '../stores/types'
import { Pause, Play, Search, FileText, Code2, PackageCheck, Brain, Eye } from 'lucide-react'


const agentIcons: Record<string, typeof Brain> = {
  orchestrator: Brain, critic: Eye,
  search_agent: Search, content_summarizer: FileText,
  code_executor: Code2, file_io_worker: FileText, packaging_worker: PackageCheck,
}

const typeCfg: Partial<Record<LogEntry['type'], { icon: string; border: string; bg: string }>> = {
  plan:     { icon: '🧠', border: 'border-l-blue-400',   bg: 'bg-blue-500/5' },
  review:   { icon: '👁',  border: 'border-l-purple-400', bg: 'bg-purple-500/5' },
  dispatch: { icon: '📤', border: 'border-l-cyan-400',   bg: 'bg-cyan-500/5' },
  memory:   { icon: '💾', border: 'border-l-amber-400',  bg: 'bg-amber-500/5' },
  error:    { icon: '❌', border: 'border-l-red-400',    bg: 'bg-red-500/10' },
  info:     { icon: 'ℹ',  border: 'border-l-slate-400',  bg: 'bg-slate-500/5' },
}


const LogRow = memo(function LogRow({ entry }: { entry: LogEntry }) {

    const cfg = typeCfg[entry.type] ?? typeCfg.info!
  const IconComp = entry.agent ? agentIcons[entry.agent] : null

  return (
    <div className={`flex items-start gap-2 px-3 h-full border-l-2 ${cfg.border} ${cfg.bg}`}>
        <span className="text-[10px] text-slate-600 font-mono shrink-0 w-14 mt-0.5 tabular-nums">
          {entry.timestamp.slice(0, 8)}
        </span>
        {IconComp ? (
          <IconComp className="w-3.5 h-3.5 text-slate-400 shrink-0 mt-0.5" />
        ) : (
          <span className="shrink-0 text-xs mt-0.5">{cfg.icon}</span>
        )}
        <span className="text-xs text-slate-400 truncate mt-0.5">
          {entry.agent && <span className="text-[10px] font-semibold text-slate-500">{entry.agent}: </span>}
          {entry.message}
        </span>
    </div>
  )
})

export default memo(function LiveActivity() {
  const { logs, agents, status } = useTaskStore()

  useEffect(() => {
    console.log("[LiveActivity] mounted, agents:", agents.length, "logs:", logs.length, "status:", status)
  }, [])

  const [paused, setPaused] = useState(false)
  const outerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!paused && outerRef.current) {
      const el = outerRef.current.querySelector('[data-virtual-list]') as HTMLElement | null
      if (el) {
        el.scrollTop = el.scrollHeight
      }
    }
  }, [logs, paused])

  const online = agents.filter(a => !a.status?.startsWith('offline')).length
  const busy = agents.filter(a => a.status?.includes('active')).length

  return (
    <div className="flex flex-col h-full space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3 text-xs">
          <span className="text-slate-500"><span className="text-emerald-400 font-semibold">{online}</span> online</span>
          <span className="text-slate-600">·</span>
          <span className="text-slate-500"><span className="text-amber-400 font-semibold">{busy}</span> busy</span>
        </div>
        <button onClick={() => setPaused(!paused)}
          className={`p-1.5 rounded-lg transition-transform duration-200 active:scale-95 ${
            paused ? 'bg-cyan-500/20 text-cyan-400' : 'bg-slate-800 text-slate-500 hover:text-slate-300'
          }`}
          title={paused ? 'Resume' : 'Pause'}>
          {paused ? <Play className="w-3.5 h-3.5" /> : <Pause className="w-3.5 h-3.5" />}
        </button>
      </div>

      {/* Agent pills */}
      <div className="flex flex-wrap gap-1.5 shrink-0">
        {agents.map(a => {
          const Icon = agentIcons[a.agent_id] || Brain
          return (
            <div key={a.agent_id}
              className={`flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] border ${
                a.status?.startsWith('offline') ? 'border-red-500/20 bg-red-500/5 text-red-400' :
                a.status?.includes('active') ? 'border-amber-500/20 bg-amber-500/5 text-amber-400' :
                'border-emerald-500/20 bg-emerald-500/5 text-emerald-400'
              }`}>
              <Icon className="w-3 h-3" />
              <span className="truncate max-w-[72px]">{a.agent_id.replace(/_/g, ' ')}</span>
            </div>
          )
        })}
      </div>

      {/* Virtual list */}
      <div ref={outerRef} className="flex-1 min-h-0 bg-slate-800/30 rounded-lg border border-slate-800 overflow-hidden">
        {logs.length === 0 ? (
          <div className="flex items-center justify-center h-64 text-slate-600 text-xs">等待事件中...</div>
        ) : (
          <div className="space-y-px">
            {logs.map((entry, i) => (
              <LogRow key={entry.id || i} entry={entry} />
            ))}
          </div>
        )}
      </div>

      {paused && (
        <div className="shrink-0 flex items-center gap-2 text-[11px] text-amber-400 bg-amber-500/10 rounded-lg px-3 py-1.5">
          <Pause className="w-3 h-3" /> Paused
        </div>
      )}
    </div>
  )
})
