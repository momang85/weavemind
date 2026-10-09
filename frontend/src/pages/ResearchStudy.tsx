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
/** 特殊用途标记：整份替换模板的真实研究规格（spec.json），不继承样例的 synthetic/元数据。 */
const SPEC_ROLE = '__spec__'
type FileRole = Role | typeof SPEC_ROLE | ''

const ROLE_LABEL: Record<Role, string> = {
  financial: '财务宽表（三表科目）',
  market: '日行情',
  events: '新闻/公告',
  calendar: '交易日历',
  credit_labels: '信用标签',
  fraud_labels: '反欺诈标签',
}

function guessRole(name: string): FileRole {
  const n = name.toLowerCase()
  if (/study.*\.(json)$/.test(n) || /(^|[-_])spec([-_.]|\.)/.test(n)) return SPEC_ROLE
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
  /** Q1 研究层窄载荷（web_ui._study_q1_payload）：卡主因句/后续页/目标核查/3×3 网格；
   *  数值句来自 ModelRun 读数装配，前端只渲染，不重算、不硬编码公司名。 */
  q1?: {
    method_version?: string; eval_version?: string
    provenance_kind?: string; provenance_note?: string
    target_year?: string; split_at?: string
    feature_cutoff?: string; evaluation_as_of?: string
    eval_direction?: string; eval_verdict?: string; eval_reason?: string
    holdout_common_n?: number
    cards?: Record<string, any>
    followups?: Record<string, any>
    joint_grids?: Record<string, any>
  }
  previews?: Record<string, any>
  spec?: any
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

/** 显示格式（亿）：只在渲染时取整，冻结值保持原始精度。与 quant_research.joint_grid._yi 同一语义；
 *  目标核查（T/实际/gap）按规划锚点用 6 位小数，网格 P/P−T/C 用 4 位小数。 */
const yi4 = (v: any): string => {
  const n = Number(v)
  if (v === null || v === undefined || v === '' || !isFinite(n)) return '—'
  return (n / 1e8).toLocaleString('zh-CN', { minimumFractionDigits: 4, maximumFractionDigits: 4 })
}
const yi6 = (v: any): string => {
  const n = Number(v)
  if (v === null || v === undefined || v === '' || !isFinite(n)) return '—'
  return (n / 1e8).toLocaleString('zh-CN', { minimumFractionDigits: 6, maximumFractionDigits: 6 })
}
/** 周期标签：源值可能已带「年」（如 2023年），去掉尾字避免「2023年 年」；与 study._q1_period_label 同一纪律。 */
const periodLabel = (v: any): string => {
  const s = String(v ?? '')
  return s.endsWith('年') ? s.slice(0, -1) : s
}
/** g 显示为 %（收入增速），m 显示为 pp（毛利率变化）——与 joint_grid.markdown 同一记法。 */
const gLabel = (v: number): string => `${v * 100 >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`
const mLabel = (v: number): string => `${v * 100 >= 0 ? '+' : ''}${(v * 100).toFixed(2)}pp`

/** 翻转轴证据问题（§18.10）：从本格九点里算哪条轴出现「达到/未达到」两侧——
 *  提示最值得补证的条件；数值全部来自本格敏感性，不外推，业务措辞仍待核。 */
function axisFlips(grid: any): { gAxis: boolean; mAxis: boolean } {
  const cells: any[] = (grid?.cells ?? []).filter((c: any) =>
    c?.profit_state === '达到研究者目标' || c?.profit_state === '未达到研究者目标')
  let gAxis = false; let mAxis = false
  for (const a of cells) for (const b of cells) {
    if (a === b) continue
    if (a.m === b.m && a.g !== b.g && a.profit_state !== b.profit_state) gAxis = true
    if (a.g === b.g && a.m !== b.m && a.profit_state !== b.profit_state) mAxis = true
  }
  return { gAxis, mAxis }
}

export default function ResearchStudy() {
  const [sample, setSample] = useState<any>(null)
  const [studies, setStudies] = useState<any[]>([])
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState('')
  const [inputId, setInputId] = useState('__sample__')
  const [files, setFiles] = useState<StudyFile[]>([])
  const [roles, setRoles] = useState<Record<string, FileRole>>({})
  const [cutoff, setCutoff] = useState('2025-04-30')
  const [asOf, setAsOf] = useState('2025-06-30')
  const [preview, setPreview] = useState<StudyPayload | null>(null)
  const [run, setRun] = useState<StudyPayload | null>(null)
  const [check, setCheck] = useState<any>(null)
  const [card, setCard] = useState<any>(null)
  const [confirmed, setConfirmed] = useState<string[]>([])
  /** ⑥ 改参编辑态：预填自 preview.spec.financial[code].joint_grid；留空预算=未设（不自动给可行绿灯）。 */
  const [gridEdits, setGridEdits] = useState<Record<string,
    { g: string; m: string; cdays: string; idays: string; budget: string }>>({})

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

  /** 组 spec：样例模板 + 上传文件按角色替换；只放共享参数，模型参数留在模板里。
   *  一旦把某个上传文件标成 SPEC_ROLE（真实研究规格），就只发文件名：
   *  服务端从本次输入目录读该文件，synthetic/元数据/模型参数以文件为准，不继承样例默认。 */
  const specFileName = (): string => {
    if (inputId === '__sample__') return ''
    const hit = files.find(f => roles[f.name] === SPEC_ROLE)
    return hit?.name ?? ''
  }
  const buildSpec = () => {
    const sf = specFileName()
    if (sf) return sf
    const base = sample?.spec ? JSON.parse(JSON.stringify(sample.spec)) : null
    if (!base) return 'sample'
    if (inputId === '__sample__') return 'sample'
    for (const key of Object.keys(ROLE_LABEL) as Role[]) delete (base as any)[key]
    for (const f of files) {
      const role = roles[f.name]
      if (role && role !== SPEC_ROLE) (base as any)[role] = { ...(base as any)[role], file: f.name }
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
      // 显示以解析后的 spec 为准（显式 spec 文件自带截止；内联模板回显页面参数）
      if (d.spec?.feature_cutoff) setCutoff(d.spec.feature_cutoff)
      if (d.spec?.evaluation_as_of) setAsOf(d.spec.evaluation_as_of)
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  const doRun = async () => {
    setBusy('run'); setErr(''); setCheck(null); setCard(null)
    try {
      const spec = buildSpec()
      const body: any = { spec, input_id: inputId, confirmed: true,
                         confirmed_subjects: confirmed }
      // 显式 spec 文件：截止以文件为准（不静默覆盖），请求体不带截止；
      // 内联模板模式才允许由页面参数决定截止。
      if (typeof spec !== 'string') {
        body.feature_cutoff = cutoff
        body.evaluation_as_of = asOf
      }
      const { ok, d } = await post('/api/research-study/run', body)
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

  /** ⑥ 改参：只覆盖 financial[code].joint_grid（克隆最新 spec），其余冻结不动——
   *  新参数 → 新 study_id（内容寻址），旧目录不覆盖。 */
  const gridOf = (code: string): any | null => {
    const specFin = ((shown ?? preview)?.spec?.financial ??
      preview?.spec?.financial) as Record<string, any> | undefined
    const g = specFin?.[code]?.joint_grid
    if (g && typeof g === 'object' && !Array.isArray(g)) return g
    const qg = (shown?.q1?.joint_grids ?? {})[code]
    return (qg && typeof qg === 'object' && !Array.isArray(qg)) ? qg : null
  }
  /** 纯读：编辑态优先，否则从网格预填（不在渲染期写 state）。 */
  const editOf = (code: string) => {
    if (gridEdits[code]) return gridEdits[code]
    const g = gridOf(code)
    if (!g) return null
    return {
      g: (g.growth_values ?? []).map((v: number) => `${v * 100}`).join(', '),
      m: (g.margin_deltas ?? []).map((v: number) => `${v * 100}`).join(', '),
      cdays: String(g.collection_days_delta ?? 10), idays: String(g.inventory_days_delta ?? 10),
      budget: g.budget?.value != null ? String(g.budget.value) : '',
    }
  }
  const setEdit = (code: string, patch: Partial<{ g: string; m: string; cdays: string; idays: string; budget: string }>) => {
    setGridEdits(prev => ({ ...prev, [code]: { ...(editOf(code) ?? { g: '', m: '', cdays: '10', idays: '10', budget: '' }), ...patch } }))
  }
  const pctList = (raw: string): number[] | null => {
    const vs = raw.split(/[,，\s]+/).filter(Boolean).map(s => Number(s) / 100)
    return (vs.length > 0 && vs.every(v => isFinite(v))) ? vs : null
  }
  /** 同一组 spec 带 new joint_grid 重新 run：服务端内容寻址生成新 study_id，旧目录不覆盖。 */
  const doRerunGrid = async (code: string) => {
    const g = gridOf(code); const e = editOf(code)
    if (!g || !e) return
    const growth = pctList(e.g)
    const margin = pctList(e.m)
    if (!growth || !margin) { setErr('g / m 请填逗号分隔的百分数，例如 0, 10, 20（三一：-10, -5, 0）'); return }
    const cdays = Number(e.cdays); const idays = Number(e.idays)
    if (!isFinite(cdays) || !isFinite(idays)) { setErr('天数须为数字（回款/库存，-30..30）；0 表示该维度冲击关闭'); return }
    const budget = e.budget.trim() === '' ? null : Number(e.budget)
    if (budget !== null && (!isFinite(budget) || budget < 0)) { setErr('预算 B 须为非负数（元）；留空=未设'); return }
    setBusy('rerun'); setErr('')
    try {
      const spec = JSON.parse(JSON.stringify((shown ?? preview)?.spec ?? preview?.spec ?? {}))
      if (!spec.financial?.[code]) throw new Error('spec 里没有该公司的 financial 节')
      spec.financial[code].joint_grid = {
        ...g, growth_values: growth, margin_deltas: margin,
        collection_days_delta: cdays, inventory_days_delta: idays,
        budget,
      }
      const body: any = { spec, input_id: inputId, confirmed: true, confirmed_subjects: confirmed }
      if (typeof spec !== 'string') { body.feature_cutoff = cutoff; body.evaluation_as_of = asOf }
      const { ok, d } = await post('/api/research-study/run', body)
      if (!ok) {
        throw new Error(`${d.error || '运行失败'}`
          + (d.subjects_in_files ? `（文件里读到：${(d.subjects_in_files || []).join('、')}）` : ''))
      }
      setRun(d); loadStudies()
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }
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
                          onChange={e => setRoles({ ...roles, [f.name]: e.target.value as FileRole })}>
                          <option value="">不使用</option>
                          <option value={SPEC_ROLE}>研究规格（spec.json 整份为准）</option>
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
              disabled={!!specFileName()}
              className="mt-1 w-full bg-slate-900 border border-slate-700 rounded-lg px-2 py-1.5 text-slate-200 disabled:opacity-50" />
          </label>
          <label className="text-xs text-slate-400">结果评价截止（evaluation_as_of）
            <input value={asOf} onChange={e => setAsOf(e.target.value)}
              disabled={!!specFileName()}
              className="mt-1 w-full bg-slate-900 border border-slate-700 rounded-lg px-2 py-1.5 text-slate-200 disabled:opacity-50" />
          </label>
          <div className="text-xs text-slate-400">预览后需逐个勾选确认的**文件内主体**
            <div className="mt-1 text-slate-500">公司身份只从文件读，不按请求里的名字替换材料</div>
          </div>
        </div>
        {specFileName() ? (
          <div className="mb-3 rounded-lg border border-cyan-500/30 bg-cyan-500/5 px-3 py-2 text-xs text-cyan-200 space-y-1">
            <div>已选真实研究规格：<span className="font-mono">{specFileName()}</span> —— synthetic / 元数据（来源、材料、投影版本）/ 模型与参数全部以该文件为准，不继承模拟样例默认。</div>
            <div className="text-slate-400">两个截止以规格文件为准；页面输入框已停用，避免与规格冲突（服务端也会拒绝冲突的截止，400）。</div>
          </div>
        ) : inputId !== '__sample__' && (
          <div className="mb-3 text-xs text-slate-500">
            上传了整份研究规格（study spec JSON）？在上方表格把它标成「研究规格」，synthetic/元数据以文件为准。
          </div>
        )}
        <button onClick={doPreview} disabled={busy === 'preview'}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm disabled:opacity-50">
          <Eye className="w-4 h-4" /> {busy === 'preview' ? '预览中…' : '文件预览（只读）'}
        </button>

        {preview && (
          <div className="mt-3 text-xs space-y-1">
            <div className="text-slate-400">本次所用规格：schema {String(preview.spec?.schema ?? '—')} · 名称 {String(preview.spec?.name ?? '—')} ·
              {' '}synthetic <span className={preview.spec?.synthetic ? 'text-amber-300' : 'text-emerald-300'}>{String(preview.spec?.synthetic ?? '—')}</span> ·
              {' '}特征截止 {String(preview.spec?.feature_cutoff ?? '—')} · 评价截止 {String(preview.spec?.evaluation_as_of ?? '—')}</div>
            {preview.spec?.financial?.metadata && (
              <div className="text-slate-500">财务元数据：来源 {String(preview.spec.financial.metadata.source ?? '—')} ·
                {' '}投影版本 {String(preview.spec.financial.metadata.projection_version ?? '—')} ·
                {' '}材料数 {Array.isArray(preview.spec.financial.metadata.materials) ? preview.spec.financial.metadata.materials.length : '—'}</div>
            )}
            {preview.spec?.financial?.models && (
              <div className="text-slate-500">模型：{(preview.spec.financial.models as any[]).map((m: any) => m.model_id ?? m).join('、') || '—'}</div>
            )}
          </div>
        )}

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
                  <li key={a.path}>
                    <a className="font-mono text-cyan-300 hover:text-cyan-200 underline decoration-dotted"
                      href={`/api/research-study/file?id=${encodeURIComponent(String(run.study_id ?? ''))}&name=${encodeURIComponent(a.path)}`}
                      download>{a.path}</a>（{a.bytes.toLocaleString()} 字节）
                  </li>
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
        <SectionTitle icon={<FileText className="w-4 h-4 text-cyan-400" />}
          title="④ Q1 读数：3×3「利润目标＋天数冲击占用」网格 / 目标核查 / 补证提示"
          extra="数值句从 ModelRun 读数装配；改一格只重算本格" />
        {!shown?.q1 || Object.keys(shown.q1.joint_grids ?? {}).length === 0 ? (
          <EmptyState title="这份产物没有 Q1 网格"
            description="只有带 q1.joint_grid 块的研究才有 3×3 读数；旧产物保持原样，不回填。" />
        ) : (
          <div className="space-y-3">
            <div className="text-xs text-slate-500">
              方法 <span className="font-mono">{shown.q1.method_version || '—'}</span>
              ；评价 <span className="font-mono">{shown.q1.eval_version || '—'}</span>
              ；特征截止 {shown.q1.feature_cutoff || '—'}；评价截止 {shown.q1.evaluation_as_of || '—'}
              ；切分 {shown.q1.split_at || '—'}；性质 **{shown.q1.provenance_kind || '—'}**
              {shown.q1.provenance_note ? `（${shown.q1.provenance_note}）` : ''}
            </div>
            {Object.entries(shown.q1.joint_grids ?? {}).map(([code, g]: [string, any]) => {
              const flips = axisFlips(g)
              const t = g.target ?? {}
              return (
                <Fold key={code} defaultOpen title={`${code}　网格`}
                  hint={`基期 t=${g.base_period ?? '—'}；${(g.growth_values ?? []).length}×${(g.margin_deltas ?? []).length} 格`}>
                  <div className="space-y-2">
                    <div className="text-slate-300">
                      研究者目标 T：{periodLabel(t.period)} 年 {t.metric} = <span className="tabular-nums">{yi6(t.value)}</span> 亿
                      （{t.source || '冻结读数'}）——{t.goal_note || '研究者设定'}；
                      <span className="text-amber-300">{t.nature || '不是管理层指引，也不构成业绩承诺'}</span>。
                      传入方式：{t.passed_as || '—'}。
                    </div>
                    <div className="text-slate-400">
                      天数冲击：回款 {g.collection_days_delta} 日、库存 {g.inventory_days_delta} 日（使用者假设，可切换 0）
                      ；毛利线以下净额按基期结构冻结（below_gross_mode={g.below_gross_mode}）。
                      {g.budget?.value != null
                        ? <> 预算 B：<span className="tabular-nums">{yi4(g.budget.value)}</span> 亿——{g.budget.label}</>
                        : ' 预算 B：未设——联合状态记「未设预算」，利润状态照算、不自动给可行绿灯。'}
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead className="text-slate-500">
                          <tr><th className="text-left">g＼m</th>
                            {(g.margin_deltas ?? []).map((m: number) => <th key={m} className="text-left">m={mLabel(m)}</th>)}
                          </tr></thead>
                        <tbody>
                          {(g.growth_values ?? []).map((rv: number) => (
                            <tr key={rv} className="border-t border-slate-800">
                              <td className="py-1 pr-2 text-slate-400">g={gLabel(rv)}</td>
                              {(g.margin_deltas ?? []).map((m: number) => {
                                const c = (g.cells ?? []).find((x: any) => x.g === rv && x.m === m)
                                if (!c || c.status === 'unavailable') {
                                  return <td key={m} className="py-1 pr-3 text-slate-600">
                                    不可算{c?.reason ? `（${String(c.reason).slice(0, 40)}）` : ''}</td>
                                }
                                const hit = c.profit_state === '达到研究者目标'
                                return (
                                  <td key={m} className={`py-1 pr-3 align-top ${hit ? 'text-emerald-300' : 'text-slate-300'}`}>
                                    <div className="tabular-nums">P {yi4(c.P)}；P−T {yi4(c.P_minus_T)}；C {yi4(c.C)}</div>
                                    <div className="text-[11px]">
                                      {c.profit_state}
                                      {c.budget_headroom != null
                                        ? `；B−C ${yi4(c.budget_headroom)}；${c.joint_state}`
                                        : (c.joint_state ? `；${c.joint_state}` : '')}
                                    </div>
                                    {c.status === 'partial' && c.reason
                                      && <div className="text-[11px] text-amber-400">{String(c.reason).slice(0, 60)}</div>}
                                  </td>
                                )
                              })}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="text-xs text-slate-400">
                      单位：亿（P−T、B−C 同单位）；显示才四舍五入，冻结值保持原始精度；
                      C 只含天数冲击的增量占款，不与利润相加。
                    </div>
                    <div className="text-xs text-slate-300">
                      最值得补证的条件（由本格九点算出的证据问题，不是概率、不是信息价值声明）：
                      {flips.gAxis && flips.mAxis
                        ? ' 两条轴上同时出现「达到/未达到」两侧——收入恢复与毛利改善都需要证据；'
                        : flips.gAxis
                          ? ' 仅沿收入增速 g 轴翻转：补证聚焦**收入恢复的销量/实现价格**；'
                          : flips.mAxis
                            ? ' 仅沿毛利率 m 轴翻转：补证聚焦**毛利改善的产品/出口结构**；'
                            : ' 九格内未出现状态翻转：本轮敏感性不构成补证提示。'}
                      <span className="text-slate-500">业务解释仍标待核。</span>
                    </div>
                    {(g.notes ?? []).map((n: string, i: number) => (
                      <div key={i} className="text-[11px] text-slate-500">- {n}</div>
                    ))}
                  </div>
                </Fold>
              )
            })}

            {Object.entries(shown.q1.followups ?? {}).map(([code, f]: [string, any]) => {
              const tf = f.target_followup
              const act = f.actual ?? {}
              return (
                <Fold key={code} title={`${code}　后续页与目标核查`}
                  hint={`${f.status || '—'}${f.mechanism_verdict ? ` / ${f.mechanism_verdict}` : ''}`}>
                  <div className="space-y-2">
                    <div>方向核查：**{f.status}**（{f.mechanism_verdict || '—'}）
                      {f.verify_by ? `；下一核查时点 ${f.verify_by}` : ''}。</div>
                    {f.status === '待数据' ? (
                      <div className="text-amber-400">{f.pending_reason || '下一同口径年度尚未取得：不写实际读数。'}</div>
                    ) : (
                      <div className="tabular-nums">同口径实际：
                        {act.period ?? ''} {act.metric ?? ''} = {yi4(act.value)} 亿
                        {act.disclosed_at ? `（披露 ${act.disclosed_at}）` : ''}。</div>
                    )}
                    {(f.next_check ?? []).length > 0 && (
                      <ul className="list-disc pl-5 text-slate-400">
                        {f.next_check.map((x: string, i: number) => <li key={i}>{x}</li>)}
                      </ul>
                    )}
                    {f.base_fingerprint && (
                      <div className="text-[11px] text-slate-500">基期实质指纹
                        <span className="font-mono"> {f.base_fingerprint}</span>
                        ：只绑定主体/年度/特征截止/基期来源/参数方法/读数判定，排除后续与全研究ID。</div>
                    )}

                    {tf && (
                      <div className="border-t border-slate-800 pt-2">
                        <div className="text-slate-300">目标核查（{periodLabel(tf.period)} 年 {tf.metric_label}）：**{tf.status}**</div>
                        <div className="text-[11px] text-slate-500">{tf.nature}；口径 {tf.caliber}。</div>
                        {tf.status === '待数据' ? (
                          <div className="text-amber-400">{tf.pending_reason || '评价截止前没有同口径读数：保持待数据，不写实际读数'}</div>
                        ) : (
                          <div className="space-y-1">
                            <div className="tabular-nums">目标 T（{periodLabel((tf.target ?? {}).period)} 年）：
                              {yi6((tf.target ?? {}).value)} 亿 —— {(tf.target ?? {}).goal_note || ''}</div>
                            <div className="tabular-nums">目标期实际（{periodLabel(act.period ?? tf.period)} 年）：
                              {yi6((tf.actual ?? {}).value)} 亿；差额 P−T 口径的 gap =
                              <span className="tabular-nums"> {yi6(tf.gap)}</span> 亿</div>
                            <div className="text-slate-300">结论：**{tf.verdict}**。</div>
                            <div className="text-[11px] text-slate-500">
                              来源 {String((tf.actual ?? {}).source || '')}
                              {(tf.actual ?? {}).fact_id ? <>；事实 <span className="font-mono">{String((tf.actual ?? {}).fact_id)}</span></> : null}
                              {(tf.actual ?? {}).source_url
                                ? <>；原件 {(tf.actual ?? {}).source_url}</> : null}
                              {((tf.actual ?? {}).material_ids ?? []).length > 0
                                ? <>；材料ID {(tf.actual ?? {}).material_ids.join('、')}</> : null}
                              {(tf.actual ?? {}).disclosed_at ? `；披露 ${String((tf.actual ?? {}).disclosed_at)}` : ''}
                              {tf.verified_at ? `；核查 ${String(tf.verified_at)}` : ''}
                            </div>
                          </div>
                        )}
                        <div className="text-[11px] text-slate-500">{tf.note}</div>
                        {tf.base_fingerprint && (
                          <div className="text-[11px] text-slate-500">对照基期指纹
                            <span className="font-mono"> {tf.base_fingerprint}</span></div>
                        )}
                      </div>
                    )}
                  </div>
                </Fold>
              )
            })}

            {Object.entries(shown.q1.cards ?? {}).map(([code, c]: [string, any]) => (
              <div key={code} className="text-xs text-slate-400">
                {code}（{c.entity || '—'}）主因句：{c.primary_cause || '—'}
                ；预期方向 {c.expected_direction || '—'}；性质 {c.provenance_kind || '—'}。
              </div>
            ))}
            <div className="text-[11px] text-slate-500">
              增量评价：{shown.q1.eval_verdict || '—'}
              {shown.q1.eval_direction ? `（方向 ${shown.q1.eval_direction}）` : ''}
              ；共同有效样本 n={String(shown.q1.holdout_common_n ?? '—')}。
              {shown.q1.eval_reason ? ` 原因：${shown.q1.eval_reason}` : ''}
              —— 这是机器验收层的读数，不是人工评审结论。
            </div>
          </div>
        )}
      </Card>

      <Card>
        <SectionTitle icon={<RotateCcw className="w-4 h-4 text-cyan-400" />}
          title="⑤ 同一入口改参：只改这家的网格假设，产出新版并重放"
          extra="新版=新研究ID；旧目录与旧卡不回填" />
        <div className="text-xs text-slate-500 mb-2">
          改一格只重算那一格：克隆当前 spec 的 financial[code].joint_grid 后覆盖 g/m/天数/预算，
          再走同一条 run 通道；输入目录与特征截止不动，所以基期实质指纹（base_fingerprint）应与旧版相同。
          百分比填数：0, 10, 20 表示 0%/+10%/+20%；三一填 -10, -5, 0。预算留空 = 未设。
        </div>
        {Object.entries(shown?.q1?.joint_grids ?? {}).map(([code]: [string, any]) => {
          const e = editOf(code)
          if (!e) return null
          return (
            <div key={code} className="border border-slate-800 rounded-xl p-3 mb-2">
              <div className="text-sm text-slate-200 mb-2">{code}</div>
              <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-xs">
                <label className="space-y-1">收入增速 g（%）
                  <input value={e.g} aria-label={`${code} g`}
                    onChange={ev => setEdit(code, { g: ev.target.value })}
                    className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1" /></label>
                <label className="space-y-1">毛利率变化 m（pp）
                  <input value={e.m} aria-label={`${code} m`}
                    onChange={ev => setEdit(code, { m: ev.target.value })}
                    className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1" /></label>
                <label className="space-y-1">回款天数冲击（日）
                  <input value={e.cdays} aria-label={`${code} 回款天数`}
                    onChange={ev => setEdit(code, { cdays: ev.target.value })}
                    className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1" /></label>
                <label className="space-y-1">库存天数冲击（日）
                  <input value={e.idays} aria-label={`${code} 库存天数`}
                    onChange={ev => setEdit(code, { idays: ev.target.value })}
                    className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1" /></label>
                <label className="space-y-1">预算 B（元，留空=未设）
                  <input value={e.budget} aria-label={`${code} 预算`}
                    onChange={ev => setEdit(code, { budget: ev.target.value })}
                    className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1" /></label>
              </div>
              <button onClick={() => doRerunGrid(code)} disabled={busy !== '' || !allConfirmed}
                title={allConfirmed ? '' : '先预览并逐个确认文件里的公司'}
                className="mt-2 inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 text-xs disabled:opacity-40">
                <Play className="w-3.5 h-3.5" /> {busy === 'rerun' ? '重算中…' : `以新参数重跑并保存新版（${code}）`}
              </button>
              {!allConfirmed && <span className="ml-2 text-xs text-slate-500">需先「文件预览」并确认全部主体</span>}
            </div>
          )
        })}
        {run?.study_id && (
          <div className="text-xs text-slate-400 mt-1">
            新版研究ID <span className="font-mono">{String(run.study_id).slice(0, 16)}…</span>
            ：下载走 ⑥ 的产物清单（<span className="font-mono">q1.json</span> /
            <span className="font-mono"> q1_followup_*.md</span> /
            <span className="font-mono"> financial.json</span>），再点 verify / replay 复算核对。
          </div>
        )}
      </Card>

      <Card>
        <SectionTitle title="⑥ 已保存的内部研究产物" extra={`${studies.length} 个`} />

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
                    <td className="text-right tabular-nums align-top">
                      <details>
                        <summary className="cursor-pointer list-none">{(s.artifacts ?? []).length} 件</summary>
                        <ul className="text-left mt-1 space-y-0.5">
                          {((s.artifacts ?? []) as { path: string }[]).map(a => (
                            <li key={a.path}>
                              <a className="text-cyan-300 hover:text-cyan-200 underline decoration-dotted"
                                href={`/api/research-study/file?id=${encodeURIComponent(String(s.study_id))}&name=${encodeURIComponent(a.path)}`}
                                download>{a.path}</a>
                            </li>
                          ))}
                        </ul>
                      </details>
                    </td>
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
