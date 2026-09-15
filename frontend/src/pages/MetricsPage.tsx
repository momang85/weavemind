// 指标看板页（T4）：GET /api/metrics（metrics_collector 汇总 JSON）+ GET /tasks（进行中数量）
import { useState, useEffect, useCallback } from 'react'
import { BarChart3, RefreshCw } from 'lucide-react'
import { useVisibleInterval } from '../lib/useVisibleInterval'
import { Card, ErrorState, SectionTitle, Skeleton, StatCard } from '../components/ui'
import { formatDuration, formatUsd } from '../lib/format'

interface TaskBreakdown {
  total: number
  success: number
  with_issues: number
  failed: number
  cancelled: number
  running: number
  queued: number
  unknown: number
  terminal: number
  available: boolean
  scope?: string
}

interface MetricsSummary {
  timestamp?: string
  total_tasks?: number
  failed_tasks?: number
  success_rate?: number | null
  failure_rate?: number | null
  avg_latency_sec?: number
  p95_latency_sec?: number
  cost_usd_total?: number
  critic?: { pass: number; fail: number }
  replan?: { total: number; success: number }
  alerts?: number
  by_capability?: Record<string, { success_rate: number }>
  search_health?: Record<string, unknown>
  /** 后端同一快照、同一范围（task_history 全表）的分类计数——前端只展示，不推断。 */
  tasks?: TaskBreakdown
}

export default function MetricsPage() {
  const [m, setM] = useState<MetricsSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // 全部统计量由后端在同一快照、同一范围（task_history 全表）算好。
  // 前端**不再**用 /tasks 列表（分页/截断）去推算运行数——那会把"100 个任务全在跑"
  // 算成 100% 成功（列表只回最近 50 条）。
  const load = useCallback(() => {
    fetch('/api/metrics')
      .then(r => { if (!r.ok) throw new Error('no data'); return r.json() })
      .then(d => { setM(d); setError('') })
      .catch(() => setError('指标接口不可达（服务未启动或 metrics 收集器未运行）。统计值显示为"未知"，不代表 0。'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])
  useVisibleInterval(load, 30000)

  const t = m?.tasks
  const available = t ? t.available : false
  const terminal = t?.terminal ?? 0
  // 成功率/失败率以终态为分母；无终态或统计不可用 → 显示"未知"，绝不显示 0% 或 100%
  const rate = (v?: number | null) => (typeof v === 'number' ? `${v}%` : '未知')

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      <div className="flex items-center gap-2">
        <BarChart3 className="w-5 h-5 text-cyan-400" />
        <h1 className="text-lg font-semibold text-slate-200">指标看板</h1>
        {m?.timestamp && <span className="text-xs text-slate-500">更新于 {m.timestamp.slice(0, 19).replace('T', ' ')}</span>}
        <button onClick={load} aria-label="刷新指标" className="ml-auto p-1.5 rounded-lg bg-slate-800 text-slate-400 hover:text-slate-200 transition-colors">
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>
      {error && <ErrorState title="指标不可用" description={error} onRetry={load} />}
      {loading && !m && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[0, 1, 2, 3].map(i => <Skeleton key={i} className="h-24" />)}
        </div>
      )}
      {m && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <StatCard label="总任务数" value={available ? String(t?.total ?? 0) : '未知'}
              sub={available ? `终态 ${terminal} · 进行中 ${t?.running ?? 0} · 排队 ${t?.queued ?? 0}` : '任务库读取失败'}
              hint={t?.scope ? `统计范围：${t.scope}` : undefined} />
            <StatCard label="成功率" value={available && terminal > 0 ? rate(m.success_rate) : (available ? '—' : '未知')}
              tone={!available || terminal === 0 ? 'default'
                : (m.success_rate ?? 0) >= 80 ? 'success' : (m.success_rate ?? 0) >= 50 ? 'issue' : 'failure'}
              sub={!available ? '统计不可用，不能按 0 计'
                : terminal === 0 ? `尚无终态任务（进行中 ${t?.running ?? 0}）`
                : `分母 ${terminal}：成功 ${t?.success ?? 0} · 有缺口 ${t?.with_issues ?? 0} · 失败 ${t?.failed ?? 0} · 已取消 ${t?.cancelled ?? 0}`}
              hint="分母为终态任务数；进行中与排队中不计入" />
            <StatCard label="失败率" value={available && terminal > 0 ? rate(m.failure_rate) : (available ? '—' : '未知')}
              tone={available && terminal > 0 && (m.failure_rate ?? 0) > 0 ? 'failure' : 'default'}
              sub={available && terminal > 0 ? `失败 ${t?.failed ?? 0}/${terminal}` : '无终态任务'}
              hint={t && t.unknown > 0 ? `另有 ${t.unknown} 条状态未登记，未计入成功或失败` : undefined} />
            <StatCard label="累计成本" value={formatUsd(m.cost_usd_total)}
              sub={available ? `含进行中 ${t?.running ?? 0} 个任务` : undefined} />
          </div>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            <StatCard label="平均耗时" value={available && terminal > 0 ? formatDuration(m.avg_latency_sec) : '—'}
              sub={available && terminal > 0 ? `P95 ${formatDuration(m.p95_latency_sec)}` : '无终态任务'} />
            <StatCard label="Critic 评审" value={`${m.critic?.pass ?? 0} 通过`} sub={`${m.critic?.fail ?? 0} 未通过`} />
            <StatCard label="重规划" value={String(m.replan?.total ?? 0)} sub={`${m.replan?.success ?? 0} 成功`} />
            <StatCard label="告警" value={String(m.alerts ?? 0)} />
            <StatCard label="搜索源" value={m.search_health && Object.keys(m.search_health).length > 0 ? '已接入' : '—'} />
          </div>
          <Card>
            <SectionTitle title="各能力成功率"
              extra={t?.scope ? `统计范围：${t.scope}` : undefined} />
            {Object.keys(m.by_capability || {}).length === 0 && (
              <div className="text-xs text-slate-500 leading-relaxed">
                暂无数据：需要至少一次任务结束才会按能力类型统计。当前
                {terminal > 0 ? `已有 ${terminal} 个终态任务，统计将随下次汇总刷新` : `没有终态任务（进行中 ${t?.running ?? 0}）`}。
              </div>
            )}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {Object.entries(m.by_capability || {}).map(([cap, v]) => (
                <div key={cap} className="border border-line rounded-lg p-3">
                  <div className="text-xs text-ink-muted truncate">{cap}</div>
                  <div className={`text-lg font-semibold mt-1 tabular-nums ${
                    v.success_rate >= 80 ? 'text-state-success' : v.success_rate >= 50 ? 'text-state-issue' : 'text-state-failure'
                  }`}>
                    {v.success_rate}%
                  </div>
                </div>
              ))}
            </div>
          </Card>
        </>
      )}
    </div>
  )
}
