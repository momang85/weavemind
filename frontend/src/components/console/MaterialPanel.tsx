import { useCallback, useEffect, useState } from 'react'
import { Link2, Upload, RefreshCw, AlertTriangle, CheckCircle2, FileText } from 'lucide-react'

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
  const [cand, setCand] = useState<any>(null)
  // C 批：候选闭环——预览（正文 + 与当前稿比较 + 证据缺口）→ 显式采纳 → 重验
  const [prev, setPrev] = useState<any>(null)
  const [adopted, setAdopted] = useState<any>(null)
  const [showBody, setShowBody] = useState(false)
  const [adoptArmed, setAdoptArmed] = useState(false)
  const [op, setOp] = useState<string>('')

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

  // 「按新材料生成候选正文」：走**确定性装配**（不调用模型、不消耗额度）。
  // 候选**不采纳**：旧交付与人工文字原样保留，采纳是另一个显式动作。
  const genCandidate = async () => {
    if (!taskId) return
    setBusy(true); setMsg(null); setCand(null); setPrev(null); setAdopted(null); setOp('')
    try {
      const res = await fetch(`/api/task/${taskId}/candidate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      const d = await res.json()
      if (res.ok && d.ok) {
        setCand(d)
        const aff = (d.affected_steps ?? []).map((s: any) => s.step_id || s.capability).filter(Boolean)
        setMsg({
          kind: 'ok',
          text: `${d.created ? '已生成' : '已存在同一个候选（未重复生成）'}：`
            + `版本 ${String(d.candidate_version_id ?? '').slice(0, 12)}…\n`
            + `影响步骤 ${aff.length} 个（${aff.join('、') || '—'}）；其余步骤未重做。\n`
            + `生成方式：确定性装配（未调用模型、未消耗额度）。\n`
            + `候选未采纳：旧交付与人工文字保留，需显式采纳后才会成为交付。`,
        })
      } else if (d.status === 'no_new_material') {
        setMsg({ kind: 'warn', text: '还没有已并入的新材料：先添加材料并等它准入。' })
      } else if (d.status === 'paid_not_authorized') {
        setMsg({ kind: 'warn', text: d.error || '本轮不新增付费生成。' })
      } else if (d.status === 'pending') {
        // 超时 = **结果未知**：给 operation id 与查回入口，不谎称"没有生成"
        setOp(String(d.operation_id ?? ''))
        setMsg({ kind: 'warn',
                 text: `${d.error || '本次结果未知'}\noperation id：${d.operation_id ?? '—'}` })
      } else {
        setMsg({ kind: 'err', text: d.error || d.detail || d.status || '未能生成候选正文' })
      }
    } catch (e: any) {
      setMsg({ kind: 'err', text: `请求失败：${e?.message ?? e}` })
    } finally {
      setBusy(false); load()
    }
  }

  /** 查回一次候选操作的结果（超时/刷新后仍能查到；查不到 ≠ 没生成）。 */
  const queryOp = async (operationId?: string) => {
    if (!taskId) return
    setBusy(true)
    try {
      const q = operationId ? `?operation_id=${encodeURIComponent(operationId)}` : ''
      const res = await fetch(`/api/task/${taskId}/candidate${q}`)
      const d = await res.json()
      if (res.ok && d.ok) {
        setCand(d)
        setMsg({ kind: 'ok', text: `已查回该操作的结果：${
          d.created === false ? '此前已生成（复用同一个候选）' : '已生成候选'}。` })
      } else {
        setMsg({ kind: d.status === 'unknown' ? 'warn' : 'err',
                 text: d.error || `查回失败（${d.status ?? res.status}）` })
      }
    } catch (e: any) {
      setMsg({ kind: 'err', text: `查回失败：${e?.message ?? e}` })
    } finally { setBusy(false) }
  }

  /** 预览候选：候选**正文** + 与当前采纳稿的**结构化比较** + 证据缺口（只读）。 */
  const previewCandidate = async (identity?: string) => {
    if (!taskId) return
    setBusy(true); setMsg(null)
    try {
      const id = identity || String(cand?.candidate_identity_id ?? '')
      const q = id ? `?identity=${encodeURIComponent(id)}` : ''
      const res = await fetch(`/api/task/${taskId}/candidate/preview${q}`)
      const d = await res.json()
      if (res.ok) {
        setPrev(d)
        const c = d.comparison ?? {}
        setMsg({ kind: 'ok',
                 text: `候选预览（只读，未采纳）：与当前稿相比改动 ${c.changed_lines ?? 0} 行`
                   + `（+${c.added_lines ?? 0}/-${c.removed_lines ?? 0}）；`
                   + `人工分析节${c.analysis_section_preserved ? '原样保留' : '**未保留**'}。` })
      } else {
        setMsg({ kind: 'err', text: d.error || d.status || '预览失败' })
      }
    } catch (e: any) {
      setMsg({ kind: 'err', text: `预览失败：${e?.message ?? e}` })
    } finally { setBusy(false) }
  }

  /** **显式采纳**：两步确认——第一次点击只是"上膛"，看清将要发生什么；第二次才真的提交。
   *
   *  为什么不用 `window.confirm`：原生弹窗既挡住页面上的身份/比较信息（用户是"盲确认"），
   *  也无法被自动化验收点击（脚本环境里 confirm 默认被拒 → 采纳永远走不到）。
   */
  const adoptArm = () => {
    const identity = String(prev?.candidate?.identity_id ?? cand?.candidate_identity_id ?? '')
    if (!identity) {
      setMsg({ kind: 'warn', text: '先预览候选，确认要采纳的那一版。' })
      return
    }
    setAdoptArmed(true)
  }

  const adoptCandidate = async () => {
    const identity = String(prev?.candidate?.identity_id ?? cand?.candidate_identity_id ?? '')
    if (!taskId || !identity) {
      setMsg({ kind: 'warn', text: '先预览候选，确认要采纳的那一版。' })
      return
    }
    setBusy(true); setMsg(null)
    try {
      const res = await fetch(`/api/task/${taskId}/candidate/adopt`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identity, confirm: true }),
      })
      const d = await res.json()
      if (res.ok && d.ok) {
        setAdopted(d); setAdoptArmed(false)
        const r = d.reverify ?? {}
        setMsg({ kind: 'ok',
                 text: `${d.changed ? '已采纳' : '这个身份已是当前选中版本（未重复采纳）'}：`
                   + `${String(d.adopted?.version_id ?? '').slice(0, 12)}…\n`
                   + `对该身份重验：${r.overall || '（未知）'}`
                   + `${r.covers_adopted_identity ? '' : '（未绑到该身份，按待重验处理）'}\n`
                   + `人工复核文件${d.human_review_untouched ? '未被改动' : '**被改动了（异常）**'}；`
                   + `旧批准未继承。${d.next?.note ?? ''}` })
        await previewCandidate(identity)
      } else {
        setMsg({ kind: 'err', text: d.error || d.status || '采纳失败' })
      }
    } catch (e: any) {
      setMsg({ kind: 'err', text: `采纳失败：${e?.message ?? e}` })
    } finally { setBusy(false); load() }
  }

  const admittedCount = items.filter(m => m.status === 'admitted').length

  return (
    <div className="space-y-3 text-xs">
      <div className="text-slate-400">
        补充原始披露：给直链（由服务取件）或上传文件。材料经同一准入判据（主体/期间/
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

      <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-3 space-y-2">
        <div className="flex items-center gap-2 text-slate-300">
          <FileText className="w-3.5 h-3.5 text-violet-400" /> 按新材料生成候选正文
        </div>
        <div className="text-slate-500">
          用已并入的新材料重新装配正文（<span className="text-slate-400">确定性装配</span>：
          不调用模型、不消耗检索或生成额度）。同一次动作只生成一个候选；候选
          <span className="text-slate-400">不采纳</span>——旧交付、人工文字与旧批准都保留，
          需你显式采纳后才会成为交付。只重做产出正文的步骤，检索/清洗不重跑。
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button onClick={genCandidate}
            disabled={disabled || busy || admittedCount === 0 || !taskId}
            className="px-3 py-1.5 rounded bg-violet-500/15 border border-violet-500/30 text-violet-300 disabled:opacity-40">
            生成候选正文
          </button>
          <button onClick={() => previewCandidate()}
            disabled={disabled || busy || !taskId}
            className="px-3 py-1.5 rounded bg-slate-800 border border-slate-700 text-slate-200 disabled:opacity-40">
            预览/比较候选
          </button>
          <button onClick={() => queryOp(op || undefined)}
            disabled={disabled || busy || !taskId}
            className="px-3 py-1.5 rounded bg-slate-800 border border-slate-700 text-slate-300 disabled:opacity-40"
            title="按 operation id 查回上次候选操作的结果；不传则查最近一次">
            查回上次操作
          </button>
          {admittedCount === 0 && (
            <span className="text-slate-600">还没有已准入的材料</span>
          )}
          {busy && <span className="text-slate-500 inline-flex items-center gap-1">
            <RefreshCw className="w-3.5 h-3.5 animate-spin" /> 处理中…</span>}
        </div>
        {cand && (
          <div className="rounded-lg border border-violet-500/30 bg-violet-500/10 text-violet-200 px-2 py-1.5 space-y-1">
            <div>
              候选 {String(cand.candidate_version_id ?? '').slice(0, 12)}…
              {cand.acceptance?.overall ? ` · 本候选验收：${cand.acceptance.overall}` : ''}
              {' · '}{cand.adopted || prev?.is_adopted ? '已采纳' : '未采纳（需显式采纳）'}
            </div>
            <div className="text-violet-300/80">
              影响步骤 {(cand.affected_steps ?? []).map((s: any) => s.step_id || s.capability).join('、') || '—'}
              ；未重做 {(cand.stopped_steps ?? []).length} 个
              {cand.budget && (cand.budget.calls_left !== undefined
                ? ` · 剩余检索额度 ${cand.budget.calls_left} 次` : '')}
            </div>
            {(cand.acceptance?.gaps ?? []).length > 0 && (
              <div className="text-amber-300">
                候选缺口：{(cand.acceptance.gaps ?? []).slice(0, 3).join('；')}
              </div>
            )}
            {cand.prior_failure?.status && (
              <div className="text-amber-300">
                原任务状态：{cand.prior_failure.status}（{cand.prior_failure.phase || '—'}）——
                {String(cand.prior_failure.detail ?? '').slice(0, 80)}
                <div className="text-amber-300/70">
                  原失败记录未被修改；候选是新版本，不代表任务已成功。
                </div>
              </div>
            )}
          </div>
        )}

        {/* C 批：预览 → 比较 → 显式采纳（闭环）。采纳是按钮 + 二次确认，绝不自动。 */}
        {prev && (
          <div className="rounded-lg border border-slate-700 bg-slate-900/70 px-2 py-2 space-y-2">
            <div className="text-slate-300">
              候选预览（只读）：
              <span className="text-slate-500"> 身份 {String(prev.candidate?.identity_id ?? '').slice(0, 16)}…</span>
              {prev.is_adopted
                ? <span className="ml-2 text-emerald-400">已是当前选中版本</span>
                : <span className="ml-2 text-amber-300">未采纳</span>}
            </div>
            <div className="text-slate-400">
              与当前稿比较：改动 {(prev.comparison?.changed_lines ?? 0)} 行
              （+{prev.comparison?.added_lines ?? 0}/-{prev.comparison?.removed_lines ?? 0}）；
              字节 {prev.comparison?.adopted_bytes ?? 0} → {prev.comparison?.candidate_bytes ?? 0}；
              人工分析节{prev.comparison?.analysis_section_preserved
                ? <span className="text-emerald-400"> 原样保留</span>
                : <span className="text-amber-300"> 未保留（需人工确认）</span>}
            </div>
            {(prev.candidate?.acceptance?.gaps ?? []).length > 0 && (
              <div className="text-amber-300">
                证据缺口：{(prev.candidate.acceptance.gaps ?? []).slice(0, 5).join('；')}
              </div>
            )}
            {(prev.comparison?.diff_head ?? []).length > 0 && (
              <pre className="max-h-40 overflow-auto rounded bg-slate-950 border border-slate-800 p-2 text-xs leading-5 text-slate-400">
                {(prev.comparison.diff_head ?? []).join('\n')}
                {prev.comparison.diff_truncated ? '\n…（差异过长，已截断）' : ''}
              </pre>
            )}
            <div className="flex items-center gap-2 flex-wrap">
              <button onClick={() => setShowBody(v => !v)}
                className="px-2 py-1 rounded border border-slate-700 text-slate-300">
                {showBody ? '收起候选正文' : '查看候选正文'}
              </button>
              {!prev.is_adopted && !adoptArmed && (
                <button onClick={adoptArm} disabled={disabled || busy}
                  className="px-3 py-1.5 rounded bg-emerald-500/15 border border-emerald-500/30 text-emerald-300 disabled:opacity-40">
                  采纳这一版…
                </button>
              )}
              <span className="text-slate-500">
                采纳后对该身份重验；人工复核仍由你单独完成，机器不代写。
              </span>
            </div>
            {adoptArmed && !prev.is_adopted && (
              <div className="rounded border border-emerald-500/40 bg-emerald-500/10 text-emerald-200 px-2 py-2 space-y-1">
                <div>确认采纳这一版作为交付正文？这会切换交付的选中版本。</div>
                <div className="text-emerald-300/80">
                  身份 {String(prev.candidate?.identity_id ?? '').slice(0, 20)}…
                  · 与当前稿改动 {prev.comparison?.changed_lines ?? 0} 行
                  · 候选验收 {prev.candidate?.acceptance?.overall || '（未知）'}
                </div>
                <div className="text-emerald-300/80">
                  旧版与人工文字保留在版本库；人工复核（human_review）不继承、不改写；
                  采纳**不等于**验收通过——重验结论会如实显示。
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={adoptCandidate} disabled={disabled || busy}
                    className="px-3 py-1.5 rounded bg-emerald-500/25 border border-emerald-400/50 text-emerald-100 disabled:opacity-40">
                    确认采纳（不可自动完成）
                  </button>
                  <button onClick={() => setAdoptArmed(false)}
                    className="px-2 py-1 rounded border border-slate-600 text-slate-300">
                    取消
                  </button>
                </div>
              </div>
            )}
            {showBody && (
              <pre className="max-h-80 overflow-auto rounded bg-slate-950 border border-slate-800 p-2 text-xs leading-5 text-slate-300 whitespace-pre-wrap">
                {String(prev.body ?? '')}
              </pre>
            )}
          </div>
        )}

        {adopted && (
          <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 text-emerald-200 px-2 py-1.5 space-y-1">
            <div>
              已采纳 {String(adopted.adopted?.version_id ?? '').slice(0, 12)}…
              （此前选中 {String(adopted.previous?.version_id ?? '').slice(0, 12)}…）
            </div>
            <div className="text-emerald-300/80">
              对该身份重验：{adopted.reverify?.overall || '（未知）'}
              {adopted.reverify?.covers_adopted_identity ? '' : ' · 未绑到该身份，按待重验处理'}
              {(adopted.reverify?.gaps ?? []).length > 0
                ? `；缺口：${(adopted.reverify.gaps ?? []).slice(0, 3).join('；')}` : ''}
            </div>
            <div className="text-emerald-300/80">
              人工复核文件{adopted.human_review_untouched ? '未被改动' : '被改动了（异常，请核对）'}；
              旧批准未继承；采纳本身不等于验证通过。
              {adopted.next?.export ? ` 下一步：${adopted.next.export}` : ''}
            </div>
          </div>
        )}
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
