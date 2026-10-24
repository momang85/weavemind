// 公司研究页（W1-COMPANY-1024）
//
// 一件事：把「这一份冻结记录里的这家公司」摊开——数据身份、关键财务、本次筛选条件读数、
// 判定与理由、逐条证据。**联合定位 code + run/dataset**：只给代码 → 「选择输入」候选
// （绝不按代码自动打开不明来源记录）；只有数据集 → 只给已有字段，不给条件命中/缺口结论。
// 三态分离：机器判定只是状态，人工复核永远「未开始」，本页不提供任何复核按钮。

import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, Building2, CheckCircle2, ChevronDown, ChevronRight,
  ExternalLink, Eye, Hash, Loader2, Plus, ShieldCheck,
} from 'lucide-react'
import {
  Alert, Card, EmptyState, ErrorState, LoadingBlock, SectionTitle, StatCard,
} from '../components/ui'

// ── 后端契约（与 web_ui._get_screen_company 同形） ─────────────────────

interface CompanyIdentity {
  run_id?: string; result_hash?: string; snapshot_id?: string
  as_of?: string; created_at?: string; report_period?: string
  impl_version?: string; field_map_version?: string
  usable?: boolean; integrity?: string
  dataset_id?: string; name?: string; source_label?: string
  official?: boolean; user_provided?: boolean; synthetic?: boolean
  provenance?: Record<string, unknown>; provenance_kind?: string
  periods?: string[]
}
interface CompanyFinancial {
  metric: string; metric_label: string; period: string; period_kind?: string
  value: number | null; unit?: string; currency?: string; caliber?: string
  state?: string; source_url?: string; fact_id?: string
}
interface CompanyReading {
  field: string; label: string; status: string; value: number | null
  unit?: string; display?: string; formula?: string; reason?: string
}
// 数据集分支的后端把 metric_label 直接填成字段键（revenue/gross_profit…），run 分支才是中文标签；
// 这里对已知字段补一份展示同义词，与 run 视图保持一致（只影响表头文字，不改变任何取值/口径）。
const METRIC_LABEL: Record<string, string> = {
  revenue: '营业收入',
  gross_profit: '毛利润',
  net_profit: '归母净利润',
  operating_cashflow: '经营活动现金流净额',
  total_assets: '总资产',
  total_liabilities: '总负债',
}
const metricLabel = (f: { metric: string; metric_label?: string }) => {
  const given = (f.metric_label || '').trim()
  if (given && given !== f.metric) return given
  return METRIC_LABEL[f.metric] || given || f.metric
}

interface CompanyCandidate {
  run_id: string; created_at: string; as_of: string
  counts?: { pass?: number; fail?: number; insufficient?: number }
  usable?: boolean; source_label?: string
}
interface DatasetMeta {
  dataset_id: string; name?: string; created_at?: string
  official?: boolean; user_provided?: boolean; synthetic?: boolean; rows?: number
}
interface CompanyWatch {
  in_watchlist?: boolean; version?: number; note?: string; baseline_run?: string
}
interface CompanyPayload {
  status: string
  company?: { code: string; name?: string }
  watch?: CompanyWatch
  as_of?: string
  identity?: CompanyIdentity | null
  financials?: CompanyFinancial[]
  conditions?: CompanyReading[]
  verdict?: 'pass' | 'fail' | 'insufficient'
  eval_period?: string
  reasons?: string[]
  gaps?: string[]
  conflicts?: string[]
  pending_pit?: Record<string, unknown>[]
  evidence?: { endpoint: string; query: { run: string; entity: string } } | null
  note?: string
  error?: string
  candidates?: CompanyCandidate[]
}
interface EvInput {
  fact_id: string; metric?: string; metric_label?: string; period?: string
  value: number | null; unit?: string; currency?: string; caliber?: string
  as_of?: string; state?: string; source_url?: string
  verify_state?: string
}
interface EvReading {
  field: string; label?: string; status?: string; display?: string
  formula?: string; reason?: string
  inputs: EvInput[]; unresolved_inputs?: string[]
}
interface Evidence {
  run_id?: string; result_hash?: string; snapshot_id?: string; as_of?: string
  entity?: string; entity_id?: string; integrity?: string
  readings: EvReading[]
  notes?: string[]
}

