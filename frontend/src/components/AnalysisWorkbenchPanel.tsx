import { useCallback, useEffect, useMemo, useState } from 'react'
import { Calculator, Download, GitCompare, RotateCcw, ShieldCheck, TriangleAlert } from 'lucide-react'

/**
 * 分析工作台（K3）：**看分析卡与原始依据 → 改允许改的假设 → 确定性复算 → 比较前后 →
 * 采纳 → 同版导出**。三条与后端一致的纪律：
 *
 * - 原始观测**不可被假设覆盖**：假设只是一组参数，复算出来的是一条**新运行**（旧运行保留）；
 * - 复算结果**不会自动被采纳**：面板上"采纳"是一次显式动作，采纳后正文/清单/导出指向同一次运行；
 * - 不适用/缺输入如实显示（不给数、不落盘成已验证）；计算核心零模型调用。
 */

type RunOutput = {
  output_id?: string; metric?: string; label?: string
  value?: number; unit?: string; output_period?: string; formula?: string
  inputs?: string[]
  basis?: {
    fact_id?: string; metric?: string; period?: string; value?: number; unit?: string
    caliber?: string; currency?: string; source_url?: string; derived_from?: string[]
    formula_version?: string; verify_state?: string; resolved?: boolean
  }[]
}
type Run = {
  run_id?: string; model_id?: string; status?: string; validation_ok?: boolean
  params?: Record<string, number | string>
  allowed_params?: Record<string, [number, number] | string[]>
  dataset_hash?: string; outputs?: RunOutput[]; limits?: string[]
}
type AnalysisState = {
  ok?: boolean; reason?: string
  inputs?: { present?: string[]; dataset_hash?: string; recomputable?: boolean }
  runs?: Run[]
  cards?: { run_id?: string; model_id?: string; title?: string; value?: number; unit?: string }[]
  current_package?: string
}
type DiffRow = {
  metric?: string; label?: string
  before?: number | null; after?: number | null; unit?: string; delta?: number | null
}

const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const fmt = (v: unknown, unit = '') => {
  const n = num(v)
  return n === null ? '—' : `${n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}${unit}`
}

