// 审计日志页（T3）：GET /api/audit，仅 admin
import { useState, useEffect } from 'react'
import { ShieldCheck } from 'lucide-react'
import { auditDetailLabel, resultClass, resultLabel } from '../lib/statusMeta'
import { formatLocalTime } from '../lib/format'

interface AuditEntry {
  ts?: string
  timestamp?: string
  user?: string
  ip?: string
  action?: string
  target?: string
  result?: string
  detail?: string
}

/** `/api/audit` 的默认单页上限（web_ui.py:4444 `limit = 200`）。 */
const AUDIT_LIMIT = 200

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

  // 标题过去写死"登录/任务提交/删除/分享等操作留痕"，而实机 200 条全是
  // system / fetch_document——承诺的类型与实际内容不符。这里改为**按实际数据**说明，
  // 并如实交代接口单页上限，用户不必自己数。
  const actionSummary = Array.from(new Set(entries.map(e => e.action).filter(Boolean)))
    .slice(0, 6).join(' / ')

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      <div className="flex items-center gap-2">
        <ShieldCheck className="w-5 h-5 text-brand" />
        <h1 className="text-lg font-semibold text-ink">审计日志</h1>
        <span className="text-xs text-ink-faint">
          最近 {entries.length} 条操作留痕{actionSummary ? `（本页动作：${actionSummary}）` : ''}
          {entries.length >= AUDIT_LIMIT && `，接口单页上限 ${AUDIT_LIMIT} 条，更早的记录未列出`}
        </span>
      </div>
      {loading && <div className="text-ink-faint text-sm">加载中…</div>}
      {error && <div className="bg-amber-500/10 border border-amber-500/30 text-amber-700 text-sm rounded-lg px-4 py-3">{error}</div>}
      <div className="bg-surface border border-line rounded-xl overflow-hidden">
        {entries.length === 0 && !loading && (
          <div className="text-ink-faint text-sm text-center py-12">暂无审计记录</div>
        )}
        <p className="sm:hidden px-4 pt-3 text-xs text-ink-faint">
          ← 表格较宽，左右滑动可查看「动作 / 目标 / 结果 / 详情」列
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-ink-faint border-b border-line">
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
                <tr key={i} className="border-b border-line/60 hover:bg-surface-raised/30">
                  <td className="px-4 py-2.5 text-xs text-ink-muted whitespace-nowrap">{formatLocalTime(e.ts || e.timestamp)}</td>
                  <td className="px-4 py-2.5 text-xs text-ink">{e.user || '-'}</td>
                  <td className="px-4 py-2.5 text-xs text-brand">{e.action || '-'}</td>
                  <td className="px-4 py-2.5 text-xs text-ink max-w-[160px] truncate" title={e.target || ''}>{e.target || '-'}</td>
                  <td className="px-4 py-2.5 text-xs">
                    <span className={resultClass(e.result)}>{resultLabel(e.result)}</span>
                  </td>
                  <td className="px-4 py-2.5 text-xs text-ink-faint max-w-[260px] truncate" title={e.detail || ''}>{auditDetailLabel(e.detail) || ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
