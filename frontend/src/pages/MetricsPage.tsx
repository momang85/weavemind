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
  const [running, setRunning] = useState(0)
  const [error, setError] = useState('')

  // 指标汇总来自 metrics_summary.json（已落库任务），但"还在跑"的任务不在其中：
  // 若直接把 total_tasks 当分母，运行中就会被算成失败——实测首屏显示
  // "成功率 0% / 失败率 100%"（两个任务刚提交、正在跑），极易误读为系统故障。
  // 这里额外取 /tasks 得到进行中数量，成功率只按"已完成"计算。
  const load = useCallback(() => {
    Promise.all([
      fetch('/api/metrics').then(r => { if (!r.ok) throw new Error('no data'); return r.json() }),
      fetch('/tasks').then(r => (r.ok ? r.json() : null)).catch(() => null),
    ])
      .then(([metrics, tasksRaw]) => {
        setM(metrics)
        setError('')
        const list = Array.isArray(tasksRaw) ? tasksRaw : (tasksRaw?.tasks || [])
        setRunning(list.filter((t: any) =>
          ['RUNNING', 'QUEUED', 'PENDING'].includes(String(t?.status || '').toUpperCase())).length)
      })
      .catch(() => setError('暂无指标数据（服务不可达或 metrics 收集器尚未运行）'))
  }, [])

  useEffect(() => { load() }, [load])
  useVisibleInterval(load, 30000)

  const total = m?.total_tasks ?? 0
  const completed = Math.max(0, total - running)
  const failed = Math.min(m?.failed_tasks ?? 0, completed)
  const succeeded = Math.max(0, completed - failed)
  const successRate = completed > 0 ? Math.round((succeeded / completed) * 100) : null

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
            <StatCard label="总任务数" value={String(total)}
              sub={running > 0 ? `进行中 ${running}` : `已完成 ${completed}`} />
            <StatCard label="成功率" value={successRate === null ? '—' : `${successRate}%`}
              sub={completed > 0 ? `已完成 ${completed} · 失败 ${failed}` : '尚无已完成任务'} />
            <StatCard label="平均耗时" value={completed > 0 ? `${m.avg_latency_sec ?? 0}s` : '—'}
              sub={completed > 0 ? `P95 ${m.p95_latency_sec ?? 0}s` : '尚无已完成任务'} />
            <StatCard label="累计成本" value={`$${(m.cost_usd_total ?? 0).toFixed(4)}`}
              sub={running > 0 ? `含进行中 ${running} 个任务` : undefined} />
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <StatCard label="Critic 评审" value={`${m.critic?.pass ?? 0} 通过`} sub={`${m.critic?.fail ?? 0} 未通过`} />
            <StatCard label="重规划" value={String(m.replan?.total ?? 0)} sub={`${m.replan?.success ?? 0} 成功`} />
            <StatCard label="告警" value={String(m.alerts ?? 0)} />
            <StatCard label="搜索源" value={m.search_health && Object.keys(m.search_health).length > 0 ? '已接入' : '—'} />
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
            <h2 className="text-sm text-slate-300 font-medium mb-4">各能力成功率</h2>
            {Object.keys(m.by_capability || {}).length === 0 && (
              <div className="text-xs text-slate-500">暂无数据：任务完成一次后按能力类型统计（当前无已完成任务）。</div>
            )}
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
