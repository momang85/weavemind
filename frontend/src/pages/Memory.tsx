import { useState, useEffect, useCallback } from 'react'
import { Brain, FlaskConical, ChevronDown, ChevronRight, Play, CheckCircle2, XCircle, ShieldCheck, Sparkles, Copy, RefreshCw } from 'lucide-react'
import type { MemoryDoc, EvolutionRound } from '../stores/types'

function chip(text: string, cls: string) {
  return <span className={`px-2 py-0.5 rounded-full text-[10px] font-semibold ${cls}`}>{text}</span>
}

export default function Memory() {
  const [convs, setConvs] = useState<MemoryDoc[]>([])
  const [strats, setStrats] = useState<MemoryDoc[]>([])
  const [stats, setStats] = useState({ conversations: 0, strategies: 0 })
  const [rounds, setRounds] = useState<EvolutionRound[]>([])
  const [pendingList, setPendingList] = useState<any[]>([])
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [summary, setSummary] = useState('')
  const [summaryLoading, setSummaryLoading] = useState(false)

  const load = useCallback(async () => {
    try {
      const mem = await (await fetch('/api/memory')).json()
      setConvs(mem.conversations ?? [])
      setStrats(mem.strategies ?? [])
      setStats(mem.stats ?? { conversations: 0, strategies: 0 })
    } catch {}
    try {
      const evo = await (await fetch('/api/evolution')).json()
      setRounds(evo.rounds ?? [])
    } catch {}
    try {
      const pend = await (await fetch('/api/evolution/pending')).json()
      setPendingList(pend.pending ?? [])
    } catch {}
    setLoading(false)
  }, [])

  useEffect(() => { load() }, [load])

  const loadSummary = useCallback(async (refresh = false) => {
    setSummaryLoading(true)
    try {
      const res = await fetch('/api/memory/summary' + (refresh ? '?refresh=1' : ''))
      const d = await res.json()
      setSummary(d.summary || '')
    } catch {}
    setSummaryLoading(false)
  }, [])

  useEffect(() => { loadSummary() }, [loadSummary])

  const copySummary = async () => {
    if (!summary) return
    try {
      await navigator.clipboard.writeText(summary)
      alert('已复制')
    } catch {}
  }

  const toggle = (id: string) => setExpanded(prev => ({ ...prev, [id]: !prev[id] }))

  const triggerEvolution = async () => {
    try {
      await fetch('/api/evolution/trigger', { method: 'POST' })
      alert('进化已触发，约需数分钟完成，稍后刷新查看回放。')
    } catch {}
  }

  const approveStrategy = async (id: string, approve: boolean) => {
    try {
      await fetch('/api/evolution/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy_id: id, approve }),
      })
      await load()
    } catch {}
  }

  const del = async (type: 'conversations' | 'strategies', ids: string[]) => {
    if (!ids.length) return
    try {
      await fetch('/api/memory/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, ids }),
      })
      await load()
    } catch {}
  }

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-slate-200 font-semibold text-lg">记忆与进化</h2>
        <button onClick={load} className="text-xs text-cyan-400 hover:text-cyan-300">刷新</button>
      </div>

      {/* 系统自述 */}
      <div className="bg-gradient-to-br from-violet-500/10 via-slate-900 to-cyan-500/10 border border-violet-500/20 rounded-xl p-5">
        <div className="flex items-center gap-2 mb-3">
          <Sparkles className="w-5 h-5 text-violet-400" />
          <h3 className="text-slate-200 font-semibold text-sm">系统自述 · 它眼中的自己</h3>
          <div className="ml-auto flex gap-2">
            <button onClick={() => loadSummary(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-violet-500/10 hover:bg-violet-500/20 text-violet-400 text-xs">
              <RefreshCw className="w-3 h-3" /> {summaryLoading ? '生成中...' : '重新生成'}
            </button>
            <button onClick={copySummary}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs">
              <Copy className="w-3 h-3" /> 复制
            </button>
          </div>
        </div>
        <p className="text-slate-300 text-sm leading-relaxed whitespace-pre-wrap">
          {summaryLoading && !summary ? '正在让 LLM 阅读记忆库并生成自述...' : (summary || '暂无自述，点击「重新生成」。')}
        </p>
        <p className="text-[11px] text-slate-600 mt-2">基于真实记忆数据生成，可直接用于发布到社交媒体。</p>
      </div>

      {/* 记忆库 */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
        <div className="flex items-center gap-2 mb-4">
          <Brain className="w-5 h-5 text-amber-400" />
          <h3 className="text-slate-200 font-semibold text-sm">记忆库 · 它记得什么</h3>
          <div className="ml-auto flex gap-2">
            {chip(`${stats.conversations} 对话`, 'bg-amber-500/15 text-amber-400')}
            {chip(`${stats.strategies} 策略`, 'bg-violet-500/15 text-violet-400')}
          </div>
        </div>

        {loading ? <div className="text-slate-600 text-xs py-6">加载中...</div> : (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
            <div>
              <div className="text-xs text-slate-500 mb-2">最近对话（任务目标）</div>
              <div className="space-y-2 max-h-96 overflow-y-auto pr-1">
                {convs.length === 0 && <div className="text-slate-600 text-xs">暂无</div>}
                {convs.map((c, i) => (
                  <div key={i} className="bg-slate-800/40 border border-slate-800 rounded-lg p-3">
                    <div className="flex items-center gap-2">
                      <span className="text-amber-400/80 text-[10px]">目标</span>
                      <span className="text-slate-300 text-xs truncate flex-1">{c.metadata.goal || (c.content || '').slice(0, 60)}</span>
                      {c.metadata.timestamp && <span className="text-slate-600 text-[10px]">{String(c.metadata.timestamp).slice(0, 16)}</span>}
                      {c.id && (
                        <button onClick={() => del('conversations', [c.id!])}
                          className="text-slate-600 hover:text-red-400 text-[10px] px-1">删除</button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <div className="text-xs text-slate-500 mb-2">成功策略（可复用的解决路径）</div>
              <div className="space-y-2 max-h-96 overflow-y-auto pr-1">
                {strats.length === 0 && <div className="text-slate-600 text-xs">暂无</div>}
                {strats.map((s, i) => (
                  <div key={i} className="bg-slate-800/40 border border-slate-800 rounded-lg overflow-hidden">
                    <button onClick={() => toggle('s' + i)}
                      className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-slate-800/60">
                      {expanded['s' + i] ? <ChevronDown className="w-3.5 h-3.5 text-slate-500" /> : <ChevronRight className="w-3.5 h-3.5 text-slate-500" />}
                      <span className="text-violet-300 text-xs truncate flex-1">{String(s.metadata.goal_keywords || '').slice(0, 60) || '策略'}</span>
                      {s.metadata.step_count && chip(`${s.metadata.step_count} 步`, 'bg-slate-700/50 text-slate-400')}
                      {s.id && (
                        <button onClick={() => del('strategies', [s.id!])}
                          className="text-slate-600 hover:text-red-400 text-[10px] px-1">删除</button>
                      )}
                    </button>
                    {expanded['s' + i] && (
                      <pre className="px-3 pb-3 text-slate-400 text-xs whitespace-pre-wrap font-sans max-h-48 overflow-y-auto">
                        {s.content}
                      </pre>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 进化部署审批 */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
        <div className="flex items-center gap-2 mb-4">
          <ShieldCheck className="w-5 h-5 text-cyan-400" />
          <h3 className="text-slate-200 font-semibold text-sm">进化部署审批</h3>
          <span className="text-slate-500 text-[10px]">人工确认后，胜出策略将实际作用于对应 Worker</span>
        </div>
        {pendingList.length === 0 ? (
          <div className="text-slate-600 text-xs py-4 text-center">
            暂无待审批的策略部署请求。触发进化并产生稳定胜者后，会出现在这里等你审批。
          </div>
        ) : (
          <div className="space-y-3">
            {pendingList.map((p, i) => (
              <div key={i} className="bg-slate-800/40 border border-slate-800 rounded-lg p-4">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-cyan-400 text-xs font-semibold">{p.strategy_id}</span>
                  {chip(p.agent_type || 'search_agent', 'bg-slate-700/50 text-slate-400')}
                  {p.temperature != null && chip('温度 ' + p.temperature, 'bg-slate-700/50 text-slate-400')}
                  {p.max_sources != null && chip('源数 ' + p.max_sources, 'bg-slate-700/50 text-slate-400')}
                  {p.timestamp && <span className="text-slate-600 text-[10px] ml-auto">{new Date(p.timestamp).toLocaleString()}</span>}
                </div>
                <div className="mt-2 text-xs text-slate-400">
                  摘要提示：<span className="text-slate-300">{p.summarization_prompt || '—'}</span>
                </div>
                {(p.filter_rules || []).length > 0 && (
                  <div className="mt-1 text-xs text-slate-400">
                    过滤规则：<span className="text-slate-300">{String(p.filter_rules.join('；'))}</span>
                  </div>
                )}
                <div className="mt-3 flex gap-2">
                  <button onClick={() => approveStrategy(p.strategy_id, true)}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 text-xs">
                    <CheckCircle2 className="w-3.5 h-3.5" /> 批准部署
                  </button>
                  <button onClick={() => approveStrategy(p.strategy_id, false)}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs">
                    <XCircle className="w-3.5 h-3.5" /> 驳回
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 进化锦标赛回放 */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
        <div className="flex items-center gap-2 mb-4">
          <FlaskConical className="w-5 h-5 text-violet-400" />
          <h3 className="text-slate-200 font-semibold text-sm">进化锦标赛回放</h3>
          <button onClick={triggerEvolution}
            className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-violet-500/10 hover:bg-violet-500/20 text-violet-400 text-xs">
            <Play className="w-3 h-3" /> 触发新一轮进化
          </button>
        </div>

        {rounds.length === 0 ? (
          <div className="text-slate-600 text-xs py-6 text-center">
            暂无进化记录。点击「触发新一轮进化」开始；进化在后台运行约 3-5 分钟。
          </div>
        ) : (
          <div className="space-y-3">
            {rounds.map((r, i) => (
              <div key={i} className="bg-slate-800/40 border border-slate-800 rounded-lg overflow-hidden">
                <button onClick={() => toggle('r' + i)}
                  className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-slate-800/60">
                  <ChevronDown className="w-4 h-4 text-slate-500 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="text-slate-300 text-xs font-medium truncate">{r.summary || '进化轮次'}</div>
                    {r.timestamp && <div className="text-slate-600 text-[10px]">{new Date(r.timestamp).toLocaleString()}</div>}
                  </div>
                  {r.stable ? chip('稳定', 'bg-emerald-500/15 text-emerald-400') : chip('不稳定', 'bg-amber-500/15 text-amber-400')}
                  {r.deployed ? chip('已部署', 'bg-cyan-500/15 text-cyan-400') : chip('未部署', 'bg-slate-700/50 text-slate-400')}
                  {r.winner && chip('胜者 ' + String(r.winner.strategy_id || ''), 'bg-violet-500/15 text-violet-400')}
                </button>

                {expanded['r' + i] && (
                  <div className="px-4 pb-4 border-t border-slate-800">
                    {(r.scoreboard && Object.keys(r.scoreboard).length > 0) && (
                      <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-2">
                        {Object.entries(r.scoreboard).map(([k, v]) => (
                          <div key={k} className="bg-slate-800/60 rounded-lg p-2 text-center">
                            <div className="text-cyan-400 font-bold text-sm">{v}</div>
                            <div className="text-slate-500 text-[10px] truncate">{k}</div>
                          </div>
                        ))}
                      </div>
                    )}
                    <div className="mt-3 space-y-1.5">
                      {(r.rankings || []).map((rk, j) => (
                        <div key={j} className="flex items-center gap-2 text-xs bg-slate-800/40 rounded-lg px-3 py-1.5">
                          {rk.winner ? <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" /> : <XCircle className="w-3.5 h-3.5 text-slate-600 shrink-0" />}
                          <span className="text-slate-400 truncate flex-1">{(rk.task || '').slice(0, 60)}</span>
                          <span className="text-violet-400 shrink-0">{(rk.ranking || []).slice(0, 3).join(' → ') || rk.winner}</span>
                          {rk.scores && Object.keys(rk.scores).length > 0 && (
                            <span className="text-slate-600 shrink-0">{JSON.stringify(rk.scores)}</span>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
