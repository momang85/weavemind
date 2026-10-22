import { useState, useEffect, useCallback } from 'react'
import { Brain, FlaskConical, ChevronDown, ChevronRight, Play, CheckCircle2, XCircle, ShieldCheck, Sparkles, Copy, RefreshCw, Activity } from 'lucide-react'
import type { MemoryDoc, EvolutionRound } from '../stores/types'
import { useTaskStore } from '../stores/useTaskStore'
import { apiOutcome, readApiOutcome } from '../lib/apiResult'
import { formatLocalTimeLabel, formatLocalTimeShort } from '../lib/timeText'
import { auditMemorySummary } from '../lib/memorySummary'
import { localizeEvolutionSummary, scoreMapText } from '../lib/evolutionText'

interface MemoryHealth {
  injections: number
  hits: number
  hit_rate: number
  strategy_count: number
  conversation_count: number
  expired_purged: number
  retrieval_mode?: 'vector' | 'literal' | 'unavailable'
  degraded_queries?: number
  degraded_hits?: number
  degraded_hit_rate?: number
  pending_writes?: number
}

const EMPTY_HEALTH: MemoryHealth = {
  injections: 0,
  hits: 0,
  hit_rate: 0,
  strategy_count: 0,
  conversation_count: 0,
  expired_purged: 0,
  retrieval_mode: 'unavailable',
  degraded_queries: 0,
  degraded_hits: 0,
  degraded_hit_rate: 0,
  pending_writes: 0,
}

