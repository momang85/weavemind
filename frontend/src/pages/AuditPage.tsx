// 审计日志页（T3）：GET /api/audit，仅 admin
import { useState, useEffect } from 'react'
import { ShieldCheck } from 'lucide-react'

interface AuditEntry {
  ts?: string
  user?: string
  ip?: string
  action?: string
  target?: string
  result?: string
  detail?: string
}

export default function AuditPage() {
  const [entries, setEntries] = useState<AuditEntry[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch('/api/audit')
      .then(r => r.json())
      .then(d => setEntries(d.entries || []))
      .catch(() => setError('加载审计日志失败（仅管理员可查看）'))
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      <div className="flex items-center gap-2">
        <ShieldCheck className="w-5 h-5 text-cyan-400" />
        <h1 className="text-lg font-semibold text-slate-200">审计日志</h1>
        <span className="text-xs text-slate-500">登录/任务提交/删除/分享等操作留痕（{entries.length} 条）</span>
      </div>
      {loading && <div className="text-slate-500 text-sm">加载中…</div>}
      {error && <div className="bg-amber-500/10 border border-amber-500/30 text-amber-400 text-sm rounded-lg px-4 py-3">{error}</div>}
      <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
        {entries.length === 0 && !loading && (
          <div className="text-slate-600 text-sm text-center py-12">暂无审计记录</div>
        )}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-slate-500 border-b border-slate-800">
                <th className="px-4 py-3 font-medium">时间</th>
                <th className="px-4 py-3 font-medium">用户</th>
                <th className="px-4 py-3 font-medium">动作</th>
                <th className="px-4 py-3 font-medium">目标</th>
                <th className="px-4 py-3 font-medium">结果</th>
                <th className="px-4 py-3 font-medium">详情</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e, i) => (
                <tr key={i} className="border-b border-slate-800/60 hover:bg-slate-800/30">
                  <td className="px-4 py-2.5 text-xs text-slate-400 whitespace-nowrap">{e.ts || '-'}</td>
                  <td className="px-4 py-2.5 text-xs text-slate-300">{e.user || '-'}</td>
                  <td className="px-4 py-2.5 text-xs text-cyan-400">{e.action || '-'}</td>
                  <td className="px-4 py-2.5 text-xs text-slate-300 max-w-[160px] truncate">{e.target || '-'}</td>
                  <td className="px-4 py-2.5 text-xs">
                    <span className={e.result === 'ok' ? 'text-emerald-400' : 'text-amber-400'}>{e.result || '-'}</span>
                  </td>
                  <td className="px-4 py-2.5 text-xs text-slate-500 max-w-[260px] truncate">{e.detail || ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
