import { useState, useEffect, useCallback } from 'react'
import { Clock, TrendingUp, Shield, Activity, Play, Heart, Skull, FlaskConical, Plus, Cpu, RefreshCw } from 'lucide-react'
import { useTaskStore } from '../stores/useTaskStore'

interface HealthEvent {
  id: string; timestamp: string
  type: 'scale' | 'crash' | 'recovery' | 'evolution' | 'guardian'
  agent?: string; message: string
}

const DEMO_EVENTS: HealthEvent[] = [
  { id:'e1',timestamp:'14:32',type:'scale',message:'AutoScaler added code_executor',agent:'code_executor' },
  { id:'e2',timestamp:'14:35',type:'crash',message:'search_agent no response (30s)',agent:'search_agent' },
  { id:'e3',timestamp:'14:35',type:'guardian',message:'Guardian detected crash, restarting',agent:'search_agent' },
  { id:'e4',timestamp:'14:35',type:'recovery',message:'search_agent recovered',agent:'search_agent' },
  { id:'e5',timestamp:'14:40',type:'scale',message:'Queue > 5, scaling file_io_worker',agent:'file_io_worker' },
  { id:'e6',timestamp:'15:00',type:'evolution',message:'Evolution round 12 complete' },
]

const typeConf: Record<string,{icon:typeof Plus;color:string;bg:string;border:string}> = {
  scale:     {icon:Plus,color:'text-cyan-400',bg:'bg-cyan-500/5',border:'border-l-cyan-400'},
  crash:     {icon:Skull,color:'text-red-400',bg:'bg-red-500/5',border:'border-l-red-400'},
  recovery:  {icon:Heart,color:'text-emerald-400',bg:'bg-emerald-500/5',border:'border-l-emerald-400'},
  guardian:  {icon:Shield,color:'text-amber-400',bg:'bg-amber-500/5',border:'border-l-amber-400'},
  evolution: {icon:FlaskConical,color:'text-violet-400',bg:'bg-violet-500/5',border:'border-l-violet-400'},
}

