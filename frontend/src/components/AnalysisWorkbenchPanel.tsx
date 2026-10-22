import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Calculator, Download, GitCompare, RotateCcw, ShieldCheck, TriangleAlert } from 'lucide-react'
import { useAnalysisPanel, usePackageState } from '../lib/analysisState'
import { packageLine } from '../lib/packageState'

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
  /** 默认值可能是数值（范围参数）或字符串（**选择型**参数，如 rounding/below_gross_mode） */
  default_params?: Record<string, number | string>
  dataset_hash?: string; outputs?: RunOutput[]; limits?: string[]
  selected?: boolean; selection_state?: string; selection_why?: string
  started_at?: string; rules_version?: string
}
/** U90-C：注册模型清单（含**本任务还没算过**的情景模型）——"改一个情景参数"的入口。 */
type ModelSpec = {
  id?: string; label?: string; ran?: boolean
  allowed_params?: Record<string, [number, number] | string[]>
  default_params?: Record<string, number | string>
}
type SelectionEntry = {
  model_id?: string; run_id?: string; state?: string; why?: string
  params?: Record<string, number>
}
type AnalysisState = {
  ok?: boolean; reason?: string
  inputs?: { present?: string[]; dataset_hash?: string; recomputable?: boolean
    /** 本次任务留存的可用事实来源（working_paper/financials/空）——补算按钮据此显隐 */
    source?: string }
  /** U90-C：没有可复算输入时的**可行动出口**——can_freeze=按现有事实确定性补算 */
  can_freeze?: boolean
  next_action?: 'freeze' | 'material' | 'recompute' | ''
  runs?: Run[]
  /** U90-C：注册的非比率模型（含没算过的情景模型）——模型选择器的选项来源之一 */
  models?: ModelSpec[]
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

export default function AnalysisWorkbenchPanel({ taskId, exportPackage, onAdopted }: {
  taskId: string | null
  /** P0-4：任务详情 payload 里的 `export.package`——与本面板的 analysis 状态一起喂给
   *  usePackageState（同一个纯函数、同一组输入），「导出与复核」面板因此与本行必然一致。 */
  exportPackage?: string
  onAdopted?: () => void
}) {
  // P0-4：分析状态**只有一份**（lib/analysisState 的模块级缓存）——
  // 此前本面板自己 fetch 一份、导出面板读另一份，同一屏上才会出现
  // 「当前包: deliverables_*.zip」与「本次任务暂无交付包」并存。
  const { data, status: loadState, error: loadErr, reload } = useAnalysisPanel(taskId)
  const pkg = usePackageState(taskId, exportPackage)
  const [modelId, setModelId] = useState('')
  const [params, setParams] = useState<Record<string, string>>({})
  const [chosenRunId, setChosenRunId] = useState('')
  const [diff, setDiff] = useState<DiffRow[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'warn' | 'err'; text: string } | null>(null)
  // 复算出的**新运行**在本地并入展示（服务端 payload 不动，采纳后由 reload 换成服务端清单）
  const [localRuns, setLocalRuns] = useState<Run[]>([])
  const state = useMemo<AnalysisState | null>(() => {
    const base = (data || null) as AnalysisState | null
    if (!base) return null
    if (!localRuns.length) return base
    const kept = (base.runs || []).filter(r => !localRuns.some(lr => lr.run_id && lr.run_id === r.run_id))
    return { ...base, runs: [...kept, ...localRuns] }
  }, [data, localRuns])

  // 任务切换必须**重置**草稿与比较状态（L0-b-5）：否则会把上一个任务的假设/差异
  // 显示在新任务上，读者以为那是本任务的读数。
  useEffect(() => {
    setLocalRuns([]); setModelId(''); setParams({}); setChosenRunId('')
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
  // U90-C：选择器 = 已有运行的模型 ∪ 注册模型（含**本任务还没算过**的情景模型）。
  // 没有这一条，用户就没有"改一个情景参数"的入口：模型进不了选择器 → 复算按钮无从点。
  const availableModels = useMemo(() => {
    const fromRuns = panelRuns.map((r) => String(r.model_id || ''))
    const fromSpecs = (state?.models || []).map((m) => String(m.id || ''))
    return Array.from(new Set([...fromRuns, ...fromSpecs])).filter(Boolean)
  }, [panelRuns, state])
  const ranModelIds = useMemo(
    () => new Set(panelRuns.map((r) => String(r.model_id || ''))),
    [panelRuns],
  )
  const specModel = useMemo(
    () => (state?.models || []).find((m) => String(m.id || '') === modelId) || null,
    [state, modelId],
  )
  // 已算过的模型用运行自带的参数声明；**没算过**的模型用注册声明（同一套 allowed_params），
  // 面板据此渲染可改的假设——参数边界仍由后端复算接口再次校验（400 越界）。
  const model = useMemo<Run | null>(() => {
    if (modelRuns[0]) return modelRuns[0]
    if (!specModel) return null
    return {
      model_id: String(specModel.id || ''), status: '', run_id: '',
      allowed_params: specModel.allowed_params,
      default_params: specModel.default_params,
    }
  }, [modelRuns, specModel])
  const chosen = useMemo(
    () => modelRuns.find((r) => r.run_id === chosenRunId) || null,
    [modelRuns, chosenRunId],
  )
  // 可改的假设 = 声明过的**范围参数**（数值上下界）与**选择型参数**（字符串候选）。
  // 二者都是"假设"，改它们只产生**新运行**，不覆盖原始观测。
  const editableKeys = useMemo(() => {
    const ap = model?.allowed_params || {}
    return Object.keys(ap).filter((k) => Array.isArray(ap[k]) && (ap[k] as any).length > 0)
  }, [model])
  const isChoiceParam = useCallback(
    (k: string) => Array.isArray(model?.allowed_params?.[k])
      && typeof (model!.allowed_params![k] as any)[0] !== 'number',
    [model],
  )

  const paramsFor = useCallback((r: Run | null): Record<string, string> => {
    const base: Record<string, string> = {}
    const ap = r?.allowed_params || {}
    for (const k of Object.keys(ap)) {
      if (!Array.isArray(ap[k]) || (ap[k] as any).length === 0) continue
      const v = (r?.params || {})[k]
      // 运行没记这个参数 → 显示模型**声明的默认值**（不让用户猜"不改会用什么"）
      const dft = (r?.default_params || {})[k]
      const use = v !== undefined && v !== null ? v : dft
      base[k] = (typeof use === 'number' || typeof use === 'string') ? String(use) : ''
    }
    return base
  }, [])

  // 读取成功后**初始化一次**选择：默认选中"进正文的那一条"（有选择记录时），
  // 没有就取该模型第一条运行。用 ref 记住已初始化过的那份服务端 payload ——
  // 复算只往 localRuns 里加东西，不能把用户刚改的参数草稿重置回去（X0）。
  const initedFor = useRef<unknown>(null)
  useEffect(() => {
    if (!data?.ok || initedFor.current === data) return
    initedFor.current = data
    const runs: Run[] = (data?.runs || []).filter(
      (r: Run) => !String(r.model_id || '').startsWith('ratio:'),
    )
    const first = runs[0]
    if (!first) return
    const mid = String(first.model_id || '')
    setModelId(mid)
    const sameModel = runs.filter((r) => String(r.model_id || '') === mid)
    const sel = sameModel.find((r) => r.selected) || sameModel[0]
    setChosenRunId(String(sel?.run_id || ''))
    setParams(paramsFor(sel))
  }, [data, paramsFor])

  // U90-C：**补算分析输入**——工作区留有本次任务的事实来源（研究底稿/财务载荷）而
  // 分析步没跑（或历史工作区被清理后重新落地）时，按现有事实确定性冻结数据集并复算
  // 注册模型（零模型调用、可复现）。契约要素不全 → 后端 409 带原文缺口：如实显示，
  // 绝不降门槛、绝不用估计值补披露数。
  const freezeNow = async () => {
    if (!taskId) return
    setBusy(true); setMsg(null)
    try {
      const res = await fetch(`/api/task/${taskId}/analysis/freeze`, { method: 'POST' })
      const d = await res.json()
      if (!res.ok) {
        setMsg({ kind: 'warn', text: `补算被拒绝（未写入任何数据）：${String(d?.error || `HTTP ${res.status}`)}` })
        return
      }
      if (d?.already) {
        setMsg({ kind: 'ok', text: String(d?.note || '该任务已有可复算输入，未重算') })
      } else {
        const r = d?.result || {}
        const gaps = Array.isArray(r.gaps) ? r.gaps : []
        setMsg({
          kind: 'ok',
          text: `补算完成：${r.runs_validated ?? 0}/${r.runs_total ?? 0} 个模型产出已验证运行`
            + (gaps.length ? `；资料不足的模型如实留缺口：${gaps.join('；')}` : ''),
        })
      }
      await reload(); onAdopted?.()
    } catch (e: any) {
      setMsg({ kind: 'err', text: `补算失败：${String(e?.message || e)}` })
    } finally { setBusy(false) }
  }

  const recompute = async () => {
    if (!taskId || !modelId) return
    setBusy(true); setMsg(null); setDiff(null)
    try {
      const body: Record<string, unknown> = { model_id: modelId, params: {} }
      const p: Record<string, number | string> = {}
      for (const k of editableKeys) {
        const raw = params[k]
        if (raw === undefined || raw === '') continue
        // **范围参数**转数值（越界由后端 400 拦）；**选择型参数**按候选原文提交，
        // 不把 "yi_2" 这种候选值 Number() 成 NaN（那会被后端判成参数不合法）。
        if (isChoiceParam(k)) {
          p[k] = raw
        } else {
          const n = Number(raw)
          if (Number.isFinite(n)) p[k] = n
        }
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
        // 只并入本地展示（服务端 payload 不动）：保留当前模型选择与参数草稿，选中新运行
        setLocalRuns((prev) => [...prev.filter((r) => r.run_id !== newRun.run_id), newRun])
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
      await reload(); onAdopted?.()
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
      // P0-4：包名取 usePackageState 的解析结果（与「导出与复核」面板同一函数、同一输入）
      let name = String(pkg.res.name || '')
      if (!name) {
        const res = await fetch(`/api/task/${taskId}/package`, { method: 'POST' })
        const d = await res.json()
        if (!res.ok || !d?.package) {
          setMsg({ kind: 'err', text: `生成当前包失败：${String(d?.error || `HTTP ${res.status}`)}` })
          return
        }
        name = String(d.package)
        await reload()
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
    <div className="rounded-lg border border-line-strong bg-surface/40 p-3 space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium text-ink">
        <Calculator className="w-4 h-4" /> 分析工作台（选运行 → 改假设 → 复算 → 对比 → 采纳 → 导出）
      </div>
      {loadState === 'loading' && (
        <div className="text-xs text-ink-faint">正在读取分析状态…（最多等 12 秒）</div>
      )}
      {loadState === 'error' && (
        <div className="text-xs border border-red-500/30 bg-red-500/10 text-red-700 rounded-lg px-3 py-2 flex items-center justify-between gap-2">
          <span className="inline-flex items-start gap-1.5">
            <TriangleAlert className="w-3.5 h-3.5 mt-0.5" />
            <span>分析状态读取失败：{loadErr || '连接中断'}（这不代表本任务没有可复算输入）</span>
          </span>
          <button onClick={() => void reload()}
            className="text-xs px-2 py-1 rounded bg-red-500/20 hover:bg-red-500/30 shrink-0">
            重试
          </button>
        </div>
      )}
      {loadState === 'ok' && !state?.ok && (
        <div className="text-xs text-amber-700 flex items-start gap-1">
          <TriangleAlert className="w-3.5 h-3.5 mt-0.5" />
          <span>{state?.reason || '该任务还没有可复算输入（analysis/dataset.json）'}</span>
        </div>
      )}
      {/* U90-C：有报告却无可复算分析输入时，给一个**可行动出口**——
          工作区留有本次任务的事实来源（底稿/财务载荷）就按现有事实确定性补算；
          契约要素不全 → 补研究请求；连事实来源都没有 → 补材料。 */}
      {loadState === 'ok' && !state?.ok && (state?.can_freeze || state?.inputs?.source) && (
        <div className="flex items-center gap-2">
          <button
            disabled={busy} onClick={() => void freezeNow()}
            title="按本任务现有留存的公开资料冻结数据集并复算注册模型（确定性、零模型调用）"
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-emerald-500/10 text-emerald-700 border border-emerald-500/30 hover:bg-emerald-500/20 disabled:opacity-60"
          >
            {busy ? '补算中…' : '补算分析输入'} <Calculator className="w-3 h-3" />
          </button>
          {state?.inputs?.source && (
            <span className="text-ink-faint">来源：{state.inputs.source === 'working_paper' ? '研究底稿 working_paper.json' : '财务载荷 financials.json'}</span>
          )}
        </div>
      )}
      {!state?.ok && state?.next_action === 'material' && (
        <div className="text-xs text-ink-muted">
          工作区没有可用的分析事实来源：请在本页「补材料」上传该公司的公开财务资料（年报/摘要），
          补齐后再点「补算分析输入」。缺口如实保留，不会用估计值代替披露数。
        </div>
      )}
      {card && (
        <div className="text-xs text-ink space-y-1">
          <div className="text-ink-muted">
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
              <details className="text-ink-muted">
                <summary className="cursor-pointer">原始依据（{rows.length} 条观察，可回溯）</summary>
                <table className="mt-1 w-full text-left font-mono">
                  <thead className="text-ink-faint">
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
                  <div className="mt-1 text-ink-faint">算式：{outs[0].formula}</div>
                )}
              </details>
            )
          })()}
        </div>
      )}
      {selEntries.length > 0 && (
        <div className="text-xs text-ink-muted space-y-0.5">
          <div>
            进正文的选择（{selEntries.length} 条，规则版本 {state?.rules_version || '—'}）：
            {selEntries.map((e) => `${e.model_id}=${String(e.run_id || '').slice(0, 12)}`).join('、')}
          </div>
          {staleEntries.map((e) => (
            <div key={`${e.model_id}-${e.run_id}`} className="text-amber-700">
              ⚠️ {e.model_id} 的选择已不可用：{e.why || e.state}
            </div>
          ))}
        </div>
      )}
      {state?.ok && (
        <>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <select
              className="bg-surface-raised border border-line-strong rounded px-2 py-1"
              value={modelId}
              onChange={(e) => {
                const mid = e.target.value
                setModelId(mid)
                setDiff(null)
                const same = panelRuns.filter((r) => String(r.model_id || '') === mid)
                const sel = same.find((r) => r.selected) || same[0] || null
                // 该模型还没算过（U90-C 情景模型）：没有运行可选，参数用**注册默认值**起稿，
                // 复算后才会出现第一条运行。
                setChosenRunId(String(sel?.run_id || ''))
                setParams(sel ? paramsFor(sel) : paramsFor(model))
              }}
            >
              {availableModels.map((mid) => (
                <option key={mid} value={mid}>
                  {mid}{ranModelIds.has(mid) ? '' : '｜未算过'}
                </option>
              ))}
            </select>
            {/* 同模型的历史运行可区分：选哪一条进正文是**显式选择**；
              还没算过的模型没有运行可选——如实提示下一步，不显示空菜单 */}
            {modelRuns.length > 0 ? (
              <select
                className="bg-surface-raised border border-line-strong rounded px-2 py-1"
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
            ) : (
              <span className="text-ink-faint">
                该模型还没算过：改参数后点「确定性复算」生成新运行
              </span>
            )}
            {editableKeys.map((k) => {
              const range = Array.isArray(model?.allowed_params?.[k])
                && typeof (model!.allowed_params![k] as any)[0] === 'number'
                ? (model!.allowed_params![k] as number[]) : null
              const choices = !range && Array.isArray(model?.allowed_params?.[k])
                ? (model!.allowed_params![k] as string[]) : null
              return (
                <label key={k} className="flex items-center gap-1 text-ink-muted">
                  {k}
                  {choices ? (
                    /* **选择型参数**（如 rounding）：候选即边界，下拉选择，不自由输入 */
                    <select
                      className="bg-surface-raised border border-line-strong rounded px-1 py-0.5 text-ink"
                      value={params[k] ?? ''}
                      onChange={(e) => setParams({ ...params, [k]: e.target.value })}
                    >
                      {choices.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                  ) : (
                    <input
                      className="w-20 bg-surface-raised border border-line-strong rounded px-1 py-0.5 text-ink"
                      value={params[k] ?? ''}
                      placeholder={String(model?.default_params?.[k] ?? '')}
                      onChange={(e) => setParams({ ...params, [k]: e.target.value })}
                    />
                  )}
                  <span className="text-ink-faint">
                    {range
                      ? `[${range[0]}, ${range[1]}]`
                      : ''}
                    {typeof model?.default_params?.[k] === 'number'
                      ? ` 默认 ${model!.default_params![k]}` : ''}
                  </span>
                </label>
              )
            })}
            <button
              className="px-2 py-1 rounded border border-sky-500/40 text-sky-700 disabled:opacity-50"
              disabled={busy} onClick={() => void recompute()}
            >
              <RotateCcw className="w-3.5 h-3.5 inline mr-1" />确定性复算
            </button>
            <button
              className="px-2 py-1 rounded border border-emerald-500/40 text-emerald-700 disabled:opacity-50"
              disabled={busy || !chosenRunId || chosen?.status !== 'validated' || chosen?.selected}
              title={chosen?.selected ? '这一条已经在正文里' : '把所选运行选定为进正文的那一条'}
              onClick={() => void adopt()}
            >
              <ShieldCheck className="w-3.5 h-3.5 inline mr-1" />采纳这版
            </button>
            <button
              className="px-2 py-1 rounded border border-line-strong/40 text-ink disabled:opacity-50"
              disabled={busy}
              onClick={() => void exportNow()}
            >
              <Download className="w-3.5 h-3.5 inline mr-1" />导出当前包
            </button>
          </div>
          {diff && diff.length > 0 && (
            <div className="text-xs">
              <div className="text-ink-muted flex items-center gap-1">
                <GitCompare className="w-3.5 h-3.5" />前后对比（新运行 vs 所选基准运行）
              </div>
              <table className="mt-1 w-full text-left font-mono">
                <thead className="text-ink-faint">
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
        <div className={`text-xs ${msg.kind === 'ok' ? 'text-emerald-700'
          : msg.kind === 'warn' ? 'text-amber-700' : 'text-red-700'}`}>
          {msg.text}
        </div>
      )}
      {/* P0-4：这一行与「导出与复核」面板的说明来自**同一次 resolvePackage 调用**
          （packageLine / exportPanelNote 同一模块），两边不可能再各说一套。 */}
      <div className="text-xs text-ink-faint">
        {packageLine(pkg.res, { adoptedIdentity: state?.adopted_identity })}
      </div>
    </div>
  )
}
