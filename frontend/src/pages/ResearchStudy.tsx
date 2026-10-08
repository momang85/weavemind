// 本地研究试验（10-08 三域规划 §16 的工作台入口）
//
// 一件事：**文件预览 → 明确截止/公司 → run_study → 折叠查看经营/事件/风险/内部附件**。
// 复用面：登录会话与管理员鉴权（POST 自动 admin）、与材料入口同形的上传契约
// （`{filename, data(base64)}`）、以及既有 `quant_research.study` 的 run/write/verify/replay。
// 本页不新建任务、不投递编排器、不调用模型；产物是**内部研究文件**：
// 不自动采纳、不进 MD/PDF/ZIP 交付包、未人工复核。

import { useEffect, useMemo, useState } from 'react'
import {
  FlaskConical, RefreshCw, Upload, Eye, Play, ShieldCheck, RotateCcw, FileText,
  ChevronDown, ChevronRight, AlertTriangle, CheckCircle2,
} from 'lucide-react'
import { Card, SectionTitle, Alert, EmptyState } from '../components/ui'

type Role = 'financial' | 'market' | 'events' | 'calendar' | 'credit_labels' | 'fraud_labels'

const ROLE_LABEL: Record<Role, string> = {
  financial: '财务宽表（三表科目）',
  market: '日行情',
  events: '新闻/公告',
  calendar: '交易日历',
  credit_labels: '信用标签',
  fraud_labels: '反欺诈标签',
}

function guessRole(name: string): Role | '' {
  const n = name.toLowerCase()
  if (/(price|market|quote|行情)/.test(n)) return 'market'
  if (/(news|event|公告|新闻)/.test(n)) return 'events'
  if (/(calendar|日历)/.test(n)) return 'calendar'
  if (/(credit|信用)/.test(n)) return 'credit_labels'
  if (/(fraud|欺诈)/.test(n)) return 'fraud_labels'
  if (/(financ|财务|三表)/.test(n)) return 'financial'
  return ''
}

interface StudyFile { name: string; bytes: number; sha256: string }
interface StudyPayload {
  study_id?: string; result_hash?: string; synthetic?: boolean
  feature_cutoff?: string; evaluation_as_of?: string
  research_state?: string; human_review?: string
  external_delivery?: Record<string, unknown>
  sources?: Record<string, { file?: string; raw_sha256?: string }>
  subjects?: string[]
  financial?: Record<string, { synthetic?: boolean; runs?: any[]; unit_conversion?: any[]; unsupported_units?: string[] }>
  events?: { candidates?: number; excluded?: number }
  quant?: Record<string, { status?: string; reason?: string }>
  risk?: Record<string, { status?: string; features?: number; gaps?: number; anomaly_clues?: string[] }>
  label_readiness?: Record<string, { status?: string }>
  previews?: Record<string, any>
  limits?: string[]
  summary_markdown?: string
  dir?: string; artifacts?: { path: string; bytes: number }[]
  status?: string; note?: string
}

function Fold({ title, hint, children, defaultOpen = false }: {
  title: string; hint?: string; children: any; defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="border border-slate-800 rounded-xl overflow-hidden">
      <button onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-4 py-2.5 bg-slate-900/60 hover:bg-slate-800/60 text-left">
        {open ? <ChevronDown className="w-4 h-4 text-cyan-400" />
          : <ChevronRight className="w-4 h-4 text-slate-500" />}
        <span className="text-sm text-slate-200">{title}</span>
        {hint && <span className="ml-auto text-xs text-slate-500">{hint}</span>}
      </button>
      {open && <div className="px-4 py-3 text-xs text-slate-400 space-y-2">{children}</div>}
    </div>
  )
}