function chip(text: string, cls: string) {
  return <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${cls}`}>{text}</span>
}

export default function Memory() {
  // 演示模式：本页的删除/审批/触发进化都会真实改数据或烧 token，逐项停用；
  // fetch 层还有统一兜底拦截（lib/demoGuard.ts），两者都要在，避免"看起来能点"。
  const demoMode = useTaskStore(s => s.demoMode)
  const [convs, setConvs] = useState<MemoryDoc[]>([])
  const [strats, setStrats] = useState<MemoryDoc[]>([])
  const [stats, setStats] = useState({ conversations: 0, strategies: 0 })
  const [health, setHealth] = useState<MemoryHealth>(EMPTY_HEALTH)
  const [rounds, setRounds] = useState<EvolutionRound[]>([])
  const [pendingList, setPendingList] = useState<any[]>([])
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [summary, setSummary] = useState('')
  const [summaryError, setSummaryError] = useState('')
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [notice, setNotice] = useState('')
  // 提示语气：拦截/失败用 warn（琥珀），成功用 info（青）——避免"被拦了还显示成功样式"
  const [noticeTone, setNoticeTone] = useState<'info' | 'warn'>('info')
  const [busyAction, setBusyAction] = useState('')

  const load = useCallback(async () => {
    try {
      const mem = await (await fetch('/api/memory')).json()
      setConvs(mem.conversations ?? [])
      setStrats(mem.strategies ?? [])
      setStats(mem.stats ?? { conversations: 0, strategies: 0 })
      setHealth(mem.memory_health ?? EMPTY_HEALTH)
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
      const payload = await res.json().catch(() => null)
      const out = apiOutcome(res.status, payload, '', '摘要加载失败')
      if (!out.ok) {
        // 拦截/失败都不得当成"摘要为空"：演示模式下该 GET 会调付费模型，已被守卫拦下
        setSummary('')
        setSummaryError(out.message)
      } else {
        setSummary(payload?.summary || '')
        if (payload?.memory_health) setHealth(payload.memory_health)
        setSummaryError('')
      }
    } catch (e: any) {
      setSummary('')
      setSummaryError('摘要加载失败：' + (e?.message || '网络错误'))
    }
    setSummaryLoading(false)
  }, [])

  useEffect(() => { loadSummary() }, [loadSummary])

  const copySummary = async () => {
    if (!summary) return
    try {
      await navigator.clipboard.writeText(summary)
      setNotice('已复制')
    } catch {
      setNotice('复制失败')
    }
  }

  const toggle = (id: string) => setExpanded(prev => ({ ...prev, [id]: !prev[id] }))

  const triggerEvolution = async () => {
    if (busyAction) return
    setBusyAction('evolution')
    try {
      const res = await fetch('/api/evolution/trigger', { method: 'POST' })
      const out = await readApiOutcome(res, '进化已触发，约需数分钟完成，稍后刷新查看回放。', '触发失败')
      setNotice(out.message)
      if (out.blocked) setNoticeTone('warn')
    } catch {
      setNotice('触发失败：网络错误或后端不可达')
      setNoticeTone('warn')
    } finally {
      setBusyAction('')
    }
  }

  const approveStrategy = async (id: string, approve: boolean) => {
    if (busyAction) return
    if (!window.confirm(`确认${approve ? '批准' : '驳回'}该进化策略？`)) return
    setBusyAction(id)
    try {
      const res = await fetch('/api/evolution/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy_id: id, approve }),
      })
      const out = await readApiOutcome(res, approve ? '策略已批准' : '策略已驳回', '操作失败')
      setNotice(out.message)
      if (out.blocked) setNoticeTone('warn')
      if (out.ok) await load()
    } catch {
      setNotice('操作失败：网络错误或后端不可达')
      setNoticeTone('warn')
    } finally {
      setBusyAction('')
    }
  }

  const del = async (type: 'conversations' | 'strategies', ids: string[]) => {
    if (!ids.length || busyAction) return
    if (!window.confirm(`确认删除 ${ids.length} 条${type === 'conversations' ? '记忆' : '策略'}？此操作不可撤销。`)) return
    setBusyAction('del-' + type)
    try {
      const res = await fetch('/api/memory/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, ids }),
      })
      const out = await readApiOutcome(res, '删除完成', '删除失败')
      setNotice(out.message)
      if (out.blocked) setNoticeTone('warn')
      if (out.ok) await load()
    } catch {
      setNotice('删除失败：网络错误或后端不可达')
      setNoticeTone('warn')
    } finally {
      setBusyAction('')
    }
  }

  // 降级口径：embedding 不可用时命中率改按关键词兜底统计，避免误读成"没有经验"
  const mode: 'vector' | 'literal' | 'unavailable' = health.retrieval_mode ?? 'unavailable'

  // S3：自述由 LLM 生成、数字措辞不稳定（实机把 464 条对话写成「464 项任务」）。
  // 这里把自述里的数字主张与本页**实取**口径逐个对照，把结论写在自述下方；
  // 不修改自述正文（那是模型输出，前端改写等于篡改来源）。
  const summaryAudit = auditMemorySummary(summary, {
    conversations: stats.conversations || health.conversation_count || 0,
    strategies: stats.strategies || health.strategy_count || 0,
  })

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-ink font-semibold text-lg">记忆与进化</h2>
        {notice && (
          <div className={`text-xs rounded-lg px-3 py-2 border ${
            noticeTone === 'warn'
              ? 'text-amber-700 bg-amber-500/10 border-amber-500/30'
              : 'text-brand bg-brand/10 border-brand/20'
          }`}>{notice}</div>
        )}
        <button onClick={load} aria-label="刷新记忆数据" className="text-xs text-brand hover:text-brand">刷新</button>
      </div>

      {/* 记忆健康度（P2 可观测：命中率 / 集合规模 / 过期清理） */}
      <div className="bg-surface border border-line rounded-xl p-5">
        <div className="flex items-center gap-2 mb-4">
          <Activity className="w-5 h-5 text-emerald-700" />
          <h3 className="text-ink font-semibold text-sm">记忆健康度</h3>
          <span className="text-ink-faint text-xs">
            {mode === 'literal'
              ? '向量检索不可用（Embedding 欠费/故障）：当前按关键词兜底检索，命中率分母是兜底检索次数'
              : '命中率 = 检索到相关记忆的注入次数 / 注入总次数（进程内统计）'}
          </span>
        </div>
        {mode === 'literal' && (
          <div className="mb-3 text-xs text-amber-700 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
            Embedding 接口不可用，历史经验的语义检索已降级为关键词匹配：仍能复用经验，但召回不如向量检索。
            {(health.pending_writes ?? 0) > 0 && ` 另有 ${health.pending_writes} 条新经验排队待补录，接口恢复后会自动写入。`}
          </div>
        )}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
          <div className="bg-surface-raised/40 border border-line rounded-lg p-3">
            <div className={`font-bold text-lg ${mode === 'literal' ? 'text-amber-700' : 'text-emerald-700'}`}>
              {mode === 'unavailable'
                ? '不可用'
                : `${((mode === 'literal' ? (health.degraded_hit_rate ?? 0) : health.hit_rate) * 100).toFixed(1)}%`}
            </div>
            <div className="text-ink-faint text-xs mt-0.5">
              {mode === 'literal' ? `关键词命中率（命中 ${health.degraded_hits ?? 0}）` : '记忆命中率'}
            </div>
          </div>
          <div className="bg-surface-raised/40 border border-line rounded-lg p-3">
            <div className="text-brand font-bold text-lg">{health.injections}</div>
            <div className="text-ink-faint text-xs mt-0.5">注入次数（命中 {health.hits}）</div>
          </div>
          <div className="bg-surface-raised/40 border border-line rounded-lg p-3">
            <div className="text-violet-700 font-bold text-lg">{health.strategy_count}</div>
            <div className="text-ink-faint text-xs mt-0.5">策略条数</div>
            <div className="text-ink-faint text-xs mt-0.5">记忆库策略集合条数（/api/memory），与下方「策略」chip 同源</div>
          </div>
          <div className="bg-surface-raised/40 border border-line rounded-lg p-3">
            <div className="text-amber-700 font-bold text-lg">{health.conversation_count}</div>
            <div className="text-ink-faint text-xs mt-0.5">对话条数</div>
            <div className="text-ink-faint text-xs mt-0.5">记忆库对话集合条数（/api/memory）；不是任务数——任务总数见「指标看板」</div>
          </div>
          <div className="bg-surface-raised/40 border border-line rounded-lg p-3">
            <div className="text-ink font-bold text-lg">{health.expired_purged}</div>
            <div className="text-ink-faint text-xs mt-0.5">过期策略已清理</div>
          </div>
        </div>
      </div>

      {/* 系统自述 */}
      <div className="bg-gradient-to-br from-violet-500/10 via-surface to-brand/10 border border-violet-500/20 rounded-xl p-5">
        <div className="flex items-center gap-2 mb-3">
          <Sparkles className="w-5 h-5 text-violet-700" />
          <h3 className="text-ink font-semibold text-sm">系统自述 · 它眼中的自己</h3>
          <div className="ml-auto flex gap-2">
            <button onClick={() => loadSummary(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-violet-500/10 hover:bg-violet-500/20 text-violet-700 text-xs">
              <RefreshCw className="w-3 h-3" /> {summaryLoading ? '生成中...' : '重新生成'}
            </button>
            <button onClick={copySummary}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-surface-raised hover:bg-line text-ink text-xs">
              <Copy className="w-3 h-3" /> 复制
            </button>
          </div>
        </div>
        <p className="text-ink text-sm leading-relaxed whitespace-pre-wrap">
          {summaryLoading && !summary ? '正在让 LLM 阅读记忆库并生成自述...' : (summary || '暂无自述，点击「重新生成」。')}
        </p>
        {summaryError && (
          <p className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700">
            {summaryError}
          </p>
        )}
        {summaryAudit.warnings.length > 0 && (
          <div className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 space-y-1">
            <div className="font-medium">自述数字核对：发现 {summaryAudit.warnings.length} 处对不上（自述由 LLM 生成，数字不被本页背书）</div>
            {summaryAudit.warnings.map((w, i) => <div key={i}>· {w}</div>)}
          </div>
        )}
        {summaryAudit.warnings.length === 0 && summaryAudit.confirmations.length > 0 && (
          <div className="mt-2 rounded-lg border border-emerald-500/20 bg-emerald-500/5 px-3 py-2 text-xs text-emerald-700 space-y-1">
            {summaryAudit.confirmations.map((c, i) => <div key={i}>· {c}</div>)}
          </div>
        )}
        <p className="text-xs text-ink-faint mt-2">{summaryAudit.authorityNote}</p>
        <p className={`text-xs mt-1 ${summaryAudit.warnings.length > 0 ? 'text-amber-700/90' : 'text-ink-faint'}`}>
          {summaryAudit.publishNote}
        </p>
      </div>

      {/* 记忆库 */}
      <div className="bg-surface border border-line rounded-xl p-5">
        <div className="flex items-center gap-2 mb-4">
          <Brain className="w-5 h-5 text-amber-700" />
          <h3 className="text-ink font-semibold text-sm">记忆库 · 它记得什么</h3>
          <div className="ml-auto flex gap-2">
            {chip(`${stats.conversations} 对话`, 'bg-amber-500/15 text-amber-700')}
            {chip(`${stats.strategies} 策略`, 'bg-violet-500/15 text-violet-700')}
          </div>
        </div>

        {loading ? <div className="text-ink-faint text-xs py-6">加载中...</div> : (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
            <div>
              <div className="text-xs text-ink-faint mb-2">最近对话（任务目标）</div>
              <div className="space-y-2 max-h-96 overflow-y-auto pr-1">
                {convs.length === 0 && <div className="text-ink-faint text-xs">暂无</div>}
                {convs.map((c, i) => (
                  <div key={i} className="bg-surface-raised/40 border border-line rounded-lg p-3">
                    <div className="flex items-start gap-2">
                      <span className="text-amber-700/80 text-xs shrink-0">目标</span>
                      <span className="text-ink text-xs flex-1 break-words whitespace-pre-wrap">
                        {c.metadata.goal || c.content || '未记录目标'}
                      </span>
                      {c.metadata.timestamp && (
                        <span className="text-ink-faint text-xs shrink-0 whitespace-nowrap">
                          {formatLocalTimeShort(c.metadata.timestamp)}
                        </span>
                      )}
                      {c.id && (
                        <button onClick={() => del('conversations', [c.id!])}
                          disabled={!!busyAction || demoMode}
                          title={demoMode ? '演示模式下已停用（会真实删除记忆）' : '删除该条记忆'}
                          className="text-ink-faint hover:text-red-700 text-xs px-1 disabled:opacity-40">删除</button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <div className="text-xs text-ink-faint mb-2">成功策略（可复用的解决路径）</div>
              <div className="space-y-2 max-h-96 overflow-y-auto pr-1">
                {strats.length === 0 && <div className="text-ink-faint text-xs">暂无</div>}
                {strats.map((s, i) => (
                  <div key={i} className="bg-surface-raised/40 border border-line rounded-lg overflow-hidden">
                    <button onClick={() => toggle('s' + i)}
                      className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-surface-raised/60">
                      {expanded['s' + i] ? <ChevronDown className="w-3.5 h-3.5 text-ink-faint" /> : <ChevronRight className="w-3.5 h-3.5 text-ink-faint" />}
                      <span className="text-violet-700 text-xs flex-1 break-words">
                        {String(s.metadata.goal_keywords || '').trim() || '未记录关键词'}
                      </span>
                      {s.metadata.step_count && chip(`${s.metadata.step_count} 步`, 'bg-line/50 text-ink-muted')}
                      {s.id && (
                        <button onClick={() => del('strategies', [s.id!])}
                          disabled={!!busyAction || demoMode}
                          title={demoMode ? '演示模式下已停用（会真实删除策略）' : '删除该策略'}
                          className="text-ink-faint hover:text-red-700 text-xs px-1 disabled:opacity-40">删除</button>
                      )}
                    </button>
                    {expanded['s' + i] && (
                      <pre className="px-3 pb-3 text-ink-muted text-xs whitespace-pre-wrap font-sans max-h-48 overflow-y-auto">
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
      <div className="bg-surface border border-line rounded-xl p-5">
        <div className="flex items-center gap-2 mb-4">
          <ShieldCheck className="w-5 h-5 text-brand" />
          <h3 className="text-ink font-semibold text-sm">进化部署审批</h3>
          <span className="text-ink-faint text-xs">人工确认后，胜出策略将实际作用于对应 Worker</span>
        </div>
        {pendingList.length === 0 ? (
          <div className="text-ink-faint text-xs py-4 text-center">
            暂无待审批的策略部署请求。触发进化并产生稳定胜者后，会出现在这里等你审批。
          </div>
        ) : (
          <div className="space-y-3">
            {pendingList.map((p, i) => (
              <div key={i} className="bg-surface-raised/40 border border-line rounded-lg p-4">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-brand text-xs font-semibold">{p.strategy_id}</span>
                  {chip(p.agent_type || 'search_agent', 'bg-line/50 text-ink-muted')}
                  {p.temperature != null && chip('温度 ' + p.temperature, 'bg-line/50 text-ink-muted')}
                  {p.max_sources != null && chip('源数 ' + p.max_sources, 'bg-line/50 text-ink-muted')}
                  {p.timestamp && <span className="text-ink-faint text-xs ml-auto">{formatLocalTimeLabel(p.timestamp)}</span>}
                </div>
                <div className="mt-2 text-xs text-ink-muted">
                  摘要提示：<span className="text-ink">{p.summarization_prompt || '—'}</span>
                </div>
                {(p.filter_rules || []).length > 0 && (
                  <div className="mt-1 text-xs text-ink-muted">
                    过滤规则：<span className="text-ink">{String(p.filter_rules.join('；'))}</span>
                  </div>
                )}
                <div className="mt-3 flex gap-2">
                  <button onClick={() => approveStrategy(p.strategy_id, true)}
                    disabled={!!busyAction || demoMode}
                    title={demoMode ? '演示模式下已停用（会真实部署策略）' : '批准并部署该策略'}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-700 text-xs disabled:opacity-40">
                    <CheckCircle2 className="w-3.5 h-3.5" /> 批准部署
                  </button>
                  <button onClick={() => approveStrategy(p.strategy_id, false)}
                    disabled={!!busyAction || demoMode}
                    title={demoMode ? '演示模式下已停用' : '驳回该部署请求'}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-surface-raised hover:bg-line text-ink text-xs disabled:opacity-40">
                    <XCircle className="w-3.5 h-3.5" /> 驳回
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 进化锦标赛回放 */}
      <div className="bg-surface border border-line rounded-xl p-5">
        <div className="flex items-center gap-2 mb-4">
          <FlaskConical className="w-5 h-5 text-violet-700" />
          <h3 className="text-ink font-semibold text-sm">进化锦标赛回放</h3>
          <button onClick={triggerEvolution} disabled={demoMode}
            title={demoMode ? '演示模式下已停用（进化会真实烧 token 并跑后台任务）' : '触发一轮策略进化（后台约 3-5 分钟）'}
            className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-violet-500/10 hover:bg-violet-500/20 text-violet-700 text-xs disabled:opacity-40">
            <Play className="w-3 h-3" /> 触发新一轮进化
          </button>
        </div>

        {rounds.length === 0 ? (
          <div className="text-ink-faint text-xs py-6 text-center">
            暂无进化记录。点击「触发新一轮进化」开始；进化在后台运行约 3-5 分钟。
          </div>
        ) : (
          <div className="space-y-3">
            {rounds.map((r, i) => (
              <div key={i} className="bg-surface-raised/40 border border-line rounded-lg overflow-hidden">
                <button onClick={() => toggle('r' + i)}
                  className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-surface-raised/60">
                  <ChevronDown className="w-4 h-4 text-ink-faint shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="text-ink text-xs font-medium break-words">
                      {localizeEvolutionSummary(r.summary) || '进化轮次（后端未回传摘要）'}
                    </div>
                    {r.timestamp && (
                      <div className="text-ink-faint text-xs">{formatLocalTimeLabel(r.timestamp)}</div>
                    )}
                  </div>
                  {r.stable ? chip('稳定', 'bg-emerald-500/15 text-emerald-700') : chip('不稳定', 'bg-amber-500/15 text-amber-700')}
                  {r.deployed ? chip('已部署', 'bg-brand/15 text-brand') : chip('未部署', 'bg-line/50 text-ink-muted')}
                  {r.winner && chip('胜者 ' + String(r.winner.strategy_id || ''), 'bg-violet-500/15 text-violet-700')}
                </button>

                {expanded['r' + i] && (
                  <div className="px-4 pb-4 border-t border-line">
                    {(r.scoreboard && Object.keys(r.scoreboard).length > 0) && (
                      <div className="mt-3">
                        <div className="text-xs text-ink-faint mb-1">
                          各策略累计得分（取自 /api/evolution 的 scoreboard；评分口径由后端进化竞技场给出，本页不解释分值含义）
                        </div>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                          {Object.entries(r.scoreboard).map(([k, v]) => (
                            <div key={k} className="bg-surface-raised/60 rounded-lg p-2 text-center">
                              <div className="text-brand font-bold text-sm">{v}</div>
                              <div className="text-ink-faint text-xs break-words">{k}</div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {(r.win_counts && Object.keys(r.win_counts).length > 0) && (
                      <div className="mt-2 text-xs text-ink-faint">
                        取胜次数：{scoreMapText(r.win_counts) || '未取得'}
                      </div>
                    )}
                    <div className="mt-3 space-y-1.5">
                      {(r.rankings || []).map((rk, j) => (
                        <div key={j} className="text-xs bg-surface-raised/40 rounded-lg px-3 py-2">
                          <div className="flex items-start gap-2">
                            {rk.winner
                              ? <CheckCircle2 className="w-3.5 h-3.5 text-emerald-700 shrink-0 mt-0.5" />
                              : <XCircle className="w-3.5 h-3.5 text-ink-faint shrink-0 mt-0.5" />}
                            <span className="text-ink flex-1 break-words">
                              任务：{rk.task || '未记录任务'}
                            </span>
                          </div>
                          <div className="mt-1 pl-5 text-ink-muted break-words">
                            排名：{(rk.ranking || []).join(' → ') || rk.winner || '未取得'}
                          </div>
                          {rk.scores && Object.keys(rk.scores).length > 0 && (
                            <div className="mt-1 pl-5 text-ink-faint break-words">
                              各方案得分：{scoreMapText(rk.scores)}
                            </div>
                          )}
                          {rk.reason && (
                            <div className="mt-1 pl-5 text-ink-faint break-words">
                              评审理由：{rk.reason}
                            </div>
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
