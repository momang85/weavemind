import { useCallback, useEffect, useState } from 'react'
import { Link2, Upload, RefreshCw, AlertTriangle, CheckCircle2 } from 'lucide-react'

/**
 * 补材料面板（C1）：披露直链 / 上传文件 → 正常入口摄取 → 同一证据快照。
 *
 * 三条与后端一致的显示纪律：
 * - 未准入就说未准入（含原因与"可作背景"），不显示成"已采用"；
 * - 逐指标状态分三种"没有"（未取得 / 已取得但未定位 / 在列明范围内未披露）；
 * - 正文重新生成是**待办**（等待既有预算/授权），界面不假装已经改好正文。
 */

type MetricState = { state?: string; label?: string; matched?: string; section_count?: number }

type Material = {
  material_id: string
  channel?: string
  channel_label?: string
  status?: string
  reason?: string
  title?: string
  url?: string
  filename?: string
  kind_label?: string
  source_class?: string
  provenance_label?: string
  bytes?: number
  raw_sha256?: string
  text_sha256?: string
  disclosure_date?: string
  date_precision?: string
  date_basis?: string
  evidence_count?: number
  metric_states?: Record<string, MetricState>
  read_scope?: { text_chars?: number; parsed_ranges?: number[][]; unparsed_ranges?: number[][]; truncated?: boolean }
  pending?: { kind?: string; reason?: string }[]
  created_at?: string
}

const STATUS_TEXT: Record<string, { label: string; cls: string }> = {
  admitted: { label: '已准入', cls: 'text-emerald-400 border-emerald-500/30 bg-emerald-500/10' },
  rejected: { label: '未准入', cls: 'text-amber-400 border-amber-500/30 bg-amber-500/10' },
  fetch_failed: { label: '未取得原文', cls: 'text-red-400 border-red-500/30 bg-red-500/10' },
  pending_intake: { label: '待摄取', cls: 'text-slate-400 border-slate-600 bg-slate-800/40' },
}

const METRIC_LABEL: Record<string, string> = {
  revenue: '收入', net_profit: '利润', operating_cashflow: '现金流',
}

