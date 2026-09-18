// S2：公司研究入口与底稿展示的纯逻辑。
//
// 抽成纯函数是为了可测（Node 行为测试直接断言，不依赖 DOM）——与 apiResult.ts 同一路子。
// 两条纪律：
//   1) 说不清的公司/期间**不拼成目标**：返回 gaps，界面据此提示"请补全"，不靠猜；
//   2) 底稿摘要只报**底稿里真实存在**的东西：目标达成与否按底稿自身判定，
//      缺口/待核验项原样带出，不美化。

export interface ResearchForm {
  company?: string
  companyId?: string
  market?: string
  yearFrom?: string | number
  yearTo?: string | number
  caliber?: string
  asOf?: string
  materials?: string
}

/** 发给后端的**结构化契约字段**（与 `web_ui._sanitize_research_request` 的白名单一致）。 */
export interface ResearchFields {
  company: string
  company_id?: string
  market?: string
  caliber: string
  as_of: string
  year_from: number
  year_to: number
  periods: number[]
  materials?: string
}

export interface ResearchGoal {
  goal: string
  fields: ResearchFields | null
  gaps: string[]
}

const CALIBERS = ['合并', '母公司']
// 代码形状（与后端 facts.market_of_code 的识别一致；市场由后端定）
const CODE_SHAPE = /^\d{4,6}(\.[A-Za-z]{2,6})?$|^[A-Za-z]{1,5}(\.[A-Za-z]{1,2})?$/
const MARKETS = ['cn', 'hk', 'us']

function _year(v: string | number | undefined): string {
  const s = String(v ?? '').trim()
  return /^(19|20)\d{2}$/.test(s) ? s : ''
}

/**
 * 把"公司与期间"表单拼成任务目标**并同时给出结构化契约字段**；缺什么就报什么缺口（不猜）。
 *
 * 为什么要两份：目标是给编排器/模型的自然语言，契约是给底稿判定的**身份与口径依据**
 * （提交时落库，见 `task_state.mark_queued(research_request=...)`）。以前只发目标，
 * 底稿只好从抓取结果里反推公司，于是抓错公司会把研究主体一起改错。
 *
 * `market` 未选时**不填默认值**（不猜 A 股）：契约里就没有这一项，由契约层记缺口。
 */
export function buildResearchGoal(form: ResearchForm): ResearchGoal {
  const gaps: string[] = []
  // "公司名或代码"：公司栏与稳定标识栏任一填了即可——只填 `600519.SH` 也是有效身份
  // （后端契约层会把带后缀的代码规范化成 company_id + market，两边同一套规则）
  const companyRaw = String(form.company ?? '').trim()
  const companyIdRaw = String(form.companyId ?? '').trim()
  const company = companyRaw || companyIdRaw
  if (!company) gaps.push('请填写公司名或股票代码')
  const y1 = _year(form.yearFrom)
  const y2 = _year(form.yearTo)
  if (!y1 || !y2) {
    gaps.push('请填写两个年度（起始 / 结束）')
  } else if (y1 === y2) {
    gaps.push('两个年度需不同（首发路径按两期做同比）')
  }
  const caliber = CALIBERS.includes(String(form.caliber ?? '')) ? String(form.caliber) : ''
  if (!caliber) gaps.push('请选择报表口径（合并 / 母公司）')
  const asOf = String(form.asOf ?? '').trim()
  if (!asOf) gaps.push('请填写资料截至日（报告须明示数据时效）')

  if (gaps.length) return { goal: '', fields: null, gaps }

  const years = [y1, y2].sort() as [string, string]
  let goal =
    `研究${company} ${years[0]} 与 ${years[1]} 两个年度的营业收入、归母净利润、` +
    `经营活动现金流净额，${caliber}报表口径，数据截至 ${asOf}。` +
    `每个数字须能回溯到来源位置并可重算；缺证据的如实标缺口。`
  const materials = String(form.materials ?? '').trim()
  if (materials) goal += `\n\n参考资料：${materials}`

  // 公司栏里写的是**代码形状**（如 600519.SH / AAPL）时，也作为稳定标识送出；
  // 市场由后端按后缀规范化（前端不猜）。名字形状（"贵州茅台"）不当代码。
  const companyId = companyIdRaw || (CODE_SHAPE.test(companyRaw) ? companyRaw : '')
  const market = String(form.market ?? '').trim().toLowerCase()
  const fields: ResearchFields = {
    company,
    caliber,
    as_of: asOf,
    year_from: Number(years[0]),
    year_to: Number(years[1]),
    periods: [Number(years[0]), Number(years[1])],
  }
  if (companyId) fields.company_id = companyId
  if (MARKETS.includes(market)) fields.market = market
  if (materials) fields.materials = materials
  return { goal, fields, gaps: [] }
}

