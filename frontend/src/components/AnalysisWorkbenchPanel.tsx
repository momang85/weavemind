import { useCallback, useEffect, useMemo, useState } from 'react'
import { Calculator, Download, GitCompare, RotateCcw, ShieldCheck, TriangleAlert } from 'lucide-react'

/**
 * 分析工作台（K3 + L0-b）：**看分析卡与原始依据 → 选哪一条运行进正文 → 改允许改的假设 →
 * 确定性复算 → 比较前后 → 采纳 → 同版导出**。与后端一致的纪律：
 *
 * - 原始观测**不可被假设覆盖**：假设只是一组参数，复算出来的是一条**新运行**（旧运行保留）；
 * - 复算结果**不会自动被采纳**：面板上"采纳"是一次显式动作，采纳后正文/清单/导出指向
 *   **所选的那一次运行**（缺 run_id 不接受默认采纳，后端 400）；
 * - 不适用/缺输入如实显示（不给数、不落盘成已验证）；计算核心零模型调用；
 * - 导出只走"与采纳稿同版"的**当前包**：没有当前包就先按当前版本生成，再下载匹配包；
 *   空/失败就不打开任何目录。
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
  default_params?: Record<string, number>
  dataset_hash?: string; outputs?: RunOutput[]; limits?: string[]
  selected?: boolean; selection_state?: string; selection_why?: string
  started_at?: string; rules_version?: string
}
type SelectionEntry = {
  model_id?: string; run_id?: string; state?: string; why?: string
  params?: Record<string, number>
}
type AnalysisState = {
  ok?: boolean; reason?: string
  inputs?: { present?: string[]; dataset_hash?: string; recomputable?: boolean }
  runs?: Run[]
  cards?: { run_id?: string; model_id?: string; title?: string; value?: number; unit?: string
    in_report?: boolean }[]
  current_package?: string
  current_package_ok?: boolean
  package_note?: string
  adopted_identity?: string
  rules_version?: string
  selection?: { ok?: boolean; entries?: SelectionEntry[]; stale?: SelectionEntry[] }
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
  const [chosenRunId, setChosenRunId] = useState('')
  const [diff, setDiff] = useState<DiffRow[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'warn' | 'err'; text: string } | null>(null)

  // 任务切换必须**重置**草稿与比较状态（L0-b-5）：否则会把上一个任务的假设/差异
  // 显示在新任务上，读者以为那是本任务的读数。
  useEffect(() => {
    setState(null); setModelId(''); setParams({}); setChosenRunId('')
    setDiff(null); setMsg(null)
  }, [taskId])

  const panelRuns = useMemo(
    () => (state?.runs || []).filter((r) => !String(r.model_id || '').startsWith('ratio:')),
    [state],
  )
  const modelRuns = useMemo(
    () => panelRuns.filter((r) => String(r.model_id || '') === modelId),
    [panelRuns, modelId],
  )
  const model = useMemo(() => modelRuns[0] || null, [modelRuns])
  const chosen = useMemo(
    () => modelRuns.find((r) => r.run_id === chosenRunId) || null,
    [modelRuns, chosenRunId],
  )
  const editableKeys = useMemo(() => {
    const ap = model?.allowed_params || {}
    return Object.keys(ap).filter((k) => Array.isArray(ap[k]) && typeof (ap[k] as any)[0] === 'number')
  }, [model])

  const paramsFor = useCallback((r: Run | null): Record<string, string> => {
    const base: Record<string, string> = {}
    const ap = r?.allowed_params || {}
    for (const k of Object.keys(ap)) {
      if (!Array.isArray(ap[k]) || typeof (ap[k] as any)[0] !== 'number') continue
      const v = (r?.params || {})[k]
      // 运行没记这个参数 → 显示模型**声明的默认值**（不让用户猜"不改会用什么"）
      const dft = (r?.default_params || {})[k]
      const use = typeof v === 'number' ? v : dft
      base[k] = typeof use === 'number' ? String(use) : ''
    }
    return base
  }, [])

  const load = useCallback(async () => {
    if (!taskId) return
    try {
      const d = await (await fetch(`/api/task/${taskId}/analysis`)).json()
      setState(d)
      const runs: Run[] = (d?.runs || []).filter(
        (r: Run) => !String(r.model_id || '').startsWith('ratio:'),
      )
      const first = runs[0]
      if (first) {
        const mid = String(first.model_id || '')
        setModelId(mid)
        const sameModel = runs.filter((r) => String(r.model_id || '') === mid)
        // 默认选中"进正文的那一条"（有选择记录时）；没有就取该模型第一条运行
        const sel = sameModel.find((r) => r.selected) || sameModel[0]
        setChosenRunId(String(sel?.run_id || ''))
        setParams(paramsFor(sel))
      }
    } catch (e: any) {
      setMsg({ kind: 'err', text: `读取分析状态失败：${String(e?.message || e)}` })
    }
  }, [taskId, paramsFor])

  useEffect(() => { void load() }, [load])

  const recompute = async () => {
    if (!taskId || !modelId) return
    setBusy(true); setMsg(null); setDiff(null)
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
      setDiff(d.diff || [])
      // X0（10-02 实测）：**把新运行并入菜单并保留所选模型**。此前只 setChosenRunId，
      // state.runs 里没有这条新运行，`chosen` 找不到 → 采纳按钮禁用、菜单仍只有旧 run
      // （真实 UI 里必须整页 reload 才能选到新运行）。这里做**最小局部合并**：
      // 保留当前模型选择与参数草稿，只把新运行追加进列表并选中它。
      const newRun: Run = {
        run_id: String(d.run_id || ''),
        model_id: String(d.model_id || modelId),
        status: String(d.status || ''),
        validation_ok: String(d.status || '') === 'validated',
        params: (d.params || p) as Record<string, number | string>,
        dataset_hash: String(d.dataset_hash || ''),
        outputs: (d.outputs || []) as RunOutput[],
        selected: false,
        selection_state: 'recomputed',
      }
      if (newRun.run_id) {
        setState((prev) => {
          if (!prev) return prev
          const runs = (prev.runs || []).filter((r) => r.run_id !== newRun.run_id)
          return { ...prev, runs: [...runs, newRun] }
        })
        setChosenRunId(newRun.run_id)
        // 参数草稿保持用户刚输入的值（不要被"运行自带参数"覆盖回去）
        setParams((prev) => ({ ...prev }))
      }
      setMsg({ kind: 'ok', text: '复算完成：这是一条**新运行**，尚未采纳（旧运行仍保留）' })
    } catch (e: any) {
      setMsg({ kind: 'err', text: `复算失败：${String(e?.message || e)}` })
    } finally { setBusy(false) }
  }

  const adopt = async () => {
    if (!taskId || !chosenRunId) return
    setBusy(true); setMsg(null)
    try {
      const res = await fetch(`/api/task/${taskId}/analysis/adopt`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: chosenRunId }),
      })
      const d = await res.json()
      if (!res.ok) { setMsg({ kind: 'err', text: String(d?.error || `HTTP ${res.status}`) }); return }
      const ident = String(d?.identity_id || '')
      if (!d?.ok) {
        setMsg({ kind: 'warn', text: `选择已记录，但交付仍为 ${d?.delivery_status || 'draft'}：${d?.reason || ''}` })
      } else {
        setMsg({ kind: 'ok', text: `已采纳：报告身份 ${ident.slice(0, 12) || '—'}；可导出当前包` })
      }
      await load(); onAdopted?.()
    } catch (e: any) {
      setMsg({ kind: 'err', text: `采纳失败：${String(e?.message || e)}` })
    } finally { setBusy(false) }
  }

  const exportNow = async () => {
    if (!taskId) return
    // 导出＝下载**与采纳稿同版**的包（后端按包内身份选，不用最新 zip 给旧包背书）。
    // 没有当前包时先按当前版本**生成**一个；生成失败/仍为空 → 不打开任何目录。
    try {
      setBusy(true)
      let name = String(state?.current_package || '')
      if (!name) {
        const res = await fetch(`/api/task/${taskId}/package`, { method: 'POST' })
        const d = await res.json()
        if (!res.ok || !d?.package) {
          setMsg({ kind: 'err', text: `生成当前包失败：${String(d?.error || `HTTP ${res.status}`)}` })
          return
        }
        name = String(d.package)
        await load()
      }
      if (!name) { setMsg({ kind: 'err', text: '没有与采纳稿同版的当前包，未打开任何目录' }); return }
      window.open(`/files/${encodeURIComponent(taskId)}/${encodeURIComponent(name)}`, '_blank')
    } catch (e: any) {
      setMsg({ kind: 'err', text: `导出失败：${String(e?.message || e)}` })
    } finally { setBusy(false) }
  }

  if (!taskId) return null
  const card = (state?.cards || [])[0]
  const selEntries = state?.selection?.entries || []
  const staleEntries = state?.selection?.stale || []

  return (
    <div className="rounded-lg border border-slate-700 bg-slate-900/40 p-3 space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium text-slate-200">
        <Calculator className="w-4 h-4" /> 分析工作台（选运行 → 改假设 → 复算 → 对比 → 采纳 → 导出）
      </div>
      {!state?.ok && (
        <div className="text-xs text-amber-400 flex items-start gap-1">
          <TriangleAlert className="w-3.5 h-3.5 mt-0.5" />
          <span>{state?.reason || '该任务还没有可复算输入（analysis/dataset.json）'}</span>
        </div>
      )}
      {card && (
        <div className="text-xs text-slate-300 space-y-1">
          <div className="text-slate-400">
            分析卡（已验证运行）{card.in_report ? '：**进正文的这一条**' : '：未进正文'}
          </div>
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
      {selEntries.length > 0 && (
        <div className="text-xs text-slate-400 space-y-0.5">
          <div>
            进正文的选择（{selEntries.length} 条，规则版本 {state?.rules_version || '—'}）：
            {selEntries.map((e) => `${e.model_id}=${String(e.run_id || '').slice(0, 12)}`).join('、')}
          </div>
          {staleEntries.map((e) => (
            <div key={`${e.model_id}-${e.run_id}`} className="text-amber-400">
              ⚠️ {e.model_id} 的选择已不可用：{e.why || e.state}
            </div>
          ))}
        </div>
      )}
      {state?.ok && (
        <>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <select
              className="bg-slate-800 border border-slate-600 rounded px-2 py-1"
              value={modelId}
              onChange={(e) => {
                const mid = e.target.value
                setModelId(mid)
                setDiff(null)
                const same = panelRuns.filter((r) => String(r.model_id || '') === mid)
                const sel = same.find((r) => r.selected) || same[0]
                setChosenRunId(String(sel?.run_id || ''))
                setParams(paramsFor(sel))
              }}
            >
              {Array.from(new Set(panelRuns.map((r) => String(r.model_id || '')))).map((mid) => (
                <option key={mid} value={mid}>{mid}</option>
              ))}
            </select>
            {/* 同模型的历史运行可区分：选哪一条进正文是**显式选择** */}
            <select
              className="bg-slate-800 border border-slate-600 rounded px-2 py-1"
              value={chosenRunId}
              onChange={(e) => {
                const rid = e.target.value
                setChosenRunId(rid)
                setDiff(null)
                const r = modelRuns.find((x) => x.run_id === rid) || null
                setParams(paramsFor(r))
              }}
            >
              {modelRuns.map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {String(r.run_id || '').slice(0, 12)}｜{r.status}
                  {r.selected ? '｜进正文' : ''}
                  {r.started_at ? `｜${String(r.started_at).slice(0, 19)}` : ''}
                </option>
              ))}
            </select>
            {editableKeys.map((k) => (
              <label key={k} className="flex items-center gap-1 text-slate-400">
                {k}
                <input
                  className="w-20 bg-slate-800 border border-slate-600 rounded px-1 py-0.5 text-slate-100"
                  value={params[k] ?? ''}
                  placeholder={String(model?.default_params?.[k] ?? '')}
                  onChange={(e) => setParams({ ...params, [k]: e.target.value })}
                />
                <span className="text-slate-500">
                  {Array.isArray(model?.allowed_params?.[k])
                    ? `[${(model!.allowed_params![k] as number[])[0]}, ${(model!.allowed_params![k] as number[])[1]}]`
                    : ''}
                  {typeof model?.default_params?.[k] === 'number'
                    ? ` 默认 ${model!.default_params![k]}` : ''}
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
              disabled={busy || !chosenRunId || chosen?.status !== 'validated' || chosen?.selected}
              title={chosen?.selected ? '这一条已经在正文里' : '把所选运行选定为进正文的那一条'}
              onClick={() => void adopt()}
            >
              <ShieldCheck className="w-3.5 h-3.5 inline mr-1" />采纳这版
            </button>
            <button
              className="px-2 py-1 rounded border border-slate-500/40 text-slate-300 disabled:opacity-50"
              disabled={busy}
              onClick={() => void exportNow()}
            >
              <Download className="w-3.5 h-3.5 inline mr-1" />导出当前包
            </button>
          </div>
          {diff && diff.length > 0 && (
            <div className="text-xs">
              <div className="text-slate-400 flex items-center gap-1">
                <GitCompare className="w-3.5 h-3.5" />前后对比（新运行 vs 所选基准运行）
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
      <div className="text-xs text-slate-500">
        {state?.current_package
          ? `当前包：${state.current_package}（与采纳稿同版才可下载）`
          : (state?.adopted_identity
            ? `尚无与当前采纳稿（${String(state.adopted_identity).slice(0, 12)}）同版的包：点"导出当前包"会先生成`
            : (state?.package_note ? `交付包：${state.package_note}` : ''))}
      </div>
    </div>
  )
}