export default function MaterialPanel({ taskId, disabled }: { taskId: string | null; disabled?: boolean }) {
  const [items, setItems] = useState<Material[]>([])
  const [url, setUrl] = useState('')
  const [title, setTitle] = useState('')
  const [declared, setDeclared] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'warn' | 'err'; text: string } | null>(null)

  const load = useCallback(async () => {
    if (!taskId) { setItems([]); return }
    try {
      const d = await (await fetch(`/api/task/${taskId}/materials`)).json()
      setItems(d.materials ?? [])
    } catch { /* 读不到就保持上一次的清单，不编 */ }
  }, [taskId])

  useEffect(() => { load() }, [load])

  const submit = async (payload: Record<string, unknown>) => {
    if (!taskId) return
    setBusy(true)
    setMsg(null)
    try {
      const res = await fetch(`/api/task/${taskId}/material`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const d = await res.json()
      if (res.ok && d.status === 'ok') {
        const st = d.intake?.metric_states ?? {}
        const states = Object.keys(st).map(k => `${METRIC_LABEL[k] ?? k}:${st[k]?.label ?? ''}`).join('；')
        setMsg({
          kind: 'ok',
          text: `已准入${d.intake?.reused ? '（复用既有结论）' : ''}，证据 ${d.intake?.evidence_count ?? 0} 条。${states}`
            + `\n正文需按新材料重新生成时另行授权（本次只重做了证据/结构/底稿）。`,
        })
      } else if (res.status === 503) {
        setMsg({ kind: 'warn', text: d.error || '编排器未回执；原件已保存，稍后会自动摄取。' })
      } else {
        setMsg({ kind: 'err', text: `${d.intake?.reason || d.error || '未准入'}\n${d.intake?.detail ?? ''}` })
      }
    } catch (e: any) {
      setMsg({ kind: 'err', text: `请求失败：${e?.message ?? e}` })
    } finally {
      setBusy(false)
      load()
    }
  }

  const submitLink = () => {
    if (!url.trim()) return
    submit({ kind: 'link', url: url.trim(), title: title.trim(), declared_disclosed_at: declared })
    setUrl(''); setTitle('')
  }

  const onFile = async (files: FileList | null) => {
    const f = files?.[0]
    if (!f) return
    const b64 = await new Promise<string>((resolve, reject) => {
      const r = new FileReader()
      r.onload = () => resolve(String(r.result).split(',')[1] || '')
      r.onerror = () => reject(new Error('read error'))
      r.readAsDataURL(f)
    }).catch(() => '')
    if (!b64) { setMsg({ kind: 'err', text: '文件读取失败' }); return }
    await submit({ kind: 'file', filename: f.name, content_type: f.type || '',
                   title: title.trim(), declared_disclosed_at: declared, data: b64 })
  }

  return (
    <div className="space-y-3 text-xs">
      <div className="text-slate-400">
        补充原始披露：给直链（由服务取件）或上传文件。材料经**同一准入判据**（主体/期间/
        披露日/正文完整性）后并入资料快照，证据、底稿与报告读的是同一份快照。
      </div>

      <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-3 space-y-2">
        <div className="flex items-center gap-2 text-slate-300">
          <Link2 className="w-3.5 h-3.5 text-cyan-400" /> 披露直链
        </div>
        <input value={url} onChange={e => setUrl(e.target.value)} disabled={disabled || busy}
          placeholder="https://…（官方披露平台或发行人公告直链）"
          className="w-full bg-slate-950 border border-slate-800 rounded px-2 py-1.5 text-slate-200" />
        <div className="grid grid-cols-2 gap-2">
          <input value={title} onChange={e => setTitle(e.target.value)} disabled={disabled || busy}
            placeholder="标题（可选，如：洋河股份:2024年年度报告）"
            className="bg-slate-950 border border-slate-800 rounded px-2 py-1.5 text-slate-200" />
          <input value={declared} onChange={e => setDeclared(e.target.value)} disabled={disabled || busy}
            placeholder="披露日（材料自身没带日期时才用，如 2025-04-29）"
            className="bg-slate-950 border border-slate-800 rounded px-2 py-1.5 text-slate-200" />
        </div>
        <div className="flex items-center gap-2">
          <button onClick={submitLink} disabled={disabled || busy || !url.trim()}
            className="px-3 py-1.5 rounded bg-cyan-500/15 border border-cyan-500/30 text-cyan-300 disabled:opacity-40">
            添加直链
          </button>
          <label className={`px-3 py-1.5 rounded border border-slate-700 text-slate-300 inline-flex items-center gap-1.5 ${disabled || busy ? 'opacity-40' : 'cursor-pointer hover:bg-slate-800'}`}>
            <Upload className="w-3.5 h-3.5" /> 上传文件
            <input type="file" className="hidden" disabled={disabled || busy}
              accept=".pdf,.txt,.md,.html,.htm,.json" onChange={e => onFile(e.target.files)} />
          </label>
          {busy && <span className="text-slate-500 inline-flex items-center gap-1">
            <RefreshCw className="w-3.5 h-3.5 animate-spin" /> 摄取中…</span>}
        </div>
        <div className="text-slate-600">
          上限：单件 3 MiB、PDF ≤ 400 页；不接受压缩包。上市公司的披露日以材料自带日期为准，
          自填日期只作声明依据（会如实标注）。
        </div>
      </div>

      {msg && (
        <div className={`rounded-xl border px-3 py-2 whitespace-pre-wrap ${
          msg.kind === 'ok' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
            : msg.kind === 'warn' ? 'border-amber-500/30 bg-amber-500/10 text-amber-300'
              : 'border-red-500/30 bg-red-500/10 text-red-300'}`}>
          <span className="inline-flex items-center gap-1.5">
            {msg.kind === 'ok' ? <CheckCircle2 className="w-3.5 h-3.5" /> : <AlertTriangle className="w-3.5 h-3.5" />}
            {msg.text}
          </span>
        </div>
      )}

      <div className="space-y-2">
        <div className="text-slate-400">任务材料（{items.length}）</div>
        {items.length === 0 && <div className="text-slate-600 text-center py-6">还没有补充材料。</div>}
        {items.map(m => {
          const st = STATUS_TEXT[m.status ?? ''] ?? { label: m.status ?? '未知', cls: 'text-slate-400 border-slate-700' }
          const scope = m.read_scope ?? {}
          return (
            <div key={m.material_id} className="bg-slate-900/60 border border-slate-800 rounded-xl p-3 space-y-1.5">
              <div className="flex items-center gap-2 flex-wrap">
                <span className={`px-1.5 py-0.5 rounded border ${st.cls}`}>{st.label}</span>
                <span className="text-slate-300 truncate max-w-[60%]" title={m.title}>{m.title || m.url || m.material_id}</span>
                <span className="text-slate-600">{m.channel_label}</span>
              </div>
              <div className="text-slate-500 break-all">
                {m.filename || m.url}
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-1 text-slate-500">
                {m.kind_label && <span>{m.kind_label}</span>}
                {m.source_class && <span>来源类别：{m.source_class}</span>}
                {m.provenance_label && <span>{m.provenance_label}</span>}
                {m.disclosure_date && (
                  <span>披露日 {m.disclosure_date}
                    {m.date_basis ? `（依据：${m.date_basis}）` : ''}</span>
                )}
                <span>证据 {m.evidence_count ?? 0} 条</span>
                {typeof scope.text_chars === 'number' && (
                  <span>正文 {scope.text_chars} 字；已解析 {(scope.parsed_ranges ?? []).length} 段，
                    未解析 {(scope.unparsed_ranges ?? []).length} 段{scope.truncated ? '（有截断）' : ''}</span>
                )}
              </div>
              {m.raw_sha256 && (
                <div className="text-slate-600 break-all">
                  原件 sha256 {m.raw_sha256.slice(0, 16)}… · 正文 sha256 {(m.text_sha256 || '').slice(0, 16)}…
                </div>
              )}
              {m.metric_states && Object.keys(m.metric_states).length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(m.metric_states).map(([k, v]) => (
                    <span key={k} className={`px-1.5 py-0.5 rounded border ${
                      v.state === 'present_in_scope' ? 'border-emerald-500/30 text-emerald-400'
                        : 'border-slate-700 text-slate-500'}`}>
                      {METRIC_LABEL[k] ?? k}：{v.label ?? v.state}
                    </span>
                  ))}
                </div>
              )}
              {m.reason && <div className="text-amber-400">未准入原因：{m.reason}</div>}
              {(m.pending ?? []).length > 0 && (
                <div className="text-amber-400">待办：{(m.pending ?? []).map(p => p.reason).join('；')}</div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
