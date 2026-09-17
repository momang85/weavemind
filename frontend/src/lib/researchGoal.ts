// S2：公司研究入口与底稿展示的纯逻辑。
//
// 抽成纯函数是为了可测（Node 行为测试直接断言，不依赖 DOM）——与 apiResult.ts 同一路子。
// 两条纪律：
//   1) 说不清的公司/期间**不拼成目标**：返回 gaps，界面据此提示"请补全"，不靠猜；
//   2) 底稿摘要只报**底稿里真实存在**的东西：目标达成与否按底稿自身判定，
//      缺口/待核验项原样带出，不美化。

export interface ResearchForm {
  company?: string
  yearFrom?: string | number
  yearTo?: string | number
  caliber?: string
  asOf?: string
  materials?: string
}

export interface ResearchGoal {
  goal: string
  gaps: string[]
}

const CALIBERS = ['合并', '母公司']

function _year(v: string | number | undefined): string {
  const s = String(v ?? '').trim()
  return /^(19|20)\d{2}$/.test(s) ? s : ''
}

/** 把"公司与期间"表单拼成任务目标；缺什么就返回什么缺口（不猜）。 */
export function buildResearchGoal(form: ResearchForm): ResearchGoal {
  const gaps: string[] = []
  const company = String(form.company ?? '').trim()
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

  if (gaps.length) return { goal: '', gaps }

  const years = [y1, y2].sort()
  let goal =
    `研究${company} ${years[0]} 与 ${years[1]} 两个年度的营业收入、归母净利润、` +
    `经营活动现金流净额，${caliber}报表口径，数据截至 ${asOf}。` +
    `每个数字须能回溯到来源位置并可重算；缺证据的如实标缺口。`
  const materials = String(form.materials ?? '').trim()
  if (materials) goal += `\n\n参考资料：${materials}`
  return { goal, gaps: [] }
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