// ── 状态徽章（与 StockScreen 同一套中文标签，不显示英文枚举） ───────────

const VERDICT_LABEL: Record<string, string> = { pass: '通过', fail: '未通过', insufficient: '资料不足' }
const VERDICT_CLASS: Record<string, string> = {
  pass: 'bg-emerald-500/15 text-emerald-700 border border-emerald-500/25',
  fail: 'bg-amber-500/15 text-amber-700 border border-amber-500/25',
  insufficient: 'bg-line-strong/20 text-ink-muted border border-line-strong/40',
}
const READING_STATUS: Record<string, { label: string; cls: string }> = {
  ok: { label: '有值', cls: 'text-state-success' },
  missing: { label: '缺失', cls: 'text-ink-faint' },
  conflicting: { label: '冲突', cls: 'text-state-failure' },
  not_applicable: { label: '不适用', cls: 'text-ink-faint' },
  pending_pit: { label: '待核验', cls: 'text-state-issue' },
}
const readingStatus = (s: string) => READING_STATUS[s] || { label: s, cls: 'text-ink-faint' }
const short12 = (s?: string) => (s ? String(s).slice(0, 12) : '—')
const fmtTime = (iso?: string) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false })
}
const fmtValue = (v: number | null | undefined) =>
  v == null ? '—' : v.toLocaleString('zh-CN')

const kv = (k: string, v: ReactNode) => (
  <div className="min-w-[9rem]" key={k}>
    <div className="text-xs text-ink-faint">{k}</div>
    <div className="text-xs text-ink tabular-nums break-all">{v || '—'}</div>
  </div>
)

const syntheticBadge = (synthetic?: boolean, userProvided?: boolean, official?: boolean) => (
  <>
    {synthetic && (
      <span className="px-2 py-0.5 rounded-full text-xs font-semibold bg-amber-500/15 text-amber-700 border border-amber-500/25">
        合成演示数据 · 非真实披露
      </span>
    )}
    {!synthetic && userProvided && (
      <span className="px-2 py-0.5 rounded-full text-xs font-semibold bg-brand/10 text-brand border border-brand/25">
        用户提供 · 非官方
      </span>
    )}
    {!synthetic && official && (
      <span className="px-2 py-0.5 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-700 border border-emerald-500/25">
        官方披露缓存
      </span>
    )}
  </>
)

// ── 页面 ─────────────────────────────────────────────────────────────