/** 底稿里一条明细/派生行的展示字段（缺什么显示"未知"，不填 0）。 */
export interface FactRow {
  factId: string
  label: string
  entity: string
  period: string
  value: string
  unit: string
  currency: string
  verifyState: string
  sourceUrl: string
  formula: string
  inputs: string[]
  derived: boolean
}

export interface WorkingPaperSummary {
  hasPaper: boolean
  goalMet: boolean
  present: number
  required: number
  missing: string[]
  gaps: string[]
  problems: string[]
  facts: FactRow[]
  derived: FactRow[]
  asOf: string
}

function _row(r: Record<string, unknown>, derived: boolean): FactRow {
  const inputs = Array.isArray(r.derived_from) ? (r.derived_from as string[]) : []
  const value = r.value === null || r.value === undefined ? '未知' : String(r.value)
  return {
    factId: String(r.fact_id ?? ''),
    label: String(r.metric_label ?? r.label ?? r.metric ?? ''),
    entity: String(r.entity ?? '') || '未知',
    period: String(r.period ?? '') || '未知',
    value,
    unit: String(r.unit ?? '') || '未知',
    currency: String(r.currency ?? '') || '未知',
    verifyState: String(r.verify_state ?? '') || '未知',
    sourceUrl: String(r.source_url ?? ''),
    formula: String(r.formula ?? ''),
    inputs,
    derived,
  }
}

/** 把 `/api/task/<id>/working_paper` 的返回整理成结果页要的摘要。 */
export function summarizeWorkingPaper(paper: unknown): WorkingPaperSummary {
  const empty: WorkingPaperSummary = {
    hasPaper: false, goalMet: false, present: 0, required: 0, missing: [],
    gaps: [], problems: [], facts: [], derived: [], asOf: '',
  }
  if (!paper || typeof paper !== 'object') return empty
  const p = paper as Record<string, unknown>
  if (!Array.isArray(p.rows)) return empty
  const completeness = (p.completeness ?? {}) as Record<string, unknown>
  const request = (p.request ?? {}) as Record<string, unknown>
  return {
    hasPaper: true,
    // 目标达成按**底稿自身**的判定（缺证据/有冲突就不算达成）
    goalMet: p.ok === true,
    present: Number(completeness.present ?? 0),
    required: Number(completeness.required ?? 0),
    missing: (Array.isArray(completeness.missing) ? completeness.missing : []).map(String),
    gaps: (Array.isArray(p.gaps) ? p.gaps : []).map((g) =>
      String((g as Record<string, unknown>)?.detail ?? g)),
    problems: (Array.isArray(p.problems) ? p.problems : []).map((g) =>
      String((g as Record<string, unknown>)?.detail ?? g)),
    facts: (p.rows as Record<string, unknown>[]).map((r) => _row(r, false)),
    derived: (Array.isArray(p.derived) ? p.derived : []).map((r) =>
      _row(r as Record<string, unknown>, true)),
    asOf: String(request.as_of ?? ''),
  }
}
