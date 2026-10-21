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
import { humanReviewLabel, researchStateLabel } from '../lib/statusMeta'

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
  /** 三态（C2a 接缝①）：机器验收 / 研究状态 / 人工复核，各按本版报告的实际绑定。 */
  states?: {
    machine?: { status?: string; basis?: string }
    research?: { status?: string; state?: string; reason?: string; basis?: string; stale?: boolean }
    human?: { status?: string; basis?: string }
  }
  /** 恢复事实（接缝②）：按研究身份从冻结 inputs 恢复时的两条恢复记录。 */
  restored_from?: string; result_hash_matches?: boolean; restore_basis?: string
  recomputed?: boolean
  /** 改参事实（C2b 接缝②分叉）：冻结输入＋改参的新候选，逐路径差异与来源研究。 */
  reparam_from?: { study_id?: string; dir?: string; result_hash?: string
    frozen_integrity_ok?: boolean; frozen_impl_version?: string }
  reparam_spec_diff?: string[]
  reparam_basis?: string
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
  /** B4 改参编辑态：g/m/B 按公司划分；天数是**两公司共同假设**，单独放 sharedDays。
   *  预填自冻结 spec.financial.joint_grid（块级 grids[code]）；留空预算=未设（不自动给值）。 */
  const [gridEdits, setGridEdits] = useState<Record<string, { g: string; m: string; budget: string }>>({})
  const [sharedDays, setSharedDays] = useState<{ cdays: string; idays: string } | null>(null)
  /** B4 直连：显式选定的研究（⑦ 列表里点「选定」）；没有隐式「最新研究」，也不重新上传。 */
  const [selectedStudyId, setSelectedStudyId] = useState('')
  const [report, setReport] = useState<any>(null)

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
      setRun(d)
      // 单选源推进（架构师：run/exists 后推进新选择）：run 成功 → 当前选版切到
      // 新研究；spec 跟随 run.spec（本次生效值），不回跳旧冻结值；改参编辑态清空。
      if (d.study_id) setSelectedStudyId(String(d.study_id))
      setGridEdits({}); setSharedDays(null)
      loadStudies()
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  /** 接缝②（2026-10-11 §18.12）：按**研究身份**恢复重跑——显式 study_id + result_hash，
   *  服务端从冻结 inputs 走原 _compute_study（实现已改变时不重算、直接读冻结产物）；
   *  不重新上传、不借用旧上传目录的输入状态；冷页面 inputId 停在 __sample__ 也能跑。
   *  确认主体来自选定研究的冻结产物（与正常分支同一硬门槛）；页面上的参数改动不带走。 */
  const doRestoreRun = async () => {
    const s = selStudy
    if (!s?.study_id || !s?.result_hash) { setErr('先在 ⑦ 历史研究列表里点「选定」'); return }
    setBusy('restore'); setErr(''); setCheck(null); setCard(null)
    try {
      const body: any = { study_id: s.study_id, result_hash: s.result_hash, confirmed: true,
                          confirmed_subjects: s.subjects ?? [] }
      const { ok, d } = await post('/api/research-study/run', body)
      if (!ok) {
        throw new Error(`${d.error || '恢复失败'}`
          + (d.subjects_in_files ? `（冻结产物里读到：${(d.subjects_in_files || []).join('、')}）` : ''))
      }
      setRun(d)
      // 恢复即同版：选定保持该研究；spec 跟随恢复载荷（冻结 spec 原样），不回跳页面参数。
      if (d.spec?.feature_cutoff) setCutoff(d.spec.feature_cutoff)
      if (d.spec?.evaluation_as_of) setAsOf(d.spec.evaluation_as_of)
      setGridEdits({}); setSharedDays(null); setReport(null)
      loadStudies()
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  const doCheck = async (kind: 'verify' | 'replay') => {
    const sid = run?.study_id || preview?.study_id || selectedStudyId
    if (!sid) { setErr('还没有已保存的研究产物'); return }
    setBusy(kind); setErr('')
    try {
      const { ok, d } = await post(`/api/research-study/${kind}`, { study_id: sid })
      if (!ok) throw new Error(d.error || `${kind} 失败`)
      setCheck({ kind, ...d })
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  const doCard = async () => {
    const sid = run?.study_id || preview?.study_id || selectedStudyId
    if (!sid) { setErr('还没有已保存的研究产物'); return }
    setBusy('card'); setErr('')
    try {
      const { ok, d } = await post('/api/research-study/hypothesis-card', { study_id: sid })
      if (!ok) throw new Error(d.error || '生成假说卡失败')
      setCard(d)
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  const shown = run ?? preview

  /** B4 单选源（架构师 2026-10-10 修复）：selectedStudyId 是**唯一**当前选版源——
   *  run/exists 成功后推进 selectedStudyId（doRun/doRerunGrid），所以进行中的
   *  run/preview spec 是「当前值」，选定研究的冻结 spec 只在其后回显兜底；
   *  selectedStudyId 为空时不取 studies[0]（没有隐式「最新研究」）。 */
  const selStudy = useMemo(
    () => studies.find((s: any) => s.study_id === selectedStudyId) ?? null,
    [studies, selectedStudyId])
  const activeSpec: any = run?.spec ?? preview?.spec ?? selStudy?.spec ?? null
  /** 主体与确认：进行中预览优先，否则用选定研究**冻结的主体回填确认**——
   *  恢复选定 study 的确认后，不重新上传/预览也能改参重跑（服务端按文件主体核对）。 */
  const subjects: string[] = preview?.subjects ?? selStudy?.subjects ?? []
  const allConfirmed = subjects.length > 0 && confirmed.length === subjects.length
  /** 网格声明来源：冻结规范的块级 grids[code]（B3 形状）优先；仅当规范没有网格声明时回退运行结果。 */
  const gridCodes = useMemo(() => {
    const block = (activeSpec?.financial ?? {})?.joint_grid
    if (block && typeof block === 'object' && !Array.isArray(block)
      && block.grids && typeof block.grids === 'object') return Object.keys(block.grids)
    return Object.keys((shown?.q1?.joint_grids ?? {}) as Record<string, any>)
  }, [activeSpec, shown])

  /** 网格读取（纯函数，不在渲染期写 state）：单一来源 activeSpec ——
   *  块级 grids[code]（B3 冻结形状）→ 旧形状 financial[code].joint_grid → 运行结果 q1 回显。 */
  const gridOf = (code: string): any | null => {
    const specFin = activeSpec?.financial as Record<string, any> | undefined
    const block = specFin?.joint_grid
    if (block && typeof block === 'object' && !Array.isArray(block)) {
      const g = (block.grids ?? {})[code]
      if (g && typeof g === 'object' && !Array.isArray(g)) return g
    }
    const g2 = specFin?.[code]?.joint_grid
    if (g2 && typeof g2 === 'object' && !Array.isArray(g2)) return g2
    const qg = (shown?.q1?.joint_grids ?? {})[code]
    return (qg && typeof qg === 'object' && !Array.isArray(qg)) ? qg : null
  }
  /** 天数是两公司共同假设：编辑态优先，否则从 activeSpec 的块级值预填。 */
  const sharedDaysOf = (): { cdays: string; idays: string } => {
    if (sharedDays) return sharedDays
    const block = (activeSpec?.financial ?? {})?.joint_grid
    if (block && typeof block === 'object' && !Array.isArray(block)) {
      return { cdays: String(block.collection_days_delta ?? 10),
               idays: String(block.inventory_days_delta ?? 10) }
    }
    const qg: any = Object.values(shown?.q1?.joint_grids ?? {})[0]
    return { cdays: String(qg?.collection_days_delta ?? 10), idays: String(qg?.inventory_days_delta ?? 10) }
  }
  const setShared = (patch: Partial<{ cdays: string; idays: string }>) => {
    setSharedDays(prev => ({ ...sharedDaysOf(), ...(prev ?? {}), ...patch }))
  }
  /** 纯读：编辑态优先，否则从网格预填（g/m 为百分数文本；
   *  B 兼容冻结 spec 的裸数值与运行结果的 {value} 两种形状）。 */
  const editOf = (code: string) => {
    if (gridEdits[code]) return gridEdits[code]
    const g = gridOf(code)
    if (!g) return null
    const b = g.budget
    return {
      g: (g.growth_values ?? []).map((v: number) => `${v * 100}`).join(', '),
      m: (g.margin_deltas ?? []).map((v: number) => `${v * 100}`).join(', '),
      budget: b != null && typeof b === 'object' ? String(b.value ?? '') : String(b ?? ''),
    }
  }
  const setEdit = (code: string, patch: Partial<{ g: string; m: string; budget: string }>) => {
    setGridEdits(prev => ({ ...prev, [code]: { ...(editOf(code) ?? { g: '', m: '', budget: '' }), ...patch } }))
  }
  const pctList = (raw: string): number[] | null => {
    const vs = raw.split(/[,，\s]+/).filter(Boolean).map(s => Number(s) / 100)
    return (vs.length > 0 && vs.every(v => isFinite(v))) ? vs : null
  }
  /** 同一入口改参（C2b 接缝）：克隆 activeSpec，覆盖**两公司共同假设**的共享天数与本公司
   *  g/m/B，再走同一条 run 通道——服务端内容寻址生成新 study_id，旧目录与旧卡不回填。
   *  不是「改一格只重算那一格」：同一次 run 重算两家；预算 B 留空=未设。
   *  **冷页面不带上传目录时**（inputId 仍是 `__sample__`）：不许拿样例顶冻结研究，
   *  改带 `study_id`(+`result_hash`) —— 服务端走「冻结输入＋改参创建新候选」（同一
   *  `_compute_study`、当前实现、新身份）；只带 `study_id` 才是「查看原版」。 */
  const doRerunGrid = async (code: string) => {
    const g = gridOf(code); const e = editOf(code); const days = sharedDaysOf()
    if (!g || !e) return
    // 主体确认（架构师：恢复选定 study 的确认）：预览确认过用预览的；否则用选定
    // 研究冻结的主体回填——同一输入目录复用（不重新上传），服务端按文件主体核对。
    if (!confirmed.length) {
      const frozen = (selStudy?.subjects ?? []) as string[]
      if (!frozen.length) { setErr('先在 ⑦ 选定研究（自动恢复冻结主体确认），或走 ② 预览并确认全部主体'); return }
      setConfirmed([...frozen])
    }
    // 冻结输入路径：没有上传目录时按研究身份取冻结输入（不静默退回样例）
    const useFrozen = inputId === '__sample__' && !!selStudy?.study_id
    if (!useFrozen && inputId === '__sample__') {
      setErr('当前输入是内置模拟样例：先在 ⑦ 选定研究（冻结输入＋改参）或上传文件，再改参重跑')
      return
    }
    const growth = pctList(e.g)
    const margin = pctList(e.m)
    if (!growth || !margin) { setErr('g / m 请填逗号分隔的百分数，例如 0, 10, 20（三一：-10, -5, 0）'); return }
    const cdays = Number(days.cdays); const idays = Number(days.idays)
    if (!isFinite(cdays) || !isFinite(idays)) { setErr('天数须为数字（回款/库存，-30..30）；0 表示该维度冲击关闭'); return }
    const budget = e.budget.trim() === '' ? null : Number(e.budget)
    if (budget !== null && (!isFinite(budget) || budget < 0)) { setErr('预算 B 须为非负数（元）；留空=未设'); return }
    setBusy('rerun'); setErr('')
    try {
      const spec = JSON.parse(JSON.stringify(activeSpec ?? {}))
      // 服务端契约（quant_research/study.py）：网格声明在 spec.financial.joint_grid ——
      // 块级 collection/inventory_days_delta（两公司共同假设）+ grids[code] 的轴与预算；
      // 这里覆盖共享天数与本公司 grids[code] 的轴与预算，另一家原样保留。
      const block = spec.financial?.joint_grid
      if (!block || typeof block !== 'object') {
        throw new Error('当前 spec 里没有 financial.joint_grid 节：先在 ⑦ 选定含网格声明的研究，或对含网格声明的研究规格做文件预览')
      }
      block.collection_days_delta = cdays
      block.inventory_days_delta = idays
      const grids = (block.grids ?? (block.grids = {})) as Record<string, any>
      grids[code] = { ...(grids[code] ?? {}), growth_values: growth, margin_deltas: margin, budget }
      const body: any = { spec, confirmed: true, confirmed_subjects: confirmed }
      if (useFrozen) {
        // 冻结输入＋改参创建新候选：显式带研究身份；截止不在请求体里改（服务端也会拒）
        body.study_id = selStudy!.study_id
        body.result_hash = selStudy!.result_hash
      } else {
        body.input_id = inputId
        if (typeof spec !== 'string') { body.feature_cutoff = cutoff; body.evaluation_as_of = asOf }
      }
      const { ok, d } = await post('/api/research-study/run', body)
      if (!ok) {
        throw new Error(`${d.error || '运行失败'}`
          + (d.subjects_in_files ? `（文件里读到：${(d.subjects_in_files || []).join('、')}）` : ''))
      }
      setRun(d)
      // 单选源推进（同 doRun）：改参 run 成功 → 选定切到新研究。下一次改参从
      // 新冻结 spec 预填——前一家公司的预算改动与天数改动保留在新 spec 里，不回跳。
      if (d.study_id) setSelectedStudyId(String(d.study_id))
      setGridEdits({}); setSharedDays(null); setReport(null)
      loadStudies()
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  /** B4 直连：从冻结研究生成公司报告。绑定三要素全部来自选定研究（界面没有「最新研究」，
   *  也不重新上传）；改参数会得到新的研究ID，服务端按三要素拒绝绑定不匹配的请求（409 原样显示）。 */
  const doReport = async (entity: string) => {
    const s = selStudy
    if (!s?.study_id || !s?.result_hash) { setErr('先在 ⑦ 历史研究列表里点「选定」'); return }
    setBusy(`report:${entity}`); setErr('')
    try {
      const body = { study_id: s.study_id, entity_id: entity, result_hash: s.result_hash,
                     feature_cutoff: s.feature_cutoff, evaluation_as_of: s.evaluation_as_of }
      const { ok, d } = await post('/api/research-study/report', body)
      if (!ok) { setReport(null); throw new Error(`${d.error || '报告生成失败'}${d.code ? `（${d.code}）` : ''}`) }
      setReport(d); loadStudies()
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }
  const summary = useMemo(() => String(shown?.summary_markdown ?? ''), [shown])

  /** ⑧ 研究实验台（C4）：选研究/主体/run → 视角＋候选项 → 0/50/100% → 旧机制→新披露更新 → 保存/重开。
   *  浏览器只传**标识与枚举**（研究/主体/run/视角/候选项/材料/已存实验 id）：数字、阈值、
   *  路径与公式全部由服务端从冻结结果与登记材料解析（不传任意路径，不隐式取第一个 run/主体）。 */
  const [expEnt, setExpEnt] = useState('')
  const [expEntities, setExpEntities] = useState<string[]>([])
  const [expRuns, setExpRuns] = useState<any[]>([])
  const [expOldMats, setExpOldMats] = useState<any[]>([])
  const [expNewMats, setExpNewMats] = useState<any[]>([])
  const [expCands, setExpCands] = useState<any[]>([])
  const [expSavedList, setExpSavedList] = useState<any[]>([])
  const [expRunId, setExpRunId] = useState('')
  const [expView, setExpView] = useState<'current' | 'yoy'>('current')
  const [expMetric, setExpMetric] = useState('')
  const [expOldMat, setExpOldMat] = useState('')
  const [expNewMat, setExpNewMat] = useState('')
  const [expArt, setExpArt] = useState<any>(null)
  const [expSaved, setExpSaved] = useState<any>(null)
  const [expNote, setExpNote] = useState('')
  const [expBaselineId, setExpBaselineId] = useState('') // C5-3 沿冻结原判：显式选一个已存实验（不现场重建旧判断）
  const [mktOn, setMktOn] = useState(false)              // C5-2 市场对照（可选：1/5/20 交易日三窗口）
  const [mktDs, setMktDs] = useState('')                 // 行情集（登记清单里选的 64 位 id）
  const [mktCal, setMktCal] = useState('')               // 交易日历（登记清单里选）
  const [mktAsOf, setMktAsOf] = useState('')             // 评价时点（YYYY-MM-DD，用户显式选）
  const [mktInv, setMktInv] = useState<any>(null)        // 已登记行情集/日历清单（服务端给）

  /** 换选定研究：⑧ 的候选/结果全部作废（上一个研究的 run 与材料不属于这一份）。 */
  useEffect(() => {
    setExpEnt(''); setExpEntities([]); setExpRuns([]); setExpOldMats([]); setExpNewMats([])
    setExpCands([]); setExpSavedList([]); setExpRunId(''); setExpMetric('')
    setExpOldMat(''); setExpNewMat(''); setExpArt(null); setExpSaved(null); setExpNote('')
    setExpBaselineId(''); setMktOn(false); setMktDs(''); setMktCal(''); setMktAsOf(''); setMktInv(null)
  }, [selectedStudyId])

  const expBody = (extra: any = {}) => ({
    study_id: selStudy?.study_id ?? '', result_hash: selStudy?.result_hash ?? '',
    entity_id: expEnt, ...extra,
  })

  /** 第一步：只取这家研究的**主体清单**（服务端给候选，不隐式取第一个主体）。 */
  const expLoadEntities = async () => {
    const s = selStudy
    if (!s?.study_id || !s?.result_hash) { setErr('先在 ⑦ 历史研究列表里点「选定」'); return }
    setBusy('exp:entities'); setErr(''); setExpNote('')
    try {
      const { ok, d } = await post('/api/research-study/experiment',
        { study_id: s.study_id, result_hash: s.result_hash, action: 'options' })
      if (!ok) throw new Error(d.error || '取研究主体失败')
      setExpEntities(d.entities ?? [])
      setExpEnt(''); setExpRuns([]); setExpCands([]); setExpArt(null); setExpSaved(null)
      setExpOldMats([]); setExpNewMats([]); setExpSavedList([]); setExpRunId(''); setExpMetric('')
      setExpOldMat(''); setExpNewMat('')
      setExpBaselineId(''); setMktOn(false); setMktDs(''); setMktCal(''); setMktAsOf(''); setMktInv(null)
      setExpNote(`该研究登记的主体 ${(d.entities ?? []).length} 个：先选一个主体，再取候选 run 与登记材料。`)
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  /** 第二步：候选 run（含每个视角是否可用＋不可用原因）、登记材料（旧机制/新披露）、已存实验。 */
  const expOptions = async (ent = expEnt, runId = expRunId) => {
    if (!ent) { setErr('先选主体'); return }
    setBusy('exp:options'); setErr(''); setExpNote('')
    try {
      const { ok, d } = await post('/api/research-study/experiment',
        expBody({ action: 'options', entity_id: ent, run_id: runId || undefined, view: expView }))
      if (!ok) throw new Error(d.error || '取候选失败')
      setExpRuns(d.runs ?? [])
      setExpOldMats(d.old_materials ?? []); setExpNewMats(d.new_materials ?? [])
      setExpSavedList(d.saved_experiments ?? [])
      setMktInv(d.market_inventory ?? null)
      setExpRunId(runId || '')
      const v = (d.views ?? {})[expView] ?? {}
      setExpCands(v.candidates ?? [])
      setExpMetric(v.selected_metric ?? '')
      setExpNote([
        v.applicable === false ? `本视角不可用：${v.reason || ''}` : '',
        `候选 run ${(d.runs ?? []).length} 条（研究冻结 ${(d.runs ?? []).filter((r: any) => r.source === 'study_frozen').length} / 续页 ${(d.runs ?? []).filter((r: any) => r.source === 'report_continuation').length}）；旧机制材料 ${(d.old_materials ?? []).length} 条、新披露 ${(d.new_materials ?? []).length} 条（都来自登记材料，浏览器不传路径）`,
        d.market_selection_note || '',
      ].filter(Boolean).join(' '))
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  /** 算这个格子：0/50/100% 三个点＋反转条件＋机制卡＋披露更新页（服务端算，前端只显示）。
   *  C5：可选「沿冻结原判」（显式原版实验 id，不现场重建旧判断）＋可选「市场对照」
   *  （披露事件/行情集/日历/评价时点都从枚举与已登记 id 里选）。 */
  const expEvaluate = async (mode: 'evaluate' | 'save' = 'evaluate') => {
    if (!expRunId) { setErr('先选一条候选 run（界面不替你取第一条）'); return }
    setBusy(mode === 'save' ? 'exp:save' : 'exp:evaluate'); setErr(''); setExpNote('')
    try {
      const { ok, d } = await post('/api/research-study/experiment', expBody({
        action: mode, run_id: expRunId, view: expView,
        metric: expMetric || undefined,
        old_material_id: expOldMat || undefined, new_material_id: expNewMat || undefined,
        baseline_experiment_id: expBaselineId || undefined,
        market: mktOn ? {
          material_id: expNewMat || undefined, price_dataset_id: mktDs || undefined,
          calendar_id: mktCal || undefined, evaluation_as_of: mktAsOf || undefined,
        } : undefined,
      }))
      if (!ok) throw new Error(`${d.error || '装配未完成'}${d.code ? `（${d.code}）` : ''}`)
      setExpArt(d)
      setExpSaved(mode === 'save' ? (d.saved ?? null) : null)
      if (mode === 'save') { setExpSavedList(d.saved?.saved_experiments ?? expSavedList); loadStudies() }
      const cell = (d.experiment?.cells ?? []).find((c: any) => c.view === expView) ?? {}
      setExpNote([d.generated_note, cell.applicable === false ? `本视角不可用：${cell.reason || ''}` : '']
        .filter(Boolean).join(' '))
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  /** 打开已保存实验：按 id 读回**同一份** JSON（不重算、不改研究登记）。 */
  const expOpen = async (eid: string) => {
    setBusy(`exp:open:${eid}`); setErr(''); setExpNote('')
    try {
      const { ok, d } = await post('/api/research-study/experiment',
        { study_id: selStudy?.study_id ?? '', result_hash: selStudy?.result_hash ?? '',
          action: 'open', experiment_id: eid })
      if (!ok) throw new Error(d.error || '打开失败')
      setExpArt(d); setExpSaved(null)
      setExpNote(`已按内容身份读回：${d.identity?.experiment_id ?? eid}（正文哈希一致 ${String(d.identity?.content_sha256_matches)}）。`)
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  const expCell = useMemo(() => (expArt?.experiment?.cells ?? []).find((c: any) => c.view === expView) ?? null,
    [expArt, expView])

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
          <div>① 内置样例公司/证券代码/财务/新闻/行情/日历/标签<b className="text-slate-200 font-semibold">全部为模拟</b>：只验证流程，不证明收益或风险模型有效性。</div>
          <div>② 输出是<b className="text-slate-200 font-semibold">内部研究草稿</b>，人工未复核；<b className="text-slate-200 font-semibold">不自动采纳</b>、不进 MD/PDF/ZIP 交付包、不对外分发。</div>
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
          <div className="text-xs text-slate-400">预览后需逐个勾选确认的<b className="text-slate-200 font-semibold">文件内主体</b>
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
                模拟数据：{run.synthetic ? '是（模拟）' : '否（真实）'}；研究状态 {researchStateLabel(run.research_state)} / 人工 {humanReviewLabel(run.human_review)}</div>
              <div>目录：<span className="font-mono">{run.dir}</span></div>
              <div>外部交付：{JSON.stringify(run.external_delivery ?? {})}</div>
              {run.restore_basis && (
                <div className={run.result_hash_matches === false ? 'text-amber-300' : ''}>
                  恢复事实（接缝②）：{run.restore_basis}；
                  结果哈希比对 {run.result_hash_matches === false ? '**不一致**（不得冒充原研究）'
                    : String(run.result_hash_matches)}；
                  来源 <span className="font-mono">{String(run.restored_from ?? '')}</span>
                </div>
              )}
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
          title="④ 从冻结研究生成公司报告（B4 直连）"
          extra="绑定三要素显式选定：study_id + entity_id + result_hash；界面没有「最新研究」，也不重新上传文件" />
        {!selStudy ? (
          <EmptyState title="未选定研究"
            description="先在 ⑦ 历史研究列表里点「选定」：三要素与双截止/研究状态全部来自该研究的冻结读数，不隐式取最新。" />
        ) : (
          <div className="space-y-3">
            <div className="text-xs text-slate-400 space-y-1">
              <div>研究ID：<span className="font-mono">{String(selStudy.study_id ?? '').slice(0, 16)}…</span>；
                结果哈希 <span className="font-mono">{String(selStudy.result_hash ?? '').slice(0, 16)}…</span></div>
              <div>{selStudy.name || '—'}</div>
              <div>特征截止 {selStudy.feature_cutoff || '—'} · 评价截止 {selStudy.evaluation_as_of || '—'}；
                研究状态 {researchStateLabel(selStudy.research_state)} / 人工 {humanReviewLabel(selStudy.human_review)}</div>
              <div>模型周期 {((selStudy.spec?.financial ?? {}).periods ?? []).join('、') || '—'}；
                主体 {(selStudy.subjects ?? []).join('、') || '—'}</div>
            </div>
            <div className="flex flex-wrap gap-2">
              {(selStudy.subjects ?? []).map((code: string) => (
                <button key={code} onClick={() => doReport(code)} disabled={busy !== ''}
                  className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 text-sm disabled:opacity-40">
                  <FileText className="w-4 h-4" />
                  {busy === `report:${code}` ? '生成中…' : `生成 ${code} 公司报告`}
                </button>
              ))}
              {/* 接缝②（C2b 两分叉）：**查看原版**＝只带 study_id（服务端按冻结 inputs 走
                  原 _compute_study；实现已改变时直接读冻结产物，不重算）；**改参**走 ⑨
                  （带 study_id+spec → 冻结输入＋改参创建**新候选**，新研究ID）。 */}
              <button onClick={doRestoreRun} disabled={busy !== ''}
                className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-slate-800/70 hover:bg-slate-700/70 text-slate-300 text-sm disabled:opacity-40">
                <RefreshCw className="w-4 h-4" />
                {busy === 'restore' ? '恢复中…' : '查看原版（按冻结研究恢复，不重传）'}
              </button>
            </div>
            {report && (
              <Alert tone={report.status === 'exists' ? 'warn' : 'info'}
                title={report.status === 'exists' ? '同版正文已交付过：返回现有包（未重算）' : '公司报告已生成（内部研究候选，未经人工复核）'}>
                <div>候选任务 <span className="font-mono">{String(report.task_id ?? '')}</span>；
                  版本 <span className="font-mono">{String(report.version_id ?? '').slice(0, 12)}…</span></div>
                {/* 三态分列（C2a 接缝①）：机器验收／研究状态／人工复核各按本版报告的实际绑定，
                    研究目录自身的状态单独一行，不冒充本版报告的研究状态。 */}
                {report.states ? (
                  <div className="mt-1 space-y-0.5">
                    <div>机器验收：<b className="text-slate-300">{report.states.machine?.status ?? '—'}</b>
                      <span className="text-slate-500">（{report.states.machine?.basis ?? ''}）</span></div>
                    <div>研究状态：<b className={report.states.research?.stale ? 'text-amber-300' : 'text-slate-300'}>
                      {report.states.research?.status ?? '—'}</b>
                      {report.states.research?.reason ? `——${report.states.research.reason}` : ''}
                      <span className="text-slate-500">（{report.states.research?.basis ?? ''}）</span></div>
                    <div>人工复核：<b className="text-amber-300">{report.states.human?.status ?? '—'}</b>
                      <span className="text-slate-500">（{report.states.human?.basis ?? ''}）</span></div>
                    <div className="text-slate-500">研究目录自身状态：
                      {report.study_catalog_research_state ? researchStateLabel(report.study_catalog_research_state) : '—'}（只描述该研究目录，不代替本版报告的研究状态）</div>
                  </div>
                ) : (
                  <div>研究状态 {researchStateLabel(report.research_state)} / 人工 {humanReviewLabel(report.human_review)}；
                    机器验收 {String((report.acceptance ?? {})?.status ?? report.acceptance ?? '—')}</div>
                )}
                <div>外部交付 {JSON.stringify(report.external_delivery ?? {})}</div>
                <div className="flex flex-wrap gap-3 mt-1">
                  {report.files?.report_md && <a className="text-cyan-300 hover:text-cyan-200 underline decoration-dotted" href={report.files.report_md} download>report.md</a>}
                  {report.files?.report_pdf && <a className="text-cyan-300 hover:text-cyan-200 underline decoration-dotted" href={report.files.report_pdf}>report.pdf</a>}
                  {report.files?.zip && <a className="text-cyan-300 hover:text-cyan-200 underline decoration-dotted" href={report.files.zip} download>{report.zip_name || '交付包 zip'}</a>}
                </div>
                <div className="text-[12px] text-slate-500">{report.note}</div>
              </Alert>
            )}
          </div>
        )}
      </Card>

      <Card>
        <SectionTitle icon={<FileText className="w-4 h-4 text-cyan-400" />}
          title="⑤ Q1 读数：3×3「利润目标＋天数冲击占用」网格 / 目标核查 / 补证提示"
          extra="数值句从 ModelRun 读数装配；本节只读回显，不在渲染期改参数或重算" />
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
                                    <div className="text-[12px]">
                                      {c.profit_state}
                                      {c.budget_headroom != null
                                        ? `；B−C ${yi4(c.budget_headroom)}；${c.joint_state}`
                                        : (c.joint_state ? `；${c.joint_state}` : '')}
                                    </div>
                                    {c.status === 'partial' && c.reason
                                      && <div className="text-[12px] text-amber-400">{String(c.reason).slice(0, 60)}</div>}
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
                      <div key={i} className="text-[12px] text-slate-500">- {n}</div>
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
                      <div className="text-[12px] text-slate-500">基期实质指纹
                        <span className="font-mono"> {f.base_fingerprint}</span>
                        ：只绑定主体/年度/特征截止/基期来源/参数方法/读数判定，排除后续与全研究ID。</div>
                    )}

                    {tf && (
                      <div className="border-t border-slate-800 pt-2">
                        <div className="text-slate-300">目标核查（{periodLabel(tf.period)} 年 {tf.metric_label}）：**{tf.status}**</div>
                        <div className="text-[12px] text-slate-500">{tf.nature}；口径 {tf.caliber}。</div>
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
                            <div className="text-[12px] text-slate-500">
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
                        <div className="text-[12px] text-slate-500">{tf.note}</div>
                        {tf.base_fingerprint && (
                          <div className="text-[12px] text-slate-500">对照基期指纹
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
            <div className="text-[12px] text-slate-500">
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
          title="⑥ 同一入口改参：改共享天数或这家的 g/m/B，产出新版研究"
          extra="新版=新研究ID；同一次 run 重算两家；旧目录与旧卡不回填" />
        <div className="text-xs text-slate-500 mb-2">
          克隆当前单一参数源（activeSpec：选定研究的冻结 spec，或预览解析后的 spec），覆盖
          <b> 两公司共同假设</b>的天数与本公司 g/m/B 后走同一条 run 通道。
          不是「改一格只重算那一格」：同一次 run 对两家同时生效；输入目录与特征截止不动。
          百分比填数：0, 10, 20 表示 0%/+10%/+20%；三一填 -10, -5, 0。预算 B 留空 = 未设（不自动给值）。
        </div>
        <div className="border border-amber-700/50 bg-amber-500/5 rounded-xl p-3 mb-3">
          <div className="text-sm text-amber-300 mb-2">两公司共同假设：同一套天数冲击（每次 run 两家同时生效）</div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
            <label className="space-y-1">回款天数冲击（日）
              <input value={sharedDaysOf().cdays} aria-label="共享 回款天数冲击"
                onChange={ev => setShared({ cdays: ev.target.value })}
                className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1" /></label>
            <label className="space-y-1">库存天数冲击（日）
              <input value={sharedDaysOf().idays} aria-label="共享 库存天数冲击"
                onChange={ev => setShared({ idays: ev.target.value })}
                className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1" /></label>
            <div className="text-slate-500 self-end col-span-2">
              填 0 = 该维度冲击关闭；范围 -30..30；只在这里改一处，两家共用。
            </div>
          </div>
        </div>
        {gridCodes.map(code => {
          const e = editOf(code)
          if (!e) return null
          return (
            <div key={code} className="border border-slate-800 rounded-xl p-3 mb-2">
              <div className="text-sm text-slate-200 mb-2">{code}（本公司的轴与预算；天数用上面的共享块）</div>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-2 text-xs">
                <label className="space-y-1">收入增速 g（%）
                  <input value={e.g} aria-label={`${code} g`}
                    onChange={ev => setEdit(code, { g: ev.target.value })}
                    className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1" /></label>
                <label className="space-y-1">毛利率变化 m（pp）
                  <input value={e.m} aria-label={`${code} m`}
                    onChange={ev => setEdit(code, { m: ev.target.value })}
                    className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1" /></label>
                <label className="space-y-1">预算 B（元，留空=未设）
                  <input value={e.budget} aria-label={`${code} 预算`}
                    onChange={ev => setEdit(code, { budget: ev.target.value })}
                    className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1" /></label>
              </div>
              <button onClick={() => doRerunGrid(code)} disabled={busy !== '' || !allConfirmed}
                title={allConfirmed ? '' : '先预览并逐个确认文件里的公司'}
                className="mt-2 inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 text-xs disabled:opacity-40">
                <Play className="w-3.5 h-3.5" /> {busy === 'rerun' ? '重算中…'
                  : `冻结输入＋改参创建新候选（${code}）`}
              </button>
              {!allConfirmed && <span className="ml-2 text-xs text-slate-500">需先「文件预览」并确认全部主体</span>}
              <span className="ml-2 text-xs text-slate-500">
                {inputId === '__sample__' && selStudy?.study_id
                  ? '（无上传目录：按 ⑦ 选定研究的冻结输入改参，样例不参与）'
                  : ''}
              </span>
            </div>
          )
        })}
        {activeSpec && !run?.study_id && inputId === '__sample__' && selStudy?.study_id && (
          <div className="text-xs text-slate-400 mt-1">
            无上传目录：⑨ 的改参走「冻结输入＋改参创建新候选」（按 ⑦ 选定研究的冻结输入，
            样例不参与）；只点「查看原版」则是恢复原版、不新建候选。
          </div>
        )}
        {!!(run?.reparam_spec_diff ?? []).length && (
          <div className="text-xs text-amber-300/90 mt-1">
            本版是改参新候选（原研究 <span className="font-mono">
              {String(run?.reparam_from?.study_id ?? '').slice(0, 16)}…</span>）：
            {(run?.reparam_spec_diff ?? []).map((s: string) => (
              <div key={s} className="font-mono pl-2">{s}</div>
            ))}
            <div className="text-slate-400">冻结输入与两个截止未改；旧版本只读保留。</div>
          </div>
        )}
        {run?.study_id && (
          <div className="text-xs text-slate-400 mt-1">
            新版研究ID <span className="font-mono">{String(run.study_id).slice(0, 16)}…</span>
            ：下载走 ⑦ 的产物清单（<span className="font-mono">q1.json</span> /
            <span className="font-mono"> q1_followup_*.md</span> /
            <span className="font-mono"> financial.json</span>），再点 verify / replay 复算核对。
          </div>
        )}
      </Card>

      <Card>
        <SectionTitle title="⑦ 已保存的内部研究产物" extra={`${studies.length} 个`} />

        {studies.length === 0 ? (
          <EmptyState title="还没有产物"
            description="运行一次研究后，内部产物会保存在 tmp/research-studies/<研究ID前16位>/；旧目录不会被覆盖。" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-slate-500"><tr>
                <th className="text-left">选定</th><th className="text-left">研究ID</th>
                <th className="text-left">主体</th><th className="text-left">特征截止</th>
                <th className="text-left">模拟</th><th className="text-left">状态</th>
                <th className="text-left">人工</th><th className="text-right">附件</th>
              </tr></thead>
              <tbody className="text-slate-300">
                {studies.map(s => (
                  <tr key={s.dir ?? s.study_id} className="border-t border-slate-800">
                    <td className="py-1">
                      <button
                        onClick={() => {
                          // 单选源切换：清掉上一个选择源的工作态（run/preview/报告/
                          // 未提交改参/共享天数都是上一个上下文的），恢复该研究
                          // **冻结的主体确认**与**冻结截止**（截止输入跟随选定研究，
                          // 请求体截止与服务端 spec 核对一致，不再依赖旧页面参数）。
                          setSelectedStudyId(selectedStudyId === s.study_id ? '' : s.study_id)
                          setRun(null); setPreview(null); setReport(null); setCheck(null); setCard(null)
                          setGridEdits({}); setSharedDays(null)
                          setConfirmed([...(s.subjects ?? [])])
                          if (s.feature_cutoff) setCutoff(String(s.feature_cutoff))
                          if (s.evaluation_as_of) setAsOf(String(s.evaluation_as_of))
                        }}
                        aria-label={`选定 ${s.study_id}`}
                        title={`${s.name || ''}（${String(s.study_id)}）`}
                        className={selectedStudyId === s.study_id
                          ? 'px-2 py-0.5 rounded bg-cyan-500/25 text-cyan-300 text-xs'
                          : 'px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-400 text-xs'}>
                        {selectedStudyId === s.study_id ? '已选定' : '选定'}
                      </button>
                    </td>
                    <td className="font-mono py-1">{String(s.study_id).slice(0, 16)}…</td>
                    <td>{(s.subjects ?? []).join('、') || '—'}</td>
                    <td>{s.feature_cutoff || '—'}</td>
                    <td>{s.synthetic ? '模拟' : '真实'}</td>
                    <td>{researchStateLabel(s.research_state)}</td>
                    <td>{humanReviewLabel(s.human_review)}</td>
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

      <Card>
        <SectionTitle icon={<FlaskConical className="w-4 h-4 text-cyan-400" />}
          title="⑧ 研究实验台：选研究 → 挑战判断 → 新披露更新 → 保存行动"
          extra="浏览器只传标识与枚举；数值/阈值/公式由服务端从冻结结果与登记材料解析（C4）" />
        {!selStudy ? (
          <EmptyState title="未选定研究"
            description="先在 ⑦ 历史研究列表里点「选定」：实验台按该研究的冻结读数与登记材料工作，不隐式取最新。" />
        ) : (
          <div className="space-y-3">
            <div className="text-xs text-slate-400 space-y-1">
              <div>研究 <span className="font-mono">{String(selStudy.study_id ?? '').slice(0, 16)}…</span> ·
                结果哈希 <span className="font-mono">{String(selStudy.result_hash ?? '').slice(0, 16)}…</span> ·
                特征截止 {selStudy.feature_cutoff || '—'}</div>
              {expNote && <div className="text-slate-500">{expNote}</div>}
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <button onClick={expLoadEntities} disabled={busy !== ''}
                className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 disabled:opacity-40">
                {busy === 'exp:entities' ? '读取中…'
                  : (expEntities.length ? `① 已取主体 ${expEntities.length} 个` : '① 取这家研究的主体')}
              </button>
              {expEntities.map(code => (
                <button key={code} disabled={busy !== ''}
                  onClick={() => { setExpEnt(code); setExpArt(null); setExpSaved(null); expOptions(code, '') }}
                  className={expEnt === code
                    ? 'px-3 py-1.5 rounded-lg bg-cyan-500/25 text-cyan-300 disabled:opacity-40'
                    : 'px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 disabled:opacity-40'}>
                  {code}
                </button>
              ))}
            </div>
            {expRuns.length > 0 && (
              <div className="space-y-1">
                <div className="text-xs text-slate-500">② 候选 run（研究冻结＋续页；**不替你取第一条**，每个视角是否可用与原因都显示）</div>
                {expRuns.map((r: any) => (
                  <button key={r.run_id} disabled={busy !== ''}
                    onClick={() => { setExpRunId(r.run_id); setExpArt(null); setExpSaved(null); expOptions(expEnt, r.run_id) }}
                    className={expRunId === r.run_id
                      ? 'w-full text-left px-3 py-1.5 rounded-lg bg-cyan-500/15 border border-cyan-700/50 text-cyan-200 disabled:opacity-40'
                      : 'w-full text-left px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-300 disabled:opacity-40'}>
                    <span className="font-mono">{String(r.run_id).slice(0, 12)}…</span>
                    {r.source === 'report_continuation' ? '（续页）' : '（研究冻结）'}
                    · 期间 {(r.periods ?? []).join('/')}
                    · 本期 {r.views?.current?.applicable
                      ? `可用（${r.views?.current?.label ?? r.views?.current?.metric ?? ''}）`
                      : `不可用（${r.views?.current?.reason || '该 run 没有可回撤的带符号项'}）`}
                    · 同比 {r.views?.yoy?.applicable ? '可用' : `不可用（${r.views?.yoy?.reason || ''}）`}
                  </button>
                ))}
              </div>
            )}
            {expRunId && (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="text-slate-500">③ 视角与候选项：</span>
                  {(['current', 'yoy'] as const).map(v => (
                    <button key={v} onClick={() => setExpView(v)}
                      className={expView === v
                        ? 'px-3 py-1.5 rounded-lg bg-cyan-500/25 text-cyan-300'
                        : 'px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300'}>
                      {v === 'current' ? '本期额（披露 OCF）' : '同比贡献（ΔOCF）'}
                    </button>
                  ))}
                  <select value={expMetric} aria-label="候选项"
                    onChange={ev => setExpMetric(ev.target.value)}
                    className="max-w-md bg-slate-900 border border-slate-700 rounded px-2 py-1">
                    <option value="">按既有默认（与 C 同向的最大带符号项）</option>
                    {expCands.map((c: any) => (
                      <option key={c.metric} value={c.metric}>
                        {c.label}（{c.item_yi} 亿{c.selected ? '，默认' : ''}）
                      </option>
                    ))}
                  </select>
                  <button onClick={() => expEvaluate('evaluate')} disabled={busy !== ''}
                    className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 disabled:opacity-40">
                    {busy === 'exp:evaluate' ? '计算中…' : '④ 算这个格子（0/50/100%）'}
                  </button>
                </div>
                <div className="text-[12px] text-slate-500">
                  候选项取自该 run 的带符号调节项（现金流量表补充资料，逐项）；单项不适用会写明原因（缺项／口径），不补零、不造数。
                </div>
              </div>
            )}
            {expArt && (
              <div className="space-y-3">
                {expCell && !expCell.applicable ? (
                  <Alert tone="warn" title="本视角不可用">{expCell.reason}</Alert>
                ) : expCell && (
                  <div className="space-y-1">
                    <div className="text-xs text-slate-400">
                      {expView === 'current' ? '本期额（披露 OCF）' : '同比贡献（ΔOCF）'} ·
                      所选：<b className="text-slate-200">{expCell.label}</b>（<span className="font-mono">{expCell.metric}</span>）·
                      基准 C {expCell.base_yi} 亿 · 该项 a {expCell.item_yi} 亿 · {expCell.selected_by}
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead className="text-slate-500"><tr>
                          <th className="text-left">回撤比例 λ</th>
                          <th className="text-right">回撤后 C′（亿元）</th>
                          <th className="text-right">（元）</th>
                        </tr></thead>
                        <tbody className="text-slate-300">
                          {(expCell.points ?? []).map((p: any) => (
                            <tr key={p.reversion_fraction} className="border-t border-slate-800">
                              <td className="py-1">{p.reversion_percent}%</td>
                              <td className="text-right tabular-nums">{p.value_yi}</td>
                              <td className="text-right tabular-nums text-slate-500">{p.value_yuan}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="text-xs text-slate-400">
                      反转条件（λ*）：{expCell.zero_lambda ?? '—'}
                      {expCell.zero_lambda_pct ? `（${expCell.zero_lambda_pct}%）` : ''} ·
                      0..1 内可达 <b>{String(expCell.reachable_within_0_1)}</b> · {expCell.threshold_note}
                    </div>
                    <div className="text-xs text-slate-300">{expCell.judgment}</div>
                    <div className="text-[12px] text-slate-500">{(expCell.boundary ?? []).join(' ')}</div>
                  </div>
                )}
                <div className="space-y-1">
                  <div className="text-xs text-slate-500">
                    机制卡（状态来自两期读数比较；判据哈希 {String(expArt.identity?.criteria_sha256 ?? '').slice(0, 12)}…，求值与文案同源）
                  </div>
                  {(expArt.cards ?? []).map((c: any) => (
                    <div key={c.judgment_id} className="border border-slate-800 rounded-xl p-2 text-xs">
                      <div>
                        {/* c.status 已是中文展示串（加强/削弱），非后端英文枚举；不走 text 位裸渲染形 */}
                        <b className={c.status === '加强' ? 'text-emerald-300' : c.status === '削弱' ? 'text-rose-300' : 'text-slate-400'}>{c["status"]}</b>
                        {' '}{c.hypothesis}
                      </div>
                      <div className="text-[12px] text-slate-400 mt-0.5">{c.basis}</div>
                      <div className="text-[12px] text-slate-500">
                        观察条件：{c.criterion?.observation_condition}｜单窗口削弱：{c.criterion?.weaken_single}｜确认门槛：{c.criterion?.weaken_confirmed}
                      </div>
                      <div className="text-[12px] text-slate-600">
                        依据：{c.basis_source?.doc_type} {c.basis_source?.disclosure_date}（{(c.basis_source?.period_labels ?? []).join('、')}）
                      </div>
                    </div>
                  ))}
                  {(expArt.excluded_cards ?? []).length > 0 && (
                    <Fold title={`未纳入卡 ${expArt.excluded_cards.length} 条`} hint="另有排除原因，不冒充比较结果">
                      {(expArt.excluded_cards ?? []).map((c: any) => (
                        <div key={c.judgment_id} className="text-[12px] text-slate-500">{c.judgment_id}：{c.reason}</div>
                      ))}
                    </Fold>
                  )}
                </div>
                <div className="border border-slate-800 rounded-xl p-3 space-y-2">
                  <div className="text-sm text-slate-200">新披露更新：同公司、晚于旧截止的登记材料</div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
                    <label className="space-y-1">旧机制快照（旧材料＋旧快照哈希）
                      <select value={expOldMat} aria-label="旧机制快照"
                        onChange={ev => { setExpOldMat(ev.target.value); setExpNewMat('') }}
                        className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1">
                        <option value="">不绑定（只算上面的格子）</option>
                        {expOldMats.map((m: any) => (
                          <option key={m.material_id} value={m.material_id}>
                            {m.disclosed_at} {m.title}（{String(m.material_id).slice(0, 12)}…{m.in_study_registry ? '，研究已登记' : ''}）
                          </option>
                        ))}
                      </select></label>
                    <label className="space-y-1">较新披露（同公司）
                      <select value={expNewMat} aria-label="较新披露"
                        onChange={ev => setExpNewMat(ev.target.value)}
                        className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1">
                        <option value="">暂不更新</option>
                        {expNewMats.map((m: any) => (
                          <option key={m.material_id} value={m.material_id}>
                            {m.disclosed_at} {m.title}（{String(m.material_id).slice(0, 12)}…）
                          </option>
                        ))}
                      </select></label>
                  </div>
                  <div className="text-[12px] text-slate-500">
                    旧截止 {expArt.based_on?.old_cutoff || '—'}：早于它的材料才算旧机制依据；新披露必须**晚于旧截止**才作更新（服务端校验，不靠浏览器传日期）。
                  </div>
                  <label className="space-y-1 text-xs">C5 更新口径（沿冻结原判，不现场重建旧判断）
                    <select value={expBaselineId} aria-label="原版实验"
                      onChange={ev => setExpBaselineId(ev.target.value)}
                      className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1">
                      <option value="">历史重建（无原版：当前代码重放旧材料，不是用户当时存下的原假说）</option>
                      {expSavedList.map((s: any) => (
                        <option key={s.experiment_id} value={s.experiment_id}>
                          沿冻结原判 {String(s.experiment_id).slice(0, 12)}…（{s.saved_at ? String(s.saved_at).slice(0, 10) : '冻结'}）
                        </option>
                      ))}
                    </select></label>
                  <Fold title="市场对照（可选：1/5/20 交易日三窗口）"
                    hint="披露事件/行情集/日历/评价时点都显式选；缺件在装配层如实 pending">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
                      <label className="space-y-1 flex items-center gap-2">
                        <input type="checkbox" checked={mktOn} aria-label="启用市场对照"
                          onChange={ev => setMktOn(ev.target.checked)} />
                        启用市场对照（几何相对基准，与经营判断并列）
                      </label>
                      <label className="space-y-1">行情集
                        <select value={mktDs} aria-label="行情集" disabled={!mktOn}
                          onChange={ev => setMktDs(ev.target.value)}
                          className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1">
                          <option value="">未选（装配层如实 pending）</option>
                          {(mktInv?.price_datasets ?? []).map((d: any) => (
                            <option key={d.dataset_id} value={d.dataset_id}>
                              {d.source}·{d.adj_basis}（{(d.instruments ?? []).join('/')}，{d.date_range?.[0]}~{d.date_range?.[1]}，{d.license}）
                            </option>
                          ))}
                        </select></label>
                      <label className="space-y-1">交易日历
                        <select value={mktCal} aria-label="交易日历" disabled={!mktOn}
                          onChange={ev => setMktCal(ev.target.value)}
                          className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1">
                          <option value="">未选（装配层如实 pending）</option>
                          {(mktInv?.calendars ?? []).map((c: any) => (
                            <option key={c.calendar_id} value={c.calendar_id}>
                              {c.source}·{c.market}（{c.range?.[0]}~{c.range?.[1]}）
                            </option>
                          ))}
                        </select></label>
                      <label className="space-y-1">评价时点
                        <input type="date" value={mktAsOf} aria-label="评价时点" disabled={!mktOn}
                          onChange={ev => setMktAsOf(ev.target.value)}
                          className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1" />
                      </label>
                    </div>
                    <div className="text-[12px] text-slate-500">
                      市场事件＝上面选的较新披露（同一事件，不另挑）；窗口 1/5/20 交易日＝服务端偏移 0/4/19，生产从数据源算，不写死。
                    </div>
                  </Fold>
                  <div className="flex flex-wrap gap-2">
                    <button onClick={() => expEvaluate('evaluate')} disabled={busy !== '' || !expOldMat}
                      title={expOldMat ? '' : '先选旧机制快照'}
                      className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-300 text-xs disabled:opacity-40">
                      {expBaselineId ? `沿冻结原判更新（原版 ${String(expBaselineId).slice(0, 8)}…）` : '更新披露读数（旧机制 → 新材料）'}
                    </button>
                    <button onClick={() => expEvaluate('save')} disabled={busy !== ''}
                      className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-300 text-xs disabled:opacity-40">
                      {busy === 'exp:save' ? '保存中…' : '保存行动页（同版 JSON＋一页摘要）'}
                    </button>
                  </div>
                  {expArt.disclosure_update?.page && (
                    <Fold title={`披露更新页（${expOldMat ? '旧材料 → 新材料' : '未绑定旧材料'}）`}
                      hint="真实材料回放，不是事前预测" defaultOpen>
                      <pre className="whitespace-pre-wrap text-[12px] text-slate-400">{expArt.disclosure_update.render_compact}</pre>
                      {expArt.based_on?.report_version && (
                        <div className="text-[12px] text-slate-500">
                          父报告仅**技术来源**：版本 {String(expArt.based_on.report_version.version_id ?? '').slice(0, 12)}…（验收 {String(expArt.based_on.report_version.acceptance_overall ?? '—')}）；不作为旧判断依据。
                        </div>
                      )}
                    </Fold>
                  )}
                  {expArt.market_comparison && (
                    <Fold title={`市场对照（${expArt.market_comparison.status === 'ok' ? '三窗口 1/5/20 交易日' : expArt.market_comparison.status}）`}
                      hint="几何相对基准，与经营判断并列；不混成一个数" defaultOpen>
                      {expArt.market_comparison.status !== 'ok' ? (
                        <div className="text-[12px] text-slate-500">
                          {expArt.market_comparison.status}：{expArt.market_comparison.reason || '—'}（不隐式取数、不拿别的数据集顶替）
                        </div>
                      ) : (
                        <div className="space-y-2 text-xs">
                          <div className="text-slate-400">
                            事件 {expArt.market_comparison.event?.date || '—'}
                            ｜锚点收盘 {expArt.market_comparison.anchor?.close ?? '—'}
                            （{String(expArt.market_comparison.anchor?.date || '')}）
                            ｜{(expArt.market_comparison.material?.title || '').slice(0, 48)}
                          </div>
                          <table className="w-full text-[12px]">
                            <thead><tr className="text-slate-500 text-left">
                              <th className="py-1">窗口（交易日）</th><th>日期</th><th>股票</th>
                              <th>基准</th><th>相对（几何）</th><th>状态</th></tr></thead>
                            <tbody>
                              {(expArt.market_comparison.windows ?? []).map((w: any) => (
                                <tr key={w.offset} className="text-slate-300 border-t border-slate-800">
                                  <td className="py-1">{w.window}</td><td>{w.date}</td>
                                  <td>{w.stock_return_pct}</td><td>{w.benchmark_return_pct}</td>
                                  <td>{w.relative_return_pct}</td><td>{w.state}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                          <div className="text-slate-400">
                            并列经营判断：
                            {(expArt.market_comparison.alongside_operations ?? []).map((a: any, i: number) => (
                              <span key={a.judgment_id ?? i} className="mr-2">
                                {a.judgment_id} <span className={a.status === '加强' ? 'text-emerald-400' : a.status === '削弱' ? 'text-rose-400' : 'text-slate-500'}>{a["status"]}</span>
                              </span>
                            ))}
                            {!(expArt.market_comparison.alongside_operations ?? []).length && '—'}
                            ｜下一观察：{String(expArt.market_comparison.next_observation || '—').slice(0, 80)}
                          </div>
                          <div className="text-[12px] text-slate-500">
                            读数哈希 {String(expArt.market_comparison.reading_hash || '').slice(0, 16)}…
                            ｜{expArt.market_comparison.delivery_eligible ? '数据源许可：可对外' : '数据源许可：仅内部试验（不对外交付，包外留痕）'}
                          </div>
                        </div>
                      )}
                    </Fold>
                  )}
                  {expSaved && (
                    <Alert tone="info" title={`已保存：experiment_id ${expSaved.experiment_id}`}>
                      <div className="flex flex-wrap gap-3 mt-1">
                        {Object.entries(expSaved.urls ?? {}).map(([k, u]) => (
                          <a key={k} className="text-cyan-300 hover:text-cyan-200 underline decoration-dotted"
                            href={String(u)} download>{k}</a>
                        ))}
                      </div>
                    </Alert>
                  )}
                </div>
                <div className="text-xs text-slate-400 space-y-1">
                  {(expArt.next_actions ?? []).map((a: any, i: number) => (
                    <div key={i}>· {a.action}——{a.why}</div>
                  ))}
                </div>
                <div className="text-[12px] text-slate-500">
                  三态：研究状态 {expArt.research_state?.state ?? '—'}｜人工复核
                  {' '}{expArt.research_state?.human_review_label ?? expArt.research_state?.human_review ?? '—'}｜
                  对外交付 {String(expArt.research_state?.external_delivery_allowed)}｜
                  实验自身结论状态 {expArt.research_state?.experiment_state}
                  （登记状态与人工复核**原样透传**，代理不代签）。
                </div>
              </div>
            )}
            {expSavedList.length > 0 && (
              <div className="space-y-1">
                <div className="text-xs text-slate-500">已保存实验（按内容身份读回：同一组输入重放得到同一个 id）</div>
                {expSavedList.map((s: any) => (
                  <div key={s.experiment_id} className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="font-mono text-slate-400">{s.experiment_id}</span>
                    <span className="text-slate-500">
                      {s.company} · {String(s.run_id).slice(0, 12)}… ·
                      {s.selection?.view}/{s.selection?.metric || '默认'}
                    </span>
                    <button onClick={() => expOpen(s.experiment_id)} disabled={busy !== ''}
                      className="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 disabled:opacity-40">
                      {busy === `exp:open:${s.experiment_id}` ? '读取中…' : '重开'}
                    </button>
                    <a className="text-cyan-300 hover:text-cyan-200 underline decoration-dotted"
                      href={`/api/research-study/file?id=${encodeURIComponent(String(expArt?.study_id ?? selStudy.study_id))}&name=${encodeURIComponent(`experiments/${s.experiment_id}.json`)}`}
                      download>json</a>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </Card>
    </div>
  )
}
