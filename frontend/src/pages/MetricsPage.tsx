// 指标看板页（T4）：GET /api/metrics（metrics_collector 汇总 JSON）
import { useState, useEffect, useCallback } from 'react'
import { BarChart3, RefreshCw } from 'lucide-react'
import { useVisibleInterval } from '../lib/useVisibleInterval'

interface MetricsSummary {
  timestamp?: string
  total_tasks?: number
  failed_tasks?: number
  success_rate?: number
  failure_rate?: number
  avg_latency_sec?: number
  p95_latency_sec?: number
  cost_usd_total?: number
  critic?: { pass: number; fail: number }
  replan?: { total: number; success: number }
  alerts?: number
  by_capability?: Record<string, { success_rate: number }>
  search_health?: Record<string, unknown>
}

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="text-xl font-semibold text-slate-200 mt-1">{value}</div>
      {sub && <div className="text-[11px] text-slate-500 mt-0.5">{sub}</div>}
    </div>
  )
}

export default function MetricsPage() {
  const [m, setM] = useState<MetricsSummary | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    fetch('/api/metrics')
      .then(r => { if (!r.ok) throw new Error('no data'); return r.json() })
      .then(d => { setM(d); setError('') })
      .catch(() => setError('暂无指标数据（metrics 收集器运行后产生）'))
  }, [])

  useEffect(() => { load() }, [load])
  useVisibleInterval(load, 30000)

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
      {error && <div className="bg-amber-500/10 border border-amber-500/30 text-amber-400 text-sm rounded-lg px-4 py-3">{error}</div>}
      {m && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <StatCard label="总任务数" value={String(m.total_tasks ?? 0)} />
            <StatCard label="成功率" value={`${m.success_rate ?? 0}%`} sub={`失败率 ${m.failure_rate ?? 0}%`} />
            <StatCard label="平均耗时" value={`${m.avg_latency_sec ?? 0}s`} sub={`P95 ${m.p95_latency_sec ?? 0}s`} />
            <StatCard label="累计成本" value={`$${(m.cost_usd_total ?? 0).toFixed(4)}`} />
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <StatCard label="Critic 评审" value={`${m.critic?.pass ?? 0} 通过`} sub={`${m.critic?.fail ?? 0} 未通过`} />
            <StatCard label="重规划" value={String(m.replan?.total ?? 0)} sub={`${m.replan?.success ?? 0} 成功`} />
            <StatCard label="告警" value={String(m.alerts ?? 0)} />
            <StatCard label="搜索源" value={m.search_health && Object.keys(m.search_health).length > 0 ? '已接入' : '—'} />
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <h2 className="text-sm text-slate-300 font-medium mb-4">各能力成功率</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {Object.entries(m.by_capability || {}).map(([cap, v]) => (
                <div key={cap} className="border border-slate-800 rounded-lg p-3">
                  <div className="text-xs text-slate-400 truncate">{cap}</div>
                  <div className="text-lg font-semibold mt-1" style={{ color: v.success_rate >= 80 ? '#34d399' : v.success_rate >= 50 ? '#fbbf24' : '#f87171' }}>
                    {v.success_rate}%
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