function formatUptime(sec: number): string {
  if (!sec || sec < 0) return '-'
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

export default function HealthPage() {
  const { agents, connected, systemStatus } = useTaskStore()
  const [events, setEvents] = useState<HealthEvent[]>(DEMO_EVENTS)
  const [live, setLive] = useState(false)

  const loadEvents = useCallback(async () => {
    try {
      const res = await fetch('/api/events')
      const data = await res.json()
      const list = (data.events ?? []) as HealthEvent[]
      if (list.length > 0) {
        setEvents(list)
        setLive(true)
      }
    } catch { /* 后端不可达时保留演示数据 */ }
  }, [])

  useEffect(() => {
    loadEvents()
    const t = setInterval(loadEvents, 5000)
    return () => clearInterval(t)
  }, [loadEvents])

  const online = agents.filter(a => !a.status?.startsWith('offline')).length
  const survival = systemStatus?.survival_rate ?? (agents.length > 0 ? Math.round(online / agents.length * 100) : 100)
  const tasksToday = systemStatus?.tasks?.today ?? 0
  const uptime = systemStatus?.uptime_sec ?? 0
  const usage = systemStatus?.llm_usage
  const totalTokens = (usage?.prompt_tokens ?? 0) + (usage?.completion_tokens ?? 0)
  const tokenLabel = totalTokens >= 1000 ? `${(totalTokens / 1000).toFixed(1)}k` : String(totalTokens)

  const triggerEvolution = async () => {
    const id = 'man-' + Date.now()
    setEvents(prev => [{ id, timestamp: new Date().toLocaleTimeString(), type: 'evolution', message: 'Triggering evolution...' }, ...prev])
    try {
      const res = await fetch('/api/evolution/trigger', { method: 'POST' })
      const data = await res.json()
      setEvents(prev => prev.map(e => e.id === id
        ? { ...e, message: data.status === 'triggered' ? 'Evolution started' : 'Failed: ' + (data.message || '') }
        : e))
    } catch {
      setEvents(prev => prev.map(e => e.id === id ? { ...e, message: 'Failed - backend unreachable' } : e))
    }
  }

  const lastEvolution = events.find(e => e.type === 'evolution' && !e.message.startsWith('Triggering'))
  const guardianOk = connected && events.some(e => e.type === 'guardian' || e.type === 'recovery' || e.type === 'crash')

  return (
    <div className="max-w-5xl mx-auto space-y-5">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { icon: Clock, label: 'Uptime', value: formatUptime(uptime), color: 'text-cyan-400' },
          { icon: TrendingUp, label: 'Tasks Today', value: String(tasksToday), color: 'text-emerald-400' },
          { icon: Shield, label: 'Survival Rate', value: `${survival}%`, color: 'text-violet-400' },
          { icon: Cpu, label: 'LLM Tokens', value: tokenLabel, color: 'text-amber-400' },
        ].map(m => (
          <div key={m.label} className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <div className="flex items-center gap-2 text-slate-500 text-xs mb-3"><m.icon className="w-4 h-4" />{m.label}</div>
            <div className={`text-3xl font-bold ${m.color}`}>{m.value}</div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-5 gap-5">
        <div className="col-span-3 bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
          <div className="flex items-center justify-between px-5 py-3 border-b border-slate-800">
            <div className="flex items-center gap-2 text-sm text-slate-300 font-medium"><Activity className="w-4 h-4 text-cyan-400" />Event Timeline</div>
            <span className="text-xs text-slate-600">{live ? 'Live' : 'Demo'}</span>
          </div>
          <div className="divide-y divide-slate-800/50 max-h-[500px] overflow-y-auto">
            {events.map(e => {
              const cfg = typeConf[e.type] ?? typeConf.guardian
              const Icon = cfg.icon
              return (
                <div key={e.id} className={`flex items-start gap-3 px-4 py-3 border-l-2 ${cfg.border} ${cfg.bg}`}>
                  <Icon className={`w-4 h-4 ${cfg.color} shrink-0 mt-0.5`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-mono text-slate-600">{e.timestamp}</span>
                      {e.agent && <span className="text-xs text-slate-500 bg-slate-800 px-1.5 py-0.5 rounded">{e.agent}</span>}
                      <span className="text-xs uppercase text-slate-600">{e.type}</span>
                    </div>
                    <p className="text-sm text-slate-400 mt-0.5">{e.message}</p>
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        <div className="col-span-2 space-y-5">
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <h3 className="flex items-center gap-2 text-sm text-slate-300 font-medium mb-4"><Heart className="w-4 h-4 text-rose-400" />Health Checks</h3>
            {[
              { label: 'Orchestrator', ok: connected, icon: Cpu },
              { label: 'Worker Pool', ok: agents.length > 0, icon: Shield },
              { label: 'Guardian', ok: guardianOk, icon: RefreshCw },
            ].map(c => (
              <div key={c.label} className="flex items-center justify-between py-2.5 border-b border-slate-800/50 last:border-0">
                <div className="flex items-center gap-2 text-sm"><c.icon className={`w-4 h-4 ${c.ok ? 'text-emerald-400' : 'text-red-400'}`} /><span className="text-slate-400">{c.label}</span></div>
                <span className={`text-xs font-medium ${c.ok ? 'text-emerald-400' : 'text-red-400'}`}>{c.ok ? 'OK' : 'DOWN'}</span>
              </div>
            ))}
          </div>

          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <h3 className="flex items-center gap-2 text-sm text-slate-300 font-medium mb-4"><FlaskConical className="w-4 h-4 text-violet-400" />Evolution Sandbox</h3>
            <div className="space-y-3 text-sm">
              {[['Last run', lastEvolution ? String(lastEvolution.timestamp).slice(0, 19) : '-'], ['Status', lastEvolution ? 'Completed' : 'Idle']].map(([k, v]) => (
                <div key={k} className="flex justify-between"><span className="text-slate-500">{k}</span><span className="text-slate-300">{v}</span></div>
              ))}
            </div>
            <button onClick={triggerEvolution}
              className="mt-4 w-full flex items-center justify-center gap-2 py-2.5 bg-violet-500/10 hover:bg-violet-500/20 text-violet-400 rounded-lg text-sm font-medium transition-colors border border-violet-500/20">
              <Play className="w-3.5 h-3.5" />Trigger Evolution
            </button>
            <p className="text-xs text-slate-600 mt-2 text-center">Admin confirmation required</p>
          </div>
        </div>
      </div>
    </div>
  )
}
