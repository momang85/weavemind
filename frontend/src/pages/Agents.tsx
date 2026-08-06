import { useState, useEffect, useRef, useMemo, useCallback } from 'react'
import {
  Search, ChevronDown, ChevronUp, Clock, CheckCircle2, XCircle, Power,
  FlaskConical, Sparkles, Cpu, Filter, Activity
} from 'lucide-react'
import AgentTopology from '../components/AgentTopology'
import { AgentInfo } from '../stores/types'
import { useTaskStore } from '../stores/useTaskStore'

const gradients = [
  'from-cyan-500 to-blue-500', 'from-emerald-500 to-teal-500',
  'from-violet-500 to-purple-500', 'from-amber-500 to-orange-500',
  'from-rose-500 to-pink-500', 'from-sky-500 to-indigo-500',
]

function avatarColor(id: string) {
  let hash = 0
  for (let i = 0; i < id.length; i++) hash = id.charCodeAt(i) + ((hash << 5) - hash)
  return gradients[Math.abs(hash) % gradients.length]
}

function initials(id: string) {
  return id.split('_').map(w => w[0]).join('').toUpperCase().slice(0, 2)
}

function timeAgo(iso: string) {
  if (!iso) return 'never'
  const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (sec < 5) return 'just now'
  if (sec < 60) return `${sec}s ago`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  return `${Math.floor(sec / 3600)}h ago`
}

const statusDot = (status: string) => {
  if (!status || status.startsWith('offline'))
    return <span className="w-2 h-2 rounded-full bg-slate-500" />
  if (status.includes('active'))
    return <span className="w-2.5 h-2.5 rounded-full bg-blue-400 animate-pulse shadow-[0_0_6px] shadow-blue-400/50" />
  return <span className="w-2 h-2 rounded-full bg-emerald-400" />
}

const statusLabel = (status: string) => {
  if (!status || status.startsWith('offline')) return '离线'
  if (status.includes('active')) return '忙碌'
  return 'Idle'
}