export default function CompanyPage() {
  const [params, setParams] = useSearchParams()
  const code = (params.get('code') || '').trim().toUpperCase()
  const run = (params.get('run') || '').trim()
  const dataset = (params.get('dataset') || '').trim()

  const [data, setData] = useState<CompanyPayload | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  const [datasets, setDatasets] = useState<DatasetMeta[]>([])
  const [watchBusy, setWatchBusy] = useState(false)
  const [watchMsg, setWatchMsg] = useState('')
  const [evOpen, setEvOpen] = useState(false)
  const [evLoading, setEvLoading] = useState(false)
  const [evErr, setEvErr] = useState('')
  const [evidence, setEvidence] = useState<Evidence | null>(null)

  const load = useCallback(async () => {
    if (!code) return
    setLoading(true); setErr(''); setEvidence(null); setEvOpen(false); setWatchMsg('')
    try {
      const qs = new URLSearchParams({ code })
      if (run) qs.set('run', run)
      if (dataset) qs.set('dataset', dataset)
      const res = await fetch(`/api/screen/company?${qs.toString()}`)
      const d: CompanyPayload = await res.json().catch(() => ({ status: 'network_error' }))
      setData(d)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setLoading(false) }
  }, [code, run, dataset])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    if (data?.status !== 'select_input' || datasets.length) return
    fetch('/api/screen/datasets?limit=50')
      .then(r => r.json())
      .then((d: { datasets?: DatasetMeta[] }) => setDatasets(d.datasets ?? []))
      .catch(() => { /* 数据集列表读不到就不展示候选，不编 */ })
  }, [data?.status, datasets.length])

  const toggleWatch = async () => {
    if (!data?.company?.code) return
    setWatchBusy(true); setWatchMsg('')
    try {
      const inList = !!data.watch?.in_watchlist
      const body: Record<string, unknown> = {
        action: inList ? 'remove' : 'add',
        code: data.company.code, name: data.company.name || '',
      }
      if (inList && data.watch?.version != null) body.if_version = data.watch.version
      const res = await fetch('/api/screen/watchlist', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const d = await res.json().catch(() => ({ error: '清单操作失败' }) as { error?: string })
      if (!res.ok) {
        if (d.error) throw new Error(d.error)
        throw new Error(`清单操作失败（HTTP ${res.status}）`)
      }
      setData(prev => prev
        ? { ...prev, watch: { ...prev.watch, in_watchlist: !inList } }
        : prev)
      setWatchMsg(inList ? `已移出清单：${code}（清单是观察对象，不是持仓）`
        : `已加入清单：${code}（观察对象，不是持仓）`)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      if (msg.includes('修改') || msg.includes('conflict')) {
        setWatchMsg('清单被另一个页面改过（版本冲突，本次没有写入）：请刷新页面后重试。')
        await load()
      } else setWatchMsg(msg)
    } finally { setWatchBusy(false) }
  }

  const loadEvidence = async () => {
    if (!data?.evidence) return
    setEvLoading(true); setEvErr('')
    try {
      const q = data.evidence.query
      const res = await fetch(
        `${data.evidence.endpoint}?run=${encodeURIComponent(q.run)}&entity=${encodeURIComponent(q.entity)}`)
      const d = await res.json().catch(() => ({ error: '证据载入失败' }))
      if (!res.ok) throw new Error(d.error || `证据载入失败（HTTP ${res.status}）`)
      setEvidence(d.evidence as Evidence)
    } catch (e) {
      setEvErr(e instanceof Error ? e.message : String(e))
    } finally { setEvLoading(false) }
  }
  const toggleEvidence = () => {
    setEvOpen(v => {
      const next = !v
      if (next && !evidence && !evErr) loadEvidence()
      return next
    })
  }

  const status = data?.status ?? ''
  const identity = data?.identity ?? null
  const isDatasetView = !!identity?.dataset_id && !identity?.run_id

  if (!code) {
    return (
      <div className="max-w-5xl mx-auto p-4 md:p-6 space-y-4">
        <Card>
          <EmptyState icon={<Building2 className="w-5 h-5" />} title="缺参数：需要 code"
            description="公司页按 code + run/dataset 联合定位。从选股结果或研究清单点「看公司」进来即可；也可以在地址栏写 /screen/company?code=600031.SH。"
            action={<Link to="/screen" className="px-3 py-1.5 rounded-lg bg-brand/15 text-brand text-xs hover:bg-brand/25">回选股工作台</Link>} />
        </Card>
      </div>
    )
  }
  if (loading) {
    return <div className="max-w-5xl mx-auto p-4 md:p-6"><LoadingBlock label="正在载入公司页…" /></div>
  }
  if (err) {
    return <div className="max-w-5xl mx-auto p-4 md:p-6"><ErrorState onRetry={load} description={err} /></div>
  }
  if (!data) return null

  // ── 异常态：not_in_run / not_in_dataset / corrupt / stale_reference ──
  if (status === 'not_in_run' || status === 'not_in_dataset') {
    return (
      <div className="max-w-5xl mx-auto p-4 md:p-6 space-y-4">
        <Card>
          <Alert tone="warn" title={status === 'not_in_run' ? '该记录没有覆盖这家公司' : '该数据集没有这家公司'}>
            {data.error || '范围未含：不做任何推断。'}
          </Alert>
        </Card>
        <Card>
          <SectionTitle icon={<Eye className="w-4 h-4 text-brand" />} title="换一个入口" />
          <div className="flex flex-wrap gap-2">
            <Link to={`/screen/company?code=${code}`}
              className="px-3 py-1.5 rounded-lg bg-brand/15 text-brand text-xs hover:bg-brand/25">查看该公司的候选记录</Link>
            <Link to="/screen" className="px-3 py-1.5 rounded-lg border border-line-strong text-xs text-ink-muted hover:text-brand">回选股工作台</Link>
          </div>
        </Card>
      </div>
    )
  }
  if (status === 'stale_reference') {
    return (
      <div className="max-w-5xl mx-auto p-4 md:p-6 space-y-4">
        <Card>
          <Alert tone="danger" title="记录不可读或完整性未通过（不重新生成替代品）">
            {data.error || data.note || '原字节已保留：请回到本人记录列表核对这份结果的状态。'}
          </Alert>
        </Card>
        <Card>
          <div className="flex flex-wrap gap-2">
            <Link to="/screen" className="px-3 py-1.5 rounded-lg bg-brand/15 text-brand text-xs hover:bg-brand/25">回选股工作台</Link>
          </div>
        </Card>
      </div>
    )
  }
  if (status === 'corrupt') {
    return (
      <div className="max-w-5xl mx-auto p-4 md:p-6 space-y-4">
        <Card>
          <Alert tone="danger" title="数据集不可读">{data.error}</Alert>
        </Card>
      </div>
    )
  }

  // ── 选择输入态：候选 = 本作用域里覆盖该公司的冻结记录 ∪ 数据集 ──
  if (status === 'select_input') {
    const cands = data.candidates ?? []
    return (
      <div className="max-w-5xl mx-auto p-4 md:p-6 space-y-4">
        <Card>
          <div className="flex items-center gap-2 pb-1">
            <Building2 className="w-5 h-5 text-brand" />
            <h1 className="text-base font-semibold text-ink">{code}{data.company?.name ? ` · ${data.company.name}` : ''}</h1>
          </div>
          <Alert tone="info" title="只有公司代码不够：请选择一次本人冻结记录（或一份数据集）">
            公司页不做「按代码自动打开不明来源记录」——先选材料，再看这一份材料里的这家公司。
          </Alert>
        </Card>
        <Card>
          <SectionTitle icon={<Eye className="w-4 h-4 text-brand" />}
            title="本人冻结记录（最近优先，最多 20 条）"
            extra={cands.length ? `${cands.length} 条` : undefined} />
          {cands.length === 0 ? (
            <EmptyState title="还没有包含这家公司的记录"
              description={data.note || '请先在选股工作台选择材料并执行筛选，或直接选择一份数据集。'}
              action={<Link to="/screen" className="px-3 py-1.5 rounded-lg bg-brand/15 text-brand text-xs hover:bg-brand/25">去执行筛选</Link>} />
          ) : (
            <div className="space-y-2">
              {cands.map(c => (
                <button key={c.run_id} type="button"
                  onClick={() => setParams({ code, run: c.run_id })}
                  aria-label={`打开记录 ${short12(c.run_id)}（截止日 ${c.as_of || '未知'}）里的 ${code}`}
                  className="w-full text-left border border-line rounded-lg px-3 py-2.5 bg-surface-sunken/40
                    hover:border-brand/40 hover:bg-surface-raised/40 transition-colors">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm text-ink">冻结于 {fmtTime(c.created_at)}</span>
                    <span className="text-xs text-ink-faint tabular-nums">截止日 {c.as_of || '—'}</span>
                    {c.usable === false
                      ? <span className="text-xs text-state-failure">完整性未通过</span>
                      : <span className="text-xs text-state-success">完整</span>}
                    {c.source_label && <span className="text-xs text-ink-faint">{c.source_label}</span>}
                  </div>
                  <div className="text-xs text-ink-faint tabular-nums pt-1">
                    通过 {(c.counts?.pass ?? 0)} / 未通过 {(c.counts?.fail ?? 0)} / 资料不足 {(c.counts?.insufficient ?? 0)} · run_id {short12(c.run_id)}…
                  </div>
                </button>
              ))}
            </div>
          )}
        </Card>
        <Card>
          <SectionTitle icon={<Eye className="w-4 h-4 text-brand" />} title="或选择一份数据集（只看已有字段，没有条件结论）" />
          {datasets.length === 0 ? (
            <p className="text-xs text-ink-faint">本作用域还没有可选数据集（官方缓存未配置、也没有上传过）。</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {datasets.map(ds => (
                <button key={ds.dataset_id} type="button"
                  onClick={() => setParams({ code, dataset: ds.dataset_id })}
                  aria-label={`打开数据集 ${ds.name || short12(ds.dataset_id)} 里的 ${code}`}
                  className="px-3 py-1.5 rounded-lg border border-line-strong text-xs text-ink-muted hover:text-brand hover:border-brand/40">
                  {ds.name || `数据集 ${short12(ds.dataset_id)}…`}
                  {ds.synthetic && '（合成）'}
                  {ds.rows != null ? ` · ${ds.rows} 行` : ''}
                </button>
              ))}
            </div>
          )}
        </Card>
      </div>
    )
  }

  // ── 正常态：run 视图（身份+财务+条件+判定）或数据集只读视图 ──
  const financials = data.financials ?? []
  const periods = Array.from(new Set(financials.map(f => f.period))).sort()
  const metrics = Array.from(new Map(financials.map(f => [f.metric, f])).values())

  return (
    <div className="max-w-5xl mx-auto p-4 md:p-6 space-y-4" aria-label={`公司页 ${code}`}>
      {/* 头部：公司 + 来源徽章（合成永远可见）+ 判定 + watch */}
      <Card>
        <div className="flex flex-wrap items-center gap-2 pb-2">
          <Building2 className="w-5 h-5 text-brand shrink-0" />
          <h1 className="text-base font-semibold text-ink">{data.company?.name || code}</h1>
          <span className="text-xs text-ink-faint tabular-nums">{code}</span>
          {data.verdict && (
            <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${VERDICT_CLASS[data.verdict]}`}>
              {VERDICT_LABEL[data.verdict]}
            </span>
          )}
          {syntheticBadge(identity?.synthetic, identity?.user_provided, identity?.official)}
        </div>
        <p className="text-xs text-ink-faint leading-relaxed">
          {isDatasetView
            ? '这是数据集只读视图：只列该数据集里已有的字段，没有执行筛选，因此没有条件命中/缺口结论。'
            : '这是「这一份冻结记录里的这家公司」的只读投影：不重算、不补数据，人工复核未开始。'}
        </p>
        <div className="flex flex-wrap items-center gap-2 pt-2">
          <button type="button" onClick={toggleWatch} disabled={watchBusy}
            aria-label={data.watch?.in_watchlist ? `把 ${code} 移出研究清单` : `把 ${code} 加入研究清单`}
            className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-line-strong text-xs
              text-ink-muted hover:text-brand hover:border-brand/40 transition-colors disabled:opacity-50">
            {data.watch?.in_watchlist
              ? <><CheckCircle2 className="w-3.5 h-3.5 text-state-success" /> 在清单中 · 移出</>
              : <><Plus className="w-3.5 h-3.5" /> 加入研究清单</>}
          </button>
          {data.evidence && (
            <Link to={`/screen?run=${encodeURIComponent(data.evidence.query.run)}`}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-line-strong text-xs text-ink-muted hover:text-brand">
              回到这份结果 <ExternalLink className="w-3 h-3" />
            </Link>
          )}
          <Link to={`/screen/company?code=${code}`}
            className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-line-strong text-xs text-ink-muted hover:text-brand">
            换一次记录/数据集
          </Link>
          {watchMsg && <span className="text-xs text-ink-muted" role="status">{watchMsg}</span>}
        </div>
        {data.note && !isDatasetView && (
          <div className="pt-2"><Alert tone="warn" title="记录完整性未通过">{data.note}</Alert></div>
        )}
      </Card>

      {/* 数据身份 */}
      <Card>
        <SectionTitle icon={<ShieldCheck className="w-4 h-4 text-brand" />}
          title={isDatasetView ? '数据身份（数据集）' : '数据身份（这一份冻结记录）'}
          extra={identity?.integrity && identity.integrity !== 'ok'
            ? <span className="text-state-failure">完整性：{identity.integrity === 'hash_mismatch' ? '哈希不一致' : '清单缺失'}</span>
            : identity?.run_id ? <span className="text-state-success">完整性 ok</span> : undefined} />
        <div className="flex flex-wrap gap-x-6 gap-y-3">
          {identity?.run_id && kv('run_id', identity.run_id)}
          {identity?.dataset_id && kv('dataset_id', identity.dataset_id)}
          {identity?.result_hash && kv('结果哈希', `${short12(identity.result_hash)}…`)}
          {identity?.snapshot_id && kv('快照', identity.snapshot_id)}
          {identity?.as_of !== undefined && kv('资料截止日', identity.as_of || '—')}
          {identity?.created_at && kv('冻结时间', fmtTime(identity.created_at))}
          {identity?.report_period && kv('报告期', identity.report_period)}
          {identity?.impl_version && kv('计算实现版本', identity.impl_version)}
          {identity?.field_map_version && kv('字段口径版本', identity.field_map_version)}
          {identity?.source_label && kv('来源', identity.source_label)}
          {(identity?.periods?.length ?? 0) > 0 && kv('数据覆盖期间', (identity?.periods ?? []).join('、'))}
        </div>
        {identity && (identity.provenance || identity.provenance_kind !== undefined) && (
          <details className="mt-3 text-xs text-ink-faint">
            <summary className="cursor-pointer select-none hover:text-ink-muted inline-flex items-center gap-1">
              <ChevronRight className="w-3.5 h-3.5" aria-hidden /> 来源与人工核验记录（provenance，只读展开）
            </summary>
            <pre className="mt-2 p-3 rounded-lg bg-surface-sunken/70 text-ink-muted whitespace-pre-wrap break-all leading-relaxed">
              {JSON.stringify(identity.provenance ?? { provenance_kind: identity.provenance_kind }, null, 2)}
            </pre>
          </details>
        )}
      </Card>

      {/* 关键财务 */}
      <Card>
        <SectionTitle icon={<Hash className="w-4 h-4 text-brand" />}
          title="关键财务（观察值逐条可核对，缺失不补 0）"
          extra={financials.length ? `${financials.length} 条观察` : '没有观察值'} />
        {financials.length === 0 ? (
          <EmptyState title="这份材料里没有该公司的财务观察值" description="不做推断；可换一份记录或数据集再打开。" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-ink-faint border-b border-line">
                  <th className="py-2 pr-3 font-medium">科目</th>
                  {metrics.map(m => (
                    <th key={m.metric} className="py-2 pr-3 font-medium">
                      {metricLabel(m)}
                      {m.unit && m.unit !== '元' ? `（${m.unit}）` : ''}
                    </th>
                  ))}
                  <th className="py-2 font-medium">口径</th>
                </tr>
              </thead>
              <tbody>
                {periods.map(period => {
                  const rowMap = new Map(
                    financials.filter(f => f.period === period).map(f => [f.metric, f]))
                  return (
                    <tr key={period} className="border-b border-line/50 last:border-0">
                      <td className="py-2 pr-3 text-xs text-ink-muted tabular-nums whitespace-nowrap">
                        {period}
                        <span className="text-ink-faint">
                          {rowMap.get(metrics[0]?.metric ?? '')?.period_kind === 'FY' ? '（年报）'
                            : rowMap.get(metrics[0]?.metric ?? '')?.period_kind ? `（${rowMap.get(metrics[0]?.metric ?? '')!.period_kind}）` : ''}
                        </span>
                      </td>
                      {metrics.map(m => {
                        const f = rowMap.get(m.metric)
                        const st = readingStatus(f?.state ?? '')
                        return (
                          <td key={m.metric} className="py-2 pr-3 text-xs tabular-nums">
                            {f == null || f.value == null
                              ? <span className={st.cls} title={st.label}>—（{st.label}）</span>
                              : <span className="text-ink">{fmtValue(f.value)} <span className="text-ink-faint">{f.unit || ''}</span></span>}
                          </td>
                        )
                      })}
                      <td className="py-2 text-xs text-ink-faint">
                        {rowMap.get(metrics[0]?.metric ?? '')?.caliber || '—'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* 条件读数 + 判定（只有 run 视图有；数据集视图显式说明没有） */}
      <Card>
        <SectionTitle icon={<ShieldCheck className="w-4 h-4 text-brand" />} title="本次筛选条件读数" />
        {(data.conditions ?? []).length === 0 ? (
          <Alert tone="info" title="没有条件读数">
            {isDatasetView
              ? '数据集视图不执行筛选：这里没有条件命中/缺口结论，也不会有判定。'
              : '该记录没有这家公司的条件读数（可能资料不足）。'}
          </Alert>
        ) : (
          <div className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
            {(data.conditions ?? []).map(r => {
              const st = readingStatus(r.status)
              return (
                <div key={r.field} className="border border-line rounded-lg px-3 py-2 bg-surface-sunken/60">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs text-ink-muted">{r.label}</span>
                    <span className={`text-xs font-medium ${st.cls}`}>{st.label}</span>
                  </div>
                  <div className="text-sm text-ink tabular-nums mt-0.5">{r.display || '—'}</div>
                  {r.reason && <div className="text-xs text-ink-faint mt-1 leading-snug">{r.reason}</div>}
                  {r.formula && <div className="text-xs text-ink-faint/80 mt-0.5 tabular-nums">{r.formula}</div>}
                </div>
              )
            })}
          </div>
        )}
        {(data.reasons ?? []).length > 0 && (
          <ul className="mt-3 space-y-1">
            {(data.reasons ?? []).map((reason, i) => (
              <li key={i} className="text-xs text-ink-muted flex gap-1.5 leading-snug">
                {data.verdict === 'pass'
                  ? <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 shrink-0 text-state-success" />
                  : <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0 text-state-issue" />}
                {reason}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* 判定与缺口（run 视图） */}
      {!isDatasetView && (
        <Card>
          <SectionTitle icon={<ShieldCheck className="w-4 h-4 text-brand" />} title="判定、缺口与冲突" />
          <div className="grid gap-2 sm:grid-cols-3">
            <StatCard label="本次判定（机器状态）"
              value={data.verdict ? VERDICT_LABEL[data.verdict] : '—'}
              hint={`评估期间 ${data.eval_period || '—'} · 人工复核未开始`} />
            <StatCard label="缺口" value={String((data.gaps ?? []).length)} tone="issue"
              sub={(data.gaps ?? [])[0] || '没有登记缺口'} />
            <StatCard label="冲突" value={String((data.conflicts ?? []).length)} tone="failure"
              sub={(data.conflicts ?? [])[0] || '没有登记冲突'} />
          </div>
          {(data.pending_pit ?? []).length > 0 && (
            <Alert tone="warn" title={`有 ${(data.pending_pit ?? []).length} 条观察在待核验期（PIT）内`}>
              这些数值在可核时点之前取得，可能影响时点可比性；明细见证据。
            </Alert>
          )}
          <p className="text-xs text-ink-faint leading-relaxed pt-2">
            三态分离：上面是机器判定（可复算）；「人工复核」只能由人给出，本页不提供复核入口、不显示任何复核结论。
          </p>
        </Card>
      )}

      {/* 证据（折叠，按需载入；绑 run_id + entity_id） */}
      {data.evidence && (
        <Card>
          <SectionTitle icon={<Eye className="w-4 h-4 text-brand" />} title="证据（逐条观察 + 来源，绑这份记录）" />
          <button type="button" onClick={toggleEvidence} aria-expanded={evOpen}
            className="inline-flex items-center gap-1 text-xs text-brand hover:underline py-1">
            {evOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
            {evOpen ? '收起证据明细' : '展开证据明细'}
          </button>
          {evOpen && (
            <div className="pt-2 space-y-2" aria-live="polite">
              {evLoading && <div className="flex items-center gap-2 text-xs text-ink-faint">
                <Loader2 className="w-4 h-4 animate-spin" /> 正在载入证据…</div>}
              {evErr && <Alert tone="danger" title="证据载入失败">{evErr}</Alert>}
              {evidence && evidence.readings.map(r => (
                <div key={r.field} className="border border-line rounded-lg px-3 py-2.5 bg-surface-sunken/60">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm text-ink">{r.label}</span>
                    <span className="text-sm text-ink tabular-nums">{r.display || '—'}</span>
                    {r.formula && <span className="text-xs text-ink-faint tabular-nums">{r.formula}</span>}
                  </div>
                  {r.inputs.length > 0 && (
                    <div className="mt-2 overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="text-left text-ink-faint border-b border-line/70">
                            <th className="py-1 pr-3 font-medium">输入科目</th>
                            <th className="py-1 pr-3 font-medium">期间</th>
                            <th className="py-1 pr-3 font-medium">数值</th>
                            <th className="py-1 pr-3 font-medium">可用时点</th>
                            <th className="py-1 font-medium">来源</th>
                          </tr>
                        </thead>
                        <tbody>
                          {r.inputs.map(inp => (
                            <tr key={inp.fact_id} className="border-b border-line/40 last:border-0">
                              <td className="py-1 pr-3 text-ink-muted">{inp.metric_label || inp.metric}</td>
                              <td className="py-1 pr-3 tabular-nums text-ink-muted">{inp.period}</td>
                              <td className="py-1 pr-3 tabular-nums text-ink">
                                {inp.value != null ? `${fmtValue(inp.value)} ${inp.unit || ''}` : '—'}
                                <span className="text-ink-faint">（{inp.caliber || '—'}）</span>
                              </td>
                              <td className="py-1 pr-3 tabular-nums text-ink-faint">{inp.as_of || '—'}</td>
                              <td className="py-1">
                                {inp.source_url
                                  ? <a href={inp.source_url} target="_blank" rel="noreferrer"
                                      className="text-brand hover:underline inline-flex items-center gap-0.5">
                                      核对 <ExternalLink className="w-3 h-3" /></a>
                                  : <span className="text-ink-faint">—</span>}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {(r.unresolved_inputs ?? []).length > 0 && (
                    <div className="mt-1.5 text-xs text-state-issue">
                      有 {(r.unresolved_inputs ?? []).length} 条引用的观察不在本结果里（fact_id：
                      {(r.unresolved_inputs ?? []).join('、')}）——不回填。
                    </div>
                  )}
                </div>
              ))}
              {evidence && (
                <div className="text-xs text-ink-faint tabular-nums">
                  证据绑定 run_id {evidence.run_id} · 结果哈希 {short12(evidence.result_hash)}…
                </div>
              )}
            </div>
          )}
        </Card>
      )}

      {/* 全量哈希/审计（折叠） */}
      <Card>
        <details className="text-xs text-ink-faint">
          <summary className="cursor-pointer select-none hover:text-ink-muted inline-flex items-center gap-1">
            <ChevronDown className="w-3.5 h-3.5" aria-hidden /> 哈希与审计（完整身份，只读展开）
          </summary>
          <pre className="mt-2 p-3 rounded-lg bg-surface-sunken/70 text-ink-muted whitespace-pre-wrap break-all leading-relaxed">
            {JSON.stringify({
              status: data.status, company: data.company, as_of: data.as_of,
              identity, eval_period: data.eval_period, verdict: data.verdict,
              watch: data.watch, evidence: data.evidence, note: data.note,
            }, null, 2)}
          </pre>
        </details>
      </Card>
    </div>
  )
}