export default function AnalysisWorkbenchPanel({ taskId, onAdopted }: {
  taskId: string | null
  onAdopted?: () => void
}) {
  const [state, setState] = useState<AnalysisState | null>(null)
  const [modelId, setModelId] = useState('')
  const [params, setParams] = useState<Record<string, string>>({})
  const [diff, setDiff] = useState<DiffRow[] | null>(null)
  const [newRun, setNewRun] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'warn' | 'err'; text: string } | null>(null)

  const model = useMemo(
    () => (state?.runs || []).find((r) => r.model_id === modelId) || null,
    [state, modelId],
  )
  const editableKeys = useMemo(() => {
    const ap = model?.allowed_params || {}
    return Object.keys(ap).filter((k) => Array.isArray(ap[k]) && typeof (ap[k] as any)[0] === 'number')
  }, [model])

  const load = useCallback(async () => {
    if (!taskId) return
    try {
      const d = await (await fetch(`/api/task/${taskId}/analysis`)).json()
      setState(d)
      const first = (d?.runs || []).find((r: Run) => !String(r.model_id || '').startsWith('ratio:'))
      if (first) {
        setModelId((cur) => cur || String(first.model_id || ''))
        const base: Record<string, string> = {}
        for (const k of Object.keys(first.allowed_params || {})) {
          const v = (first.params || {})[k]
          if (typeof v === 'number') base[k] = String(v)
        }
        setParams((cur) => (Object.keys(cur).length ? cur : base))
      }
    } catch (e: any) {
      setMsg({ kind: 'err', text: `读取分析状态失败：${String(e?.message || e)}` })
    }
  }, [taskId])

  useEffect(() => { void load() }, [load])

  const recompute = async () => {
    if (!taskId || !modelId) return
    setBusy(true); setMsg(null); setDiff(null); setNewRun(null)
    try {
      const body: Record<string, unknown> = { model_id: modelId, params: {} }
      const p: Record<string, number> = {}
      for (const k of editableKeys) {
        const raw = params[k]
        if (raw !== undefined && raw !== '') p[k] = Number(raw)
      }
      body.params = p
      const res = await fetch(`/api/task/${taskId}/analysis/recompute`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const d = await res.json()
      if (!res.ok) {
        setMsg({ kind: 'err', text: String(d?.error || `HTTP ${res.status}`) })
        return
      }
      if (!d?.ok) {
        setMsg({ kind: 'warn', text: String(d?.message || d?.reason || '这份输入下该模型不适用') })
        return
      }
      setDiff(d.diff || []); setNewRun(d)
      setMsg({ kind: 'ok', text: '复算完成：这是一条**新运行**，尚未采纳（旧运行仍保留）' })
    } catch (e: any) {
      setMsg({ kind: 'err', text: `复算失败：${String(e?.message || e)}` })
    } finally { setBusy(false) }
  }

  const adopt = async () => {
    if (!taskId || !newRun?.run_id) return
    setBusy(true); setMsg(null)
    try {
      const res = await fetch(`/api/task/${taskId}/analysis/adopt`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: newRun.run_id }),
      })
      const d = await res.json()
      if (!res.ok) { setMsg({ kind: 'err', text: String(d?.error || `HTTP ${res.status}`) }); return }
      setMsg({ kind: 'ok', text: `已采纳：交付状态 ${d?.delivery_status || '—'}；可导出当前包` })
      await load(); onAdopted?.()
    } catch (e: any) {
      setMsg({ kind: 'err', text: `采纳失败：${String(e?.message || e)}` })
    } finally { setBusy(false) }
  }

  const exportNow = async () => {
    if (!taskId) return
    // 导出＝下载**与采纳稿同版**的包（后端按包内身份选，不用最新清单给旧包背书）
    window.open(`/files/${taskId}/${state?.current_package || ''}`, '_blank')
  }

  if (!taskId) return null
  const card = (state?.cards || [])[0]

  return (
    <div className="rounded-lg border border-slate-700 bg-slate-900/40 p-3 space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium text-slate-200">
        <Calculator className="w-4 h-4" /> 分析工作台（改假设 → 复算 → 对比 → 采纳 → 导出）
      </div>
      {!state?.ok && (
        <div className="text-xs text-amber-400 flex items-start gap-1">
          <TriangleAlert className="w-3.5 h-3.5 mt-0.5" />
          <span>{state?.reason || '该任务还没有可复算输入（analysis/dataset.json）'}</span>
        </div>
      )}
      {card && (
        <div className="text-xs text-slate-300 space-y-1">
          <div className="text-slate-400">分析卡（已验证运行）</div>
          <div className="font-mono">
            {card.title}：{fmt(card.value, card.unit || '')}（run={String(card.run_id || '').slice(0, 12)}）
          </div>
          {/* 原始依据：卡上的读数由哪些**包内观察**算出来（fact_id/期间/值/单位/口径/来源）。
              取不到就写"未解析到观察"，不编来源。 */}
          {(() => {
            const cur = (state?.runs || []).find((r) => r.run_id === card.run_id)
            const outs = cur?.outputs || []
            const rows = outs.flatMap((o) => (o.basis || []).map((b) => ({ o, b })))
            if (!rows.length) return null
            return (
              <details className="text-slate-400">
                <summary className="cursor-pointer">原始依据（{rows.length} 条观察，可回溯）</summary>
                <table className="mt-1 w-full text-left font-mono">
                  <thead className="text-slate-500">
                    <tr><th>指标</th><th>期间</th><th>值</th><th>口径</th><th>fact_id</th><th>来源</th></tr>
                  </thead>
                  <tbody>
                    {rows.slice(0, 12).map(({ o, b }, i) => (
                      <tr key={`${o.output_id}-${i}`}>
                        <td>{b.metric || '—'}</td>
                        <td>{b.period || '—'}</td>
                        <td>{fmt(b.value, b.unit || '')}</td>
                        <td>{b.caliber || '—'}</td>
                        <td title={b.fact_id || ''}>{String(b.fact_id || '').slice(0, 12)}</td>
                        <td className="truncate max-w-[16rem]" title={b.source_url || ''}>
                          {b.resolved === false ? '未解析到观察' : (b.source_url || '—')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {outs[0]?.formula && (
                  <div className="mt-1 text-slate-500">算式：{outs[0].formula}</div>
                )}
              </details>
            )
          })()}
        </div>
      )}
      {state?.ok && (
        <>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <select
              className="bg-slate-800 border border-slate-600 rounded px-2 py-1"
              value={modelId}
              onChange={(e) => { setModelId(e.target.value); setDiff(null); setNewRun(null) }}
            >
              {(state.runs || [])
                .filter((r) => !String(r.model_id || '').startsWith('ratio:'))
                .map((r) => (
                  <option key={r.run_id} value={r.model_id}>
                    {r.model_id}（{r.status}）
                  </option>
                ))}
            </select>
            {editableKeys.map((k) => (
              <label key={k} className="flex items-center gap-1 text-slate-400">
                {k}
                <input
                  className="w-20 bg-slate-800 border border-slate-600 rounded px-1 py-0.5 text-slate-100"
                  value={params[k] ?? ''}
                  onChange={(e) => setParams({ ...params, [k]: e.target.value })}
                />
                <span className="text-slate-500">
                  {Array.isArray(model?.allowed_params?.[k])
                    ? `[${(model!.allowed_params![k] as number[])[0]}, ${(model!.allowed_params![k] as number[])[1]}]`
                    : ''}
                </span>
              </label>
            ))}
            <button
              className="px-2 py-1 rounded border border-sky-500/40 text-sky-300 disabled:opacity-50"
              disabled={busy} onClick={() => void recompute()}
            >
              <RotateCcw className="w-3.5 h-3.5 inline mr-1" />确定性复算
            </button>
            <button
              className="px-2 py-1 rounded border border-emerald-500/40 text-emerald-300 disabled:opacity-50"
              disabled={busy || !newRun?.run_id} onClick={() => void adopt()}
            >
              <ShieldCheck className="w-3.5 h-3.5 inline mr-1" />采纳这版
            </button>
            <button
              className="px-2 py-1 rounded border border-slate-500/40 text-slate-300"
              onClick={() => void exportNow()}
            >
              <Download className="w-3.5 h-3.5 inline mr-1" />导出当前包
            </button>
          </div>
          {diff && diff.length > 0 && (
            <div className="text-xs">
              <div className="text-slate-400 flex items-center gap-1">
                <GitCompare className="w-3.5 h-3.5" />前后对比（新运行 vs 上一条同模型已验证运行）
              </div>
              <table className="mt-1 w-full text-left font-mono">
                <thead className="text-slate-500">
                  <tr><th>指标</th><th>改前</th><th>改后</th><th>变化</th></tr>
                </thead>
                <tbody>
                  {diff.map((d) => (
                    <tr key={String(d.metric || d.label)}>
                      <td>{d.label || d.metric}</td>
                      <td>{fmt(d.before, d.unit || '')}</td>
                      <td>{fmt(d.after, d.unit || '')}</td>
                      <td>{fmt(d.delta, d.unit || '')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
      {msg && (
        <div className={`text-xs ${msg.kind === 'ok' ? 'text-emerald-400'
          : msg.kind === 'warn' ? 'text-amber-400' : 'text-red-400'}`}>
          {msg.text}
        </div>
      )}
      {state?.current_package && (
        <div className="text-xs text-slate-500">
          当前包：{state.current_package}（与采纳稿同版才可下载）
        </div>
      )}
    </div>
  )
}