export default function ResearchStudy() {
  const [sample, setSample] = useState<any>(null)
  const [studies, setStudies] = useState<any[]>([])
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')
  const [inputId, setInputId] = useState('__sample__')
  const [files, setFiles] = useState<StudyFile[]>([])
  const [roles, setRoles] = useState<Record<string, Role | ''>>({})
  const [cutoff, setCutoff] = useState('2025-04-30')
  const [asOf, setAsOf] = useState('2025-06-30')
  const [preview, setPreview] = useState<StudyPayload | null>(null)
  const [run, setRun] = useState<StudyPayload | null>(null)
  const [check, setCheck] = useState<any>(null)
  const [card, setCard] = useState<any>(null)
  const [confirmed, setConfirmed] = useState<string[]>([])

  const loadSample = async () => {
    try {
      const d = await (await fetch('/api/research-study/sample')).json()
      setSample(d)
      setFiles(d.files ?? [])
      setInputId(d.sample_input_id ?? '__sample__')
      setCutoff(d.spec?.feature_cutoff ?? '2025-04-30')
      setAsOf(d.spec?.evaluation_as_of ?? '2025-06-30')
      setErr('')
    } catch (e: any) { setErr(`样例清单加载失败：${e?.message ?? e}`) }
  }
  const loadStudies = async () => {
    try {
      const d = await (await fetch('/api/research-study/studies')).json()
      setStudies(d.studies ?? [])
    } catch { /* 读不到就保持上一次列表，不编 */ }
  }
  useEffect(() => { loadSample(); loadStudies() }, [])

  /** 上传：与材料入口同形的 base64 契约；服务端按管理员鉴权与上限体检。 */
  const upload = async (list: FileList | null) => {
    if (!list?.length) return
    setBusy('upload'); setErr(''); setPreview(null); setRun(null); setCheck(null); setCard(null)
    try {
      let id = inputId === '__sample__' ? '' : inputId
      for (const f of Array.from(list)) {
        const b64 = await new Promise<string>((resolve) => {
          const r = new FileReader()
          r.onload = () => resolve(String(r.result).split(',')[1] || '')
          r.onerror = () => resolve('')
          r.readAsDataURL(f)
        })
        if (!b64) throw new Error(`${f.name} 读取失败`)
        const res = await fetch('/api/research-study/upload', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: f.name, data: b64, input_id: id || undefined }),
        })
        const d = await res.json()
        if (!res.ok) throw new Error(d.error || '上传失败')
        id = d.input_id
        setFiles(d.files ?? [])
      }
      setInputId(id)
      setRoles(prev => {
        const next = { ...prev }
        for (const f of Array.from(list)) if (!next[f.name]) next[f.name] = guessRole(f.name)
        return next
      })
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  /** 组 spec：样例模板 + 上传文件按角色替换；只放共享参数，模型参数留在模板里。 */
  const buildSpec = () => {
    const base = sample?.spec ? JSON.parse(JSON.stringify(sample.spec)) : null
    if (!base) return 'sample'
    if (inputId === '__sample__') return 'sample'
    for (const key of Object.keys(ROLE_LABEL) as Role[]) delete (base as any)[key]
    for (const f of files) {
      const role = roles[f.name]
      if (role) (base as any)[role] = { ...(base as any)[role], file: f.name }
    }
    base.feature_cutoff = cutoff
    base.evaluation_as_of = asOf
    return base
  }

  const post = async (path: string, body: any) => {
    const res = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const d = await res.json()
    return { ok: res.ok, status: res.status, d }
  }

  const doPreview = async () => {
    setBusy('preview'); setErr(''); setRun(null); setCheck(null); setCard(null)
    try {
      const { ok, d } = await post('/api/research-study/preview',
        { spec: buildSpec(), input_id: inputId })
      if (!ok) throw new Error(d.error || '预览失败')
      setPreview(d); setConfirmed([])
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  const doRun = async () => {
    setBusy('run'); setErr(''); setCheck(null); setCard(null)
    try {
      const { ok, d } = await post('/api/research-study/run', {
        spec: buildSpec(), input_id: inputId, confirmed: true,
        confirmed_subjects: confirmed, feature_cutoff: cutoff, evaluation_as_of: asOf,
      })
      if (!ok) {
        throw new Error(`${d.error || '运行失败'}`
          + (d.subjects_in_files ? `（文件里读到：${(d.subjects_in_files || []).join('、')}）` : ''))
      }
      setRun(d); loadStudies()
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  const doCheck = async (kind: 'verify' | 'replay') => {
    const sid = run?.study_id || preview?.study_id || studies[0]?.study_id
    if (!sid) { setErr('还没有已保存的研究产物'); return }
    setBusy(kind); setErr('')
    try {
      const { ok, d } = await post(`/api/research-study/${kind}`, { study_id: sid })
      if (!ok) throw new Error(d.error || `${kind} 失败`)
      setCheck({ kind, ...d })
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  const doCard = async () => {
    const sid = run?.study_id || preview?.study_id || studies[0]?.study_id
    if (!sid) { setErr('还没有已保存的研究产物'); return }
    setBusy('card'); setErr('')
    try {
      const { ok, d } = await post('/api/research-study/hypothesis-card', { study_id: sid })
      if (!ok) throw new Error(d.error || '生成假说卡失败')
      setCard(d)
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  const subjects = preview?.subjects ?? []
  const allConfirmed = subjects.length > 0 && confirmed.length === subjects.length
  const shown = run ?? preview
  const summary = useMemo(() => String(shown?.summary_markdown ?? ''), [shown])

  return (
    <div className="max-w-6xl mx-auto space-y-5">
      <div className="flex items-center gap-3">
        <FlaskConical className="w-6 h-6 text-cyan-400" />
        <h1 className="text-slate-200 text-lg font-semibold">本地研究试验</h1>
        <span className="text-xs text-slate-500">
          离线·不建任务·不调模型·内部研究文件
        </span>
        <button onClick={() => { loadSample(); loadStudies() }} aria-label="刷新"
          className="ml-auto text-slate-500 hover:text-cyan-400">
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      <Alert tone="warn" title="先看清这份产物的性质">
        <div className="space-y-1">
          <div>① 内置样例公司/证券代码/财务/新闻/行情/日历/标签**全部为模拟**：只验证流程，不证明收益或风险模型有效性。</div>
          <div>② 输出是**内部研究草稿**，人工未复核；**不自动采纳**、不进 MD/PDF/ZIP 交付包、不对外分发。</div>
          <div>③ 学校/供应商的“允许查看”不等于商业使用或云端处理授权；许可状态在本入口一律未核实。</div>
        </div>
      </Alert>

      {err && <Alert tone="danger" title="这一步没成功">{err}</Alert>}

      <Card>
        <SectionTitle icon={<Upload className="w-4 h-4 text-cyan-400" />} title="① 选定输入"
          extra={inputId === '__sample__' ? '内置模拟样例' : `上传目录 ${inputId}`} />
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <button onClick={() => { setInputId('__sample__'); setFiles(sample?.files ?? []); setPreview(null); setRun(null) }}
            className={`px-3 py-1.5 rounded-lg text-xs border ${inputId === '__sample__'
              ? 'border-cyan-500/40 bg-cyan-500/10 text-cyan-300' : 'border-slate-700 text-slate-400'}`}>
            用内置两公司模拟样例
          </button>
          <label className={`px-3 py-1.5 rounded-lg text-xs border cursor-pointer ${inputId !== '__sample__'
            ? 'border-cyan-500/40 bg-cyan-500/10 text-cyan-300' : 'border-slate-700 text-slate-400'}`}>
            上传 CSV/JSON（可多选，单件上限与材料入口相同）
            <input type="file" multiple className="hidden" disabled={busy === 'upload'}
              onChange={e => upload(e.target.files)} />
          </label>
          {busy === 'upload' && <span className="text-xs text-slate-500">上传中…</span>}
        </div>
        {files.length === 0 ? (
          <EmptyState title="还没有输入文件"
            description="选内置模拟样例，或上传你自己的财务宽表/行情/新闻/日历/标签文件；缺某域就删掉对应项，不用假数据补。" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-slate-500">
                <tr><th className="text-left py-1">文件</th><th className="text-right">字节</th>
                  <th className="text-left">sha256</th><th className="text-left">用途</th></tr>
              </thead>
              <tbody className="text-slate-300">
                {files.map(f => (
                  <tr key={f.name} className="border-t border-slate-800">
                    <td className="py-1 pr-3">{f.name}</td>
                    <td className="text-right tabular-nums">{f.bytes.toLocaleString()}</td>
                    <td className="px-3 font-mono text-slate-500">{f.sha256.slice(0, 12)}…</td>
                    <td>
                      {inputId === '__sample__' ? <span className="text-slate-500">按样例 spec</span> : (
                        <select value={roles[f.name] ?? ''} className="bg-slate-900 border border-slate-700 rounded px-1 py-0.5"
                          onChange={e => setRoles({ ...roles, [f.name]: e.target.value as Role })}>
                          <option value="">不使用</option>
                          {(Object.keys(ROLE_LABEL) as Role[]).map(r => (
                            <option key={r} value={r}>{ROLE_LABEL[r]}</option>
                          ))}
                        </select>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card>
        <SectionTitle icon={<Eye className="w-4 h-4 text-cyan-400" />} title="② 明确截止与公司 → 文件预览"
          extra="特征截止只定特征，结果评价截止允许之后的行情作为结果" />
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-3">
          <label className="text-xs text-slate-400">特征截止（feature_cutoff）
            <input value={cutoff} onChange={e => setCutoff(e.target.value)}
              className="mt-1 w-full bg-slate-900 border border-slate-700 rounded-lg px-2 py-1.5 text-slate-200" />
          </label>
          <label className="text-xs text-slate-400">结果评价截止（evaluation_as_of）
            <input value={asOf} onChange={e => setAsOf(e.target.value)}
              className="mt-1 w-full bg-slate-900 border border-slate-700 rounded-lg px-2 py-1.5 text-slate-200" />
          </label>
          <div className="text-xs text-slate-400">预览后需逐个勾选确认的**文件内主体**
            <div className="mt-1 text-slate-500">公司身份只从文件读，不按请求里的名字替换材料</div>
          </div>
        </div>
        <button onClick={doPreview} disabled={busy === 'preview'}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm disabled:opacity-50">
          <Eye className="w-4 h-4" /> {busy === 'preview' ? '预览中…' : '文件预览（只读）'}
        </button>

        {preview?.previews && (
          <div className="mt-4 space-y-2">
            {Object.entries(preview.previews).map(([key, p]: [string, any]) => (
              <Fold key={key} title={`${ROLE_LABEL[key as Role] ?? key}：${p.raw_file ?? ''}`}
                hint={`${p.rows} 行 · 状态 ${p.status} · 可用记录 ${p.eligible_records ?? '—'} · 问题 ${p.issue_count}`}>
                <div>原字段：{(p.raw_columns ?? []).join('、') || '—'}</div>
                <div>行外默认：{Object.entries(p.declared ?? {}).map(([k, v]) => `${k}=${v}`).join('；') || '—'}</div>
                <div>未映射字段：{(p.unknown_columns ?? []).join('、') || '无'}</div>
                <div>标的：{(p.instruments ?? []).join('、') || '—'}；许可状态 {p.license_status ?? '—'}；可导出 {String(p.export_allowed)}</div>
                <div>{p.historical_version ?? ''}</div>
                {(p.sample_rows ?? []).length > 0 && (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs mt-1">
                      <thead className="text-slate-500"><tr>
                        <th className="text-left">主体</th><th className="text-left">指标/日期</th>
                        <th className="text-right">值</th><th className="text-left">单位</th>
                        <th className="text-left">期间</th><th className="text-left">披露</th>
                        <th className="text-left">可用</th><th className="text-left">可用?</th>
                      </tr></thead>
                      <tbody>
                        {p.sample_rows.map((r: any, i: number) => (
                          <tr key={i} className="border-t border-slate-800">
                            <td>{r.entity_id}</td><td>{r.metric}</td>
                            <td className="text-right tabular-nums">{r.value}</td>
                            <td>{r.unit}</td><td>{r.period}</td>
                            <td>{r.disclosed_at}</td><td>{r.available_at}</td>
                            <td>{r.eligible ? <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                              : <AlertTriangle className="w-3.5 h-3.5 text-amber-400" />}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {(p.issues ?? []).length > 0 && (
                  <ul className="list-disc pl-4">
                    {p.issues.slice(0, 8).map((it: any, i: number) => (
                      <li key={i}>[{it.severity}] {it.code} 第{it.row || '—'}行 {it.field}：{it.message}</li>
                    ))}
                  </ul>
                )}
              </Fold>
            ))}
          </div>
        )}

        {subjects.length > 0 && (
          <div className="mt-4">
            <div className="text-xs text-slate-400 mb-1">文件里读到的公司（逐个确认后才允许运行）：</div>
            <div className="flex flex-wrap gap-2">
              {subjects.map(s => (
                <label key={s} className="inline-flex items-center gap-1 px-2 py-1 rounded-lg border border-slate-700 text-xs text-slate-300">
                  <input type="checkbox" checked={confirmed.includes(s)}
                    onChange={e => setConfirmed(e.target.checked
                      ? [...confirmed, s] : confirmed.filter(x => x !== s))} />
                  {s}
                </label>
              ))}
            </div>
            <div className="text-xs text-slate-500 mt-1">
              确认清单与文件不一致时服务端会拒绝运行（409），不会按请求里的公司名替换材料身份。
            </div>
          </div>
        )}
      </Card>

      <Card>
        <SectionTitle icon={<Play className="w-4 h-4 text-cyan-400" />} title="③ 运行并保存内部研究产物"
          extra="同一研究已存在则不覆盖；改参数或输入会得到新的研究ID" />
        <button onClick={doRun} disabled={busy === 'run' || !allConfirmed}
          title={allConfirmed ? '' : '先预览并逐个确认文件里的公司'}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 text-sm disabled:opacity-40">
          <Play className="w-4 h-4" /> {busy === 'run' ? '运行中…' : '运行并保存'}
        </button>
        {!allConfirmed && <span className="ml-3 text-xs text-slate-500">
          需先「文件预览」并确认 {subjects.length ? '全部主体' : '文件里的公司'}
        </span>}

        {run && (
          <div className="mt-4 space-y-3">
            <Alert tone={run.status === 'exists' ? 'warn' : 'info'} title={run.note}>
              <div>研究ID：<span className="font-mono">{String(run.study_id ?? '').slice(0, 16)}…</span>；
                模拟数据：{String(run.synthetic)}；研究状态 {run.research_state} / 人工 {run.human_review}</div>
              <div>目录：<span className="font-mono">{run.dir}</span></div>
              <div>外部交付：{JSON.stringify(run.external_delivery ?? {})}</div>
            </Alert>
            <Fold title="经营（财务模型运行）" defaultOpen
              hint={(run.subjects ?? []).join('、')}>
              {Object.entries(run.financial ?? {}).map(([code, row]: [string, any]) => (
                <div key={code}>
                  <div className="text-slate-300">{code}（{row.synthetic ? '模拟' : '真实'}）</div>
                  {(row.runs ?? []).map((r: any) => (
                    <div key={r.model_id} className="pl-3">
                      - {r.model_id}：{r.status}{r.reason ? `；${r.reason}` : ''}
                      {(r.outputs ?? []).slice(0, 6).map((o: any, i: number) => (
                        <div key={i} className="pl-3 text-slate-500">
                          {o.label ?? o.metric}：{typeof o.value === 'number' ? o.value.toLocaleString() : o.value} {o.unit}
                        </div>
                      ))}
                    </div>
                  ))}
                  {(row.unsupported_units ?? []).length > 0 &&
                    <div className="pl-3 text-amber-400">不支持的单位：{row.unsupported_units.join('、')}</div>}
                </div>
              ))}
            </Fold>
            <Fold title="事件与情绪候选" hint={`候选 ${run.events?.candidates ?? 0} · 排除 ${run.events?.excluded ?? 0}`}>
              <div>原文引用、字符位置、版本与排除记录见 <span className="font-mono">events.json</span>；候选不等于事实认证。</div>
            </Fold>
            <Fold title="历史检验（事件反应/内部账本）" hint={Object.keys(run.quant ?? {}).join('、')}>
              {Object.entries(run.quant ?? {}).map(([k, v]: [string, any]) => (
                <div key={k}>- {k}：{v.status}{v.reason ? `；${v.reason}` : ''}</div>
              ))}
              <div>已明确标 internal、不可对外；不是策略有效性证明。</div>
            </Fold>
            <Fold title="风险特征与标签准备" hint={`${Object.keys(run.risk ?? {}).length} 个主体`}>
              {Object.entries(run.risk ?? {}).map(([code, r]: [string, any]) => (
                <div key={code}>- {code}：{r.status}；特征 {r.features}，缺口 {r.gaps}
                  {(r.anomaly_clues ?? []).length > 0 && `；核查线索 ${(r.anomaly_clues ?? []).join('、')}`}</div>
              ))}
              {Object.entries(run.label_readiness ?? {}).map(([k, v]: [string, any]) => (
                <div key={k}>- {k} 标签：{v.status}；尚未训练模型</div>
              ))}
            </Fold>
            <Fold title="内部附件（随研究目录保存，不进交付包）"
              hint={`${(run.artifacts ?? []).length} 个文件`}>
              <ul className="list-disc pl-4">
                {(run.artifacts ?? []).map(a => (
                  <li key={a.path}><span className="font-mono">{a.path}</span>（{a.bytes.toLocaleString()} 字节）</li>
                ))}
              </ul>
            </Fold>
            <div className="flex flex-wrap gap-2">
              <button onClick={() => doCheck('verify')} disabled={busy !== ''}
                className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs">
                <ShieldCheck className="w-3.5 h-3.5" /> verify（核对字节多hash）
              </button>
              <button onClick={() => doCheck('replay')} disabled={busy !== ''}
                className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs">
                <RotateCcw className="w-3.5 h-3.5" /> replay（用冻结输入重算）
              </button>
              <button onClick={doCard} disabled={busy !== ''}
                className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs">
                <FileText className="w-3.5 h-3.5" /> 冻结假说卡（待人工确认）
              </button>
            </div>
            {check && (
              <Alert tone={check.ok ? 'info' : 'danger'}
                title={`${check.kind}：${check.ok ? '通过' : '未通过'}`}>
                <pre className="whitespace-pre-wrap break-all">{JSON.stringify(check, null, 1).slice(0, 1200)}</pre>
              </Alert>
            )}
            {card && (
              <Fold title="假说卡（研究草稿，未经人工确认）" defaultOpen
                hint={String(card.card?.card_hash ?? '').slice(0, 12)}>
                <pre className="whitespace-pre-wrap">{String(card.markdown_text ?? '').slice(0, 4000)}</pre>
              </Fold>
            )}
            {summary && (
              <Fold title="摘要（summary.md）" hint={`${summary.length} 字符`}>
                <pre className="whitespace-pre-wrap">{summary.slice(0, 4000)}</pre>
              </Fold>
            )}
          </div>
        )}
      </Card>

      <Card>
        <SectionTitle title="④ 已保存的内部研究产物" extra={`${studies.length} 个`} />
        {studies.length === 0 ? (
          <EmptyState title="还没有产物"
            description="运行一次研究后，内部产物会保存在 tmp/research-studies/<研究ID前16位>/；旧目录不会被覆盖。" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-slate-500"><tr>
                <th className="text-left">研究ID</th><th className="text-left">特征截止</th>
                <th className="text-left">模拟</th><th className="text-left">状态</th>
                <th className="text-left">人工</th><th className="text-right">附件</th>
              </tr></thead>
              <tbody className="text-slate-300">
                {studies.map(s => (
                  <tr key={s.dir ?? s.study_id} className="border-t border-slate-800">
                    <td className="font-mono py-1">{String(s.study_id).slice(0, 16)}…</td>
                    <td>{s.feature_cutoff || '—'}</td>
                    <td>{s.synthetic ? '模拟' : '真实'}</td>
                    <td>{s.research_state || '—'}</td>
                    <td>{s.human_review || '—'}</td>
                    <td className="text-right tabular-nums">{(s.artifacts ?? []).length}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