function AgentCard({
  agent, isAutoGen, expanded, onToggle, onKill
}: {
  agent: AgentInfo & { load?: string; avgTime?: number; successRate?: number; tasks?: any[] }
  isAutoGen: boolean
  expanded: boolean
  onToggle: () => void
  onKill: (id: string) => void
}) {
  const load = agent.load ?? agent.status?.match(/active:(\d+)/)?.[1] ?? '0'
  const maxLoad = agent.status?.match(/\/(\d+)/)?.[1] ?? '5'

  return (
    <div className={`bg-slate-900 border rounded-xl overflow-hidden transition-all duration-300 ${
      isAutoGen ? 'border-amber-500/50 ring-1 ring-amber-500/20' :
      agent.status?.startsWith('offline') ? 'border-slate-800 opacity-60' : 'border-slate-800'
    }`}>
      <div className="p-5">
        {/* Header */}
        <div className="flex items-start justify-between mb-4">
          <div className={`w-12 h-12 rounded-xl bg-gradient-to-br ${avatarColor(agent.agent_id)} flex items-center justify-center shrink-0`}>
            <span className="text-white font-bold text-sm">{initials(agent.agent_id)}</span>
          </div>
          <div className="flex flex-col items-end gap-1">
            <div className="flex items-center gap-1.5">
              {statusDot(agent.status)}
              <span className={`text-xs font-medium ${
                !agent.status || agent.status.startsWith('offline') ? 'text-slate-500' :
                agent.status.includes('active') ? 'text-blue-400' : 'text-emerald-400'
              }`}>{statusLabel(agent.status)}</span>
            </div>
            {isAutoGen && (
              <span className="flex items-center gap-1 text-[10px] font-semibold text-amber-400 bg-amber-500/10 px-2 py-0.5 rounded-full">
                <Sparkles className="w-2.5 h-2.5" /> 新物种
              </span>
            )}
          </div>
        </div>

        {/* Name */}
        <h3 className="text-slate-200 font-semibold text-sm mb-2">{agent.agent_id.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}</h3>

        {/* Capability badges */}
        <div className="flex flex-wrap gap-1.5 mb-3">
          {agent.capabilities.split(',').map(c => (
            <span key={c} className="px-2 py-0.5 bg-slate-800 rounded-md text-xs text-slate-400 font-mono">{c.trim()}</span>
          ))}
        </div>

        {/* Load + heartbeat */}
        <div className="flex items-center justify-between text-xs text-slate-500">
          <span className="flex items-center gap-1">
            <Cpu className="w-3 h-3" /> {load}/{maxLoad}
          </span>
          <span className="flex items-center gap-1">
            <Clock className="w-3 h-3" /> {timeAgo(agent.last_heartbeat)}
          </span>
        </div>

        <button
          onClick={() => onKill(agent.agent_id)}
          disabled={!agent.status || agent.status.startsWith('offline')}
          className="mt-3 w-full flex items-center justify-center gap-1.5 text-xs text-red-400/80 hover:text-red-400 py-1.5 rounded-lg hover:bg-red-500/10 transition-colors disabled:opacity-30 disabled:cursor-not-allowed">
          <Power className="w-3.5 h-3.5" /> Terminate
        </button>

        {/* Expand toggle */}
        <button onClick={onToggle}
          className="mt-3 w-full flex items-center justify-center gap-1 text-xs text-slate-600 hover:text-slate-400 py-1.5 rounded-lg hover:bg-slate-800/50 transition-colors">
          {expanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
          {expanded ? '收起详情' : '展开详情'}
        </button>
      </div>

      {/* Expanded detail panel */}
      {expanded && (
        <div className="px-5 pb-4 border-t border-slate-800 animate-fade-in">
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 mt-4 text-center">
            <div className="bg-slate-800/50 rounded-lg p-2.5">
              <div className="text-emerald-400 font-bold text-lg">{agent.successRate ?? 0}%</div>
              <div className="text-slate-500 text-[11px]">成功率</div>
            </div>
            <div className="bg-slate-800/50 rounded-lg p-2.5">
              <div className="text-cyan-400 font-bold text-lg">{agent.tasks?.length ?? 0}</div>
              <div className="text-slate-500 text-[11px]">任务数</div>
            </div>
            <div className="bg-slate-800/50 rounded-lg p-2.5">
              <div className="text-violet-400 font-bold text-lg">{agent.avgTime ?? '-'}s</div>
              <div className="text-slate-500 text-[11px]">平均耗时</div>
            </div>
          </div>

          {agent.tasks && agent.tasks.length > 0 && (
            <div className="mt-3 space-y-1 max-h-32 overflow-y-auto">
              {agent.tasks.slice(0, 5).map((t: any, i: number) => (
                <div key={i} className="flex items-center gap-2 text-xs py-1">
                  {t.status === 'SUCCESS' ? <CheckCircle2 className="w-3 h-3 text-emerald-400" /> :
                   <XCircle className="w-3 h-3 text-red-400" />}
                  <span className="text-slate-400 truncate">{t.goal?.substring(0, 40) ?? t.instruction?.substring(0, 40)}</span>
                </div>
              ))}
            </div>
          )}

          {isAutoGen && (
            <div className="mt-3 flex items-center gap-2 text-xs text-amber-400 bg-amber-500/10 rounded-lg p-2.5">
              <FlaskConical className="w-3.5 h-3.5" />
              自动孵化生成 — 由中枢在检测到能力缺失时自动创建
            </div>
          )}
        </div>
      )}
    </div>
  )
}


function LazyAgentCard(props: any) {
  const ref = useRef<HTMLDivElement>(null)
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    const el = ref.current; if (!el) return
    const obs = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) { setVisible(true); obs.disconnect() }
    }, { rootMargin: '100px' })
    obs.observe(el)
    return () => obs.disconnect()
  }, [])
  return (
    <div ref={ref} className={visible ? 'agent-card-enter-active' : 'agent-card-enter'}>
      {visible ? <AgentCard {...props} /> : <div className="bg-slate-900 border border-slate-800 rounded-xl h-48 animate-pulse" />}
    </div>
  )
}
export default function AgentsPage() {
  const [agents, setAgents] = useState<(AgentInfo & { load?: string })[]>([])
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<string | null>(null)
  const { connected } = useTaskStore()

  const fetchAgents = useCallback(async () => {
    try {
      const res = await fetch('/api/status')
      const data = await res.json()
      setAgents(data.agents ?? [])
    } catch {}
  }, [])

  useEffect(() => { fetchAgents(); const t = setInterval(fetchAgents, 2000); return () => clearInterval(t) }, [fetchAgents])

  const killAgent = useCallback(async (id: string) => {
    try {
      await fetch('/api/kill-worker', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_id: id }),
      })
      setTimeout(fetchAgents, 1500)
    } catch { /* ignore */ }
  }, [fetchAgents])

  const allCaps = useMemo(() => {
    const caps = new Set<string>()
    agents.forEach(a => a.capabilities.split(',').forEach(c => caps.add(c.trim())))
    return Array.from(caps).sort()
  }, [agents])

  const filtered = useMemo(() => {
    return agents.filter(a => {
      if (search && !a.agent_id.toLowerCase().includes(search.toLowerCase())) return false
      if (filter && !a.capabilities.includes(filter)) return false
      return true
    })
  }, [agents, search, filter])

  const toggle = (id: string) => {
    const next = new Set(expanded)
    next.has(id) ? next.delete(id) : next.add(id)
    setExpanded(next)
  }

  const autoGenIds = new Set<string>()

  return (
    <div className="max-w-6xl mx-auto space-y-5">
      {/* Topology */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
        <h2 className="flex items-center gap-2 text-sm text-slate-300 font-medium mb-3">
          <Activity className="w-4 h-4 text-cyan-400" /> Agent Topology
        </h2>
        <AgentTopology />
      </div>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-3">
        {[
          { label: '总计', value: agents.length, color: 'text-cyan-400' },
          { label: '在线', value: agents.filter(a => !a.status?.startsWith('offline')).length, color: 'text-emerald-400' },
          { label: '忙碌', value: agents.filter(a => a.status?.includes('active')).length, color: 'text-blue-400' },
          { label: '离线', value: agents.filter(a => !a.status || a.status.startsWith('offline')).length, color: 'text-slate-500' },
        ].map(s => (
          <div key={s.label} className="bg-slate-900 border border-slate-800 rounded-xl p-4 text-center">
            <div className={`text-2xl font-bold ${s.color}`}>{s.value}</div>
            <div className="text-slate-500 text-xs mt-1">{s.label}</div>
          </div>
        ))}
      </div>

      {/* Search + Filter */}
      <div className="flex gap-3">
        <div className="flex-1 relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-500" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search agents..."
            className="w-full bg-slate-900 border border-slate-800 rounded-lg pl-10 pr-4 py-2.5 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-cyan-500"
          />
        </div>
        {allCaps.map(cap => (
          <button key={cap} onClick={() => setFilter(filter === cap ? null : cap)}
            className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-mono transition-colors ${
              filter === cap ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/30' :
              'bg-slate-900 border border-slate-800 text-slate-500 hover:text-slate-300'
            }`}>
            <Filter className="w-3 h-3" /> {cap}
          </button>
        ))}
      </div>

      {/* Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {filtered.map(a => (
          <LazyAgentCard
            key={a.agent_id}
            agent={a}
            isAutoGen={autoGenIds.has(a.agent_id)}
            expanded={expanded.has(a.agent_id)}
            onToggle={() => toggle(a.agent_id)}
            onKill={killAgent}
          />
        ))}
      </div>

      {filtered.length === 0 && (
        <div className="text-center text-slate-600 py-12">无匹配智能体</div>
      )}

      {!connected && (
        <div className="text-center text-red-400 text-sm py-4 bg-red-500/10 rounded-xl">
          后端断连 — 每2秒轮询
        </div>
      )}
    </div>
  )
}
