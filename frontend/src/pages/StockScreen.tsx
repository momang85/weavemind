// 可解释选股 MVP（R1-SCREEN-1023，R1-C 工作台入口）
//
// 一件事：**选股票池 + 资料截止日 → 确认少量财务规则 → 看三态候选（带理由/缺口）→
// 看对应公司证据 → 结果已冻结（run_id），可重开同一份原版**。
// 边界：不做全市场回测、不给综合评分/上涨概率、不调用模型与搜索；
// 点公司只打开**这一份结果**里的证据（绑 run_id + entity_id），不提交研究、不跳"最近任务"。
// 视觉：纸白底 + 金色主色（bg-surface / text-brand），沿用全局 token，不新增散写样式。

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Filter, RefreshCw, Play, Eye, ShieldCheck, AlertTriangle, CheckCircle2,
  ChevronDown, ChevronRight, ExternalLink, FlaskConical, Loader2, Plus, X, Star, Coins,
  GitCompareArrows,
} from 'lucide-react'
import { Card, SectionTitle, Alert, EmptyState, StatCard } from '../components/ui'

// ── 后端契约（与 screen_store / stock_screen 同形） ─────────────────────

interface FieldSpec {
  field_id: string; label: string; kind: 'amount' | 'pct'
  unit: string; metric: string; numerator: string; denominator: string
  needs_prev: boolean; note: string
}
interface PeriodMode {
  mode: 'uniform' | 'pinned' | 'latest_each'; label: string; comparable: boolean
}
interface FieldsPayload {
  schema: string; impl_version: string; field_map_version: string
  ops: string[]; amount_units: string[]; fields: FieldSpec[]
  period_modes?: PeriodMode[]
}
interface PoolEntity {
  code: string; entity: string; rows: number; periods: string[]
  first_available: string; last_available: string; pending_rows: number
}
interface Provenance {
  kind: string; kind_label: string; file_sha256: string; origin_ref: string
  source_urls: string[]; original_verified: boolean
  human_review: { status: string; by: string | null; at: string | null }
  note: string
}
interface SourceMeta {
  label: string; sha256: string; row_count: number
  synthetic: boolean; user_provided: boolean; origin?: string
  official?: boolean; provenance?: Provenance
}
// R1.2：数据入口（官方缓存 + 我上传的数据集；三类来源显式区分）
interface DatasetSummary {
  dataset_id: string; name: string; created_at?: string; row_count?: number
  codes?: string[]; periods?: string[]; filename?: string
  usable?: boolean; integrity?: string
  // 官方身份只由服务端物化写进清单：上传数据集恒 official=false
  official?: boolean; user_provided?: boolean; synthetic?: boolean
  provenance_kind?: string; source_urls?: string[]
}
interface OfficialSource {
  kind: string; label: string; official: boolean; synthetic: boolean
  row_count: number; sha256?: string; usable: boolean; errors?: string[]
}
interface Preset {
  preset_id: string; name: string; note?: string; pool: string[]
  dataset_id?: string
  as_of: string; rules: { field: string; op: string; value: number; unit?: string }[]
  rules_hash: string; created_at: string; updated_at: string
}
interface ExcludedRow {
  code: string; entity?: string; period: string
  available_at?: string; reason: string
}
interface PendingRow { code: string; period?: string; reason: string }
interface PreviewEntity {
  entity: string; entity_id: string; periods: string[]; caliber: string
  currency: string[]; units: string[]; available_at: string[]
  observations: number; metrics: string[]; dataset_hash: string
  gaps: string[]; conflicts: string[]; excluded_rows: number; pending_rows: number
}
interface PreviewPayload {
  status: string; snapshot_id?: string; as_of?: string; pool?: string[]
  source: SourceMeta; available_pool: PoolEntity[]; missing_pool_codes?: string[]
  entities_ready?: number; entities_without_periods?: number
  excluded?: ExcludedRow[]; pending?: PendingRow[]
  rules?: any[]; rules_errors?: string[]; errors?: string[]
  entities?: PreviewEntity[]
  period_policy?: PeriodPolicy; suggested_report_period?: string
  common_periods?: string[]; per_entity_periods?: Record<string, string[]>
}
interface PeriodPolicy { mode: 'uniform' | 'pinned' | 'latest_each'; report_period: string }
interface Reading {
  field: string; label: string; status: string; value: number | null
  unit: string; display: string; formula: string; reason: string
  inputs: string[]; source_url: string
}
interface EvidenceInput {
  fact_id: string; metric: string; metric_label: string; period: string
  value: number | null; unit: string; currency: string; caliber: string
  period_kind: string; period_start: string; period_end: string
  as_of: string; state: string; source_url: string; source_hash: string
  verify_state: string
}
interface Evidence {
  schema: string; run_id: string; result_hash: string; snapshot_id: string
  as_of: string; created_at: string; source: SourceMeta
  entity: string; entity_id: string; verdict: string; eval_period: string
  readings: { field: string; label: string; status: string; display: string
    value: number | null; unit: string; formula: string; reason: string
    inputs: EvidenceInput[]; unresolved_inputs: string[] }[]
  source_urls: string[]; excluded?: ExcludedRow[]; pending_pit?: PendingRow[]
  integrity: string
}
interface RunEntity {
  entity: string; entity_id: string; dataset_hash: string; periods: string[]
  caliber: string; gaps: string[]; conflicts: string[]
  excluded: ExcludedRow[]; pending_pit: PendingRow[]; observations: any[]
}
interface ScreenRow {
  entity: string; entity_id: string; verdict: 'pass' | 'fail' | 'insufficient'
  eval_period: string; readings: Reading[]; reasons: string[]; dataset_hash: string
}
interface Run {
  schema: string; impl_version: string; field_map_version: string
  run_id: string; created_at: string; as_of: string; pool: string[]
  rules: any[]; rules_hash: string; snapshot_id: string; result_hash: string
  period_policy: PeriodPolicy; report_period: string; comparable: boolean
  counts: { pass: number; fail: number; insufficient: number; total: number }
  notes: string[]; source: SourceMeta; entities: RunEntity[]
  excluded?: ExcludedRow[]; rows: { pass: ScreenRow[]; fail: ScreenRow[]; insufficient: ScreenRow[] }
  reused?: boolean
  view?: { sort: { field: string; direction: string } | null; applied: boolean; note: string }
}
interface RunSummary {
  run_id: string; created_at: string; as_of: string; counts: Run['counts']
  source: SourceMeta; integrity?: string; usable?: boolean; reused?: boolean
  rules_count?: number; pool?: string[]
  report_period?: string; period_policy?: PeriodPolicy; comparable?: boolean
  result_hash?: string
}
type RuleDraft = { field: string; op: string; value: string; unit: string }

// ── W2 自选变化简报（watch_brief/1，两份冻结结果的只读投影） ──────────
type BriefSide = {
  run_id?: string; result_hash?: string; created_at?: string; as_of?: string
  report_period?: string; impl_version?: string; field_map_version?: string
  source?: SourceMeta
}
type BriefCard = {
  entity_id: string; entity?: string
  base_status: string; current_status: string; status_transition: string[]
  no_observed_change?: boolean
  reading_changes: { field: string; label?: string
    base: { status?: string | null; value?: number | null; display?: string; reason?: string }
    current: { status?: string | null; value?: number | null; display?: string; reason?: string } }[]
  value_changes: { metric: string; metric_label?: string; period?: string
    change: string; note?: string; delta?: number | null
    delta_pct_points?: number | null
    base?: { value?: number | null; unit?: string; currency?: string; caliber?: string }
    current?: { value?: number | null; unit?: string; currency?: string; caliber?: string }
    source_urls?: string[] }[]
  change_kinds: string[]; warnings: string[]
  evidence?: { base: { run_id?: string; entity_id?: string }
               current: { run_id?: string; entity_id?: string } }
}
type BriefOverview = {
  conditions_changed?: number; data_changed?: number; scope_changed?: number
  insufficient?: number; missing?: number; unchanged?: number
  rules_changed?: boolean; impl_version_changed?: boolean
  source_changed?: boolean; period_policy_changed?: boolean
  base_only_companies?: string[]; current_only_companies?: string[]
}
type Brief = {
  schema?: string; title?: string; brief_id?: string; status?: string
  identity?: { base?: BriefSide; current?: BriefSide
    watchlist_rev?: number | string
    comparability_flags?: Record<string, boolean>; scope_entity_ids?: string[] }
  comparability?: { flags?: Record<string, boolean>; not_comparable_reasons?: string[] }
  overview?: BriefOverview
  cards?: BriefCard[]
  change_kinds_legend?: Record<string, string>
  notes?: string[]
  cost?: { model_calls?: number; network_requests?: string }
  watchlist?: { rev?: number; count?: number; baseline_run?: string }
  scope?: Record<string, unknown>
  errors?: string[]
}

// ── R1.3 两份冻结结果的对比（只读、字段白名单、条件变了标「不可直接归因」） ──
interface CompareSide {
  run_id: string; created_at: string; as_of: string; pool: string[]
  rules: { field: string; op: string; value: number; unit?: string }[]
  period_policy: PeriodPolicy; report_period?: string; counts: Run['counts']
  source: SourceMeta
}
interface CompareCondition {
  key: string; label: string; same: boolean; base: string; other: string
}
interface CompareFieldChange {
  field: string; label: string
  base: { status: string | null; display: string }
  other: { status: string | null; display: string }
  kind: 'value' | 'state'
}
interface CompareStayed {
  code: string; entity?: string
  base_verdict: string; other_verdict: string
  base_verdict_label: string; other_verdict_label: string
  verdict_changed: boolean
  period: { base?: string; other?: string; same: boolean }
  comparable: boolean; field_changes: CompareFieldChange[]; note: string
}
interface CompareResult {
  schema: string; base: CompareSide; other: CompareSide
  conditions: CompareCondition[]; changed_conditions: CompareCondition[]
  attribution: { code: string; attributable: boolean; label: string; reasons: string[]; note: string }
  summary: { pool_added: number; pool_removed: number
    entered: number; exited: number; stayed: number; stayed_changed: number
    stayed_unchanged: number; verdict_flipped: number
    became_pass: number; left_pass: number
    insufficient_new: number; insufficient_resolved: number
    pass_from_insufficient: number; fail_from_insufficient: number
    not_comparable: number }
  entered: { code: string; entity?: string; verdict_label: string; eval_period?: string
    explain: string; reason_kind?: string }[]
  exited: { code: string; entity?: string; verdict_label: string; eval_period?: string
    explain: string; reason_kind?: string }[]
  stayed: CompareStayed[]
  field_whitelist: { field: string; label: string; unit: string }[]
  reading: string; note: string
}

// ── 状态徽章（纸白/金色基调，三态候选用绿/琥珀/灰；不直接显示英文枚举） ──

const VERDICT_LABEL: Record<ScreenRow['verdict'], string> = {
  pass: '通过', fail: '未通过', insufficient: '资料不足',
}
const VERDICT_CLASS: Record<ScreenRow['verdict'], string> = {
  pass: 'bg-emerald-500/15 text-emerald-700 border border-emerald-500/25',
  fail: 'bg-amber-500/15 text-amber-700 border border-amber-500/25',
  insufficient: 'bg-line-strong/20 text-ink-muted border border-line-strong/40',
}
const verdictBadge = (v: ScreenRow['verdict']) => (
  <span className={`px-2 py-0.5 rounded-full text-xs font-semibold whitespace-nowrap ${VERDICT_CLASS[v]}`}>
    {VERDICT_LABEL[v]}
  </span>
)
const READING_STATUS: Record<string, { label: string; cls: string }> = {
  ok: { label: '有值', cls: 'text-state-success' },
  missing: { label: '缺失', cls: 'text-ink-faint' },
  conflicting: { label: '冲突', cls: 'text-state-failure' },
  not_applicable: { label: '不适用', cls: 'text-ink-faint' },
  pending_pit: { label: '待核验', cls: 'text-state-issue' },
}
const readingStatus = (s: string) => READING_STATUS[s] || { label: s, cls: 'text-ink-faint' }

// ── W2：变化简报里的状态/数值文案（不显示英文枚举；缺记录不等于无变化） ──
const BRIEF_STATUS_LABEL: Record<string, string> = {
  pass: '通过', fail: '未通过', insufficient: '资料不足', missing: '缺记录',
}
const briefStatus = (s?: string | null) => BRIEF_STATUS_LABEL[s || ''] || '—'
const fmtNum = (v: number | null | undefined, digits = 2) =>
  v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: digits })
const fmtDelta = (v: number | null | undefined, digits = 2) => {
  if (v == null) return '—'
  const s = fmtNum(Math.abs(v), digits)
  return v > 0 ? `+${s}` : v < 0 ? `-${s}` : '0'
}

const short12 = (s?: string) => (s ? String(s).slice(0, 12) : '—')
const fmtTime = (iso?: string) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false })
}
const todayISO = () => {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}
const OP_LABEL: Record<string, string> = { '>=': '≥', '<=': '≤', '>': '>', '<': '<' }

// ── 页面 ─────────────────────────────────────────────────────────────

// CSV 字段说明（B1）：与 screen_store.CSV_COLUMNS 一一对应，中文解释 + 常见坑。
// 普通用户不需要读文档：点开「字段说明」就能知道一列填什么、缺了会怎样。
const CSV_FIELDS: Array<{ name: string; required: boolean; meaning: string; note?: string }> = [
  { name: 'entity', required: true, meaning: '公司名称（显示用）', note: '同名不算错，但 code 必须能唯一对应主体' },
  { name: 'code', required: true, meaning: 'A 股代码，带交易所后缀', note: '如 600519.SH / 000858.SZ；不认识的代码执行后计「资料不足」，不编造读数' },
  { name: 'period', required: true, meaning: '报告期，写成日期', note: '如 2024-12-31；写「2024年」会被拒收（不猜是哪一期）' },
  { name: 'unit', required: false, meaning: '金额单位', note: '固定填 元。填「亿元」不会被自动换算，宁可自己换算成元' },
  { name: 'currency', required: false, meaning: '币种', note: '缺省即未知，不补默认' },
  { name: 'caliber', required: false, meaning: '报表口径', note: '合并 / 母公司；不填按合并，跨口径不直接比较' },
  { name: 'period_type', required: false, meaning: '期间类型', note: '年报 / 中报；筛选默认只比年报' },
  { name: 'source_url', required: false, meaning: '这份数字的原始出处', note: '建议填披露 PDF 链接；留空会在证据里显示「未标注来源」' },
  { name: 'available_at', required: false, meaning: '你最早能拿到它的日期', note: '决定「资料截止日」能不能用它；留空则该行待核验、不参与比较' },
  { name: 'disclosed_at', required: false, meaning: '披露日期', note: '可从巨潮/交易所公告页抄' },
  { name: 'revenue', required: false, meaning: '营业收入（元）' },
  { name: 'gross_profit', required: false, meaning: '毛利（元）= 营业收入 − 营业成本' },
  { name: 'net_profit', required: false, meaning: '净利润（含少数股东，元）' },
  { name: 'net_profit_consolidated', required: false, meaning: '归母净利润（元）' },
  { name: 'total_assets', required: false, meaning: '期末总资产（元）' },
  { name: 'total_liabilities', required: false, meaning: '期末总负债（元）' },
  { name: 'operating_cashflow', required: false, meaning: '经营活动现金流量净额（元）' },
]

// ── 公司详情：四期财务小表 + 趋势（B2）────────────────────────────
// 只读**这一份结果快照**里的原始观察（run.entities[].observations），不重新取数、不补缺失期。
type TrendObs = { metric: string; period: string; value: number; state?: string }

function observationsOf(entities: any[] | undefined, entityId: string): TrendObs[] {
  const hit = (entities || []).find(e => e?.entity_id === entityId)
  return (hit?.observations || []) as TrendObs[]
}

function CompanyTrend({ entities, entityId }: { entities: any[] | undefined; entityId: string }) {
  const obs = observationsOf(entities, entityId)
  if (!obs.length) {
    return <p className="text-xs text-ink-faint py-2">本份结果没有这家公司的原始观察，画不出趋势（不补数）。</p>
  }
  const periods = [...new Set(obs.map(o => o.period))].sort()
  const pick = (metric: string) => {
    const out: Record<string, number> = {}
    for (const o of obs) if (o.metric === metric && (o.state ?? 'ok') === 'ok' && Number.isFinite(o.value)) out[o.period] = o.value
    return out
  }
  // 归母优先；缓存里没有归母字段时退回净利润，并在表头写清「含少数股东」，不冒充归母
  const npConsolidated = pick('net_profit_consolidated')
  const npAll = pick('net_profit')
  const npKey = Object.keys(npConsolidated).length ? npConsolidated : npAll
  const revenue = pick('revenue'), gross = pick('gross_profit'), npc = npKey, ocf = pick('operating_cashflow')
  const yi = (v?: number) => (v == null ? null : v / 1e8)
  const fmt = (v?: number | null, digits = 2) => (v == null ? '—' : v.toFixed(digits))
  const max = Math.max(1, ...periods.map(p => yi(revenue[p]) ?? 0))
  const W = 360, H = 128, base = 100, usable = 76
  const slot = (W - 16) / periods.length
  const barW = Math.min(34, slot * 0.5)
  const points = periods.map((p, i) => {
    const gm = revenue[p] && gross[p] != null ? (gross[p] / revenue[p]) * 100 : null
    if (gm == null) return null
    const x = 8 + slot * i + slot / 2
    return { x, y: base - (gm / 100) * usable, gm }
  }).filter(Boolean) as Array<{ x: number; y: number; gm: number }>

  return (
    <div className="mt-3 rounded-lg border border-line bg-surface-sunken/40 p-3">
      <div className="flex flex-wrap items-center gap-2 mb-1">
        <span className="text-xs text-ink font-medium">公司详情 · 四期财务</span>
        <span className="text-xs text-ink-faint">柱＝营业收入（亿元）　线＝毛利率（%）</span>
        <span className="ml-auto text-xs text-ink-faint tabular-nums">{periods.join(' / ')}</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-md" role="img"
        aria-label={`营业收入与毛利率趋势：${periods.length} 期`}>
        <line x1="8" y1={base} x2={W - 8} y2={base} stroke="currentColor" strokeOpacity="0.25" />
        {periods.map((p, i) => {
          const v = yi(revenue[p])
          const h = v == null ? 0 : Math.max(1, (v / max) * usable)
          const x = 8 + slot * i + slot / 2 - barW / 2
          return (
            <g key={p}>
              <rect x={x} y={base - h} width={barW} height={h} rx="2" fill="#876728" fillOpacity="0.28" />
              {v != null && <text x={x + barW / 2} y={base - h - 4} textAnchor="middle" fontSize="9" fill="currentColor" fillOpacity="0.7">{v.toFixed(0)}</text>}
              <text x={x + barW / 2} y={H - 12} textAnchor="middle" fontSize="9" fill="currentColor" fillOpacity="0.55">{p.replace('年', '')}</text>
            </g>
          )
        })}
        {points.length > 1 && (
          <polyline fill="none" stroke="#2f6f5e" strokeWidth="1.6"
            points={points.map(pt => `${pt.x},${pt.y}`).join(' ')} />
        )}
        {points.map((pt, i) => (
          <circle key={i} cx={pt.x} cy={pt.y} r="2.2" fill="#2f6f5e">
            <title>{`毛利率 ${pt.gm.toFixed(2)}%`}</title>
          </circle>
        ))}
      </svg>
      <div className="overflow-x-auto">
        <table className="w-full text-xs tabular-nums mt-1">
          <thead>
            <tr className="text-ink-faint"><th className="text-left font-normal">年度</th>
              <th className="text-right font-normal">营业收入(亿)</th>
              <th className="text-right font-normal">毛利润(亿)</th>
              <th className="text-right font-normal">{Object.keys(npConsolidated).length ? '归母净利(亿)' : '净利润(含少数股东)(亿)'}</th>
              <th className="text-right font-normal">毛利率</th>
              <th className="text-right font-normal">经营现金流(亿)</th></tr>
          </thead>
          <tbody>
            {periods.map(p => (
              <tr key={p} className="text-ink">
                <td>{p}</td>
                <td className="text-right">{fmt(yi(revenue[p]))}</td>
                <td className="text-right">{fmt(yi(gross[p]))}</td>
                <td className="text-right">{fmt(yi(npc[p]))}</td>
                <td className="text-right">{revenue[p] && gross[p] != null ? (gross[p] / revenue[p] * 100).toFixed(2) + '%' : '—'}</td>
                <td className="text-right">{fmt(yi(ocf[p]))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-1.5 text-xs text-ink-faint leading-snug">
        金额按「元 → 亿元」÷1e8 换算；毛利率＝毛利润/营业收入；数据来自本份结果快照（不重新取数、不补齐缺失期、不改口径）。
      </p>
    </div>
  )
}

const RULE_EXAMPLES: Array<{ name: string; desc: string; rules: RuleDraft[] }> = [
  { name: '高毛利低负债', desc: '毛利率 ≥ 70%、资产负债率 ≤ 60%',
    rules: [{ field: 'gross_margin', op: '>=', value: '70', unit: '%' },
            { field: 'debt_ratio', op: '<=', value: '60', unit: '%' }] },
  { name: '增长且现金流覆盖', desc: '营收增速 ≥ 0%、经营现金流/归母净利 ≥ 1',
    rules: [{ field: 'revenue_growth', op: '>=', value: '0', unit: '%' },
            { field: 'cashflow_coverage', op: '>=', value: '1', unit: '' }] },
  // 注意：示例只能用后端开放的 5 个规则字段（revenue_growth/gross_margin/
  // operating_cashflow/debt_ratio/cashflow_coverage）。曾用 net_profit 写示例，
  // 真机执行直接 400「未知字段」——示例必须自己先能跑通。
  { name: '现金覆盖且低负债', desc: '经营现金流/归母净利 ≥ 1、资产负债率 ≤ 50%',
    rules: [{ field: 'cashflow_coverage', op: '>=', value: '1', unit: '' },
            { field: 'debt_ratio', op: '<=', value: '50', unit: '%' }] },
]

type WatchItem = { code: string; name?: string; note?: string; added_at?: string
                    updated_at?: string; version?: number }
type TplPreview = {
  rules: { field: string; op: string; value: number; unit?: string }[]
  rules_count?: number
  period_policy?: { mode?: string; report_period?: string }
  notes?: string
  warnings?: string[]
}
type CostLedger = {
  prepare_requests: number; previews: number; compute_runs: number; compute_ms: number
  cache_hits: number; runs_total: number; model_calls: number
  network_requests: number | null       // 传输层不暴露连接事件时未知（null），不冒充 0
  adapter_calls?: number; network_attempts?: number; network_denied?: number
  network_ok?: number; network_failed?: number
  external_fee: string; updated_at?: string; integrity?: string
}

// ── C1 数据准备：来源表 / 材料投影 / 覆盖预览 / 受约束取数 ──
type PrepSource = {
  id: string; kind: string; label: string; requires_network: boolean
  licence: string; fee: string; fields?: string[]; update?: string
  note?: string; constraint?: string; available?: boolean | null
  available_note?: string | null
}
type PrepDataset = {
  dataset_id: string; name: string; row_count?: number; codes?: string[]
  periods?: string[]; source?: string; note?: string; usable?: boolean
  official?: boolean; user_provided?: boolean; provenance_kind?: string
}
type PrepMaterials = {
  official_cache: {
    available: boolean; row_count?: number; codes?: string[]; periods?: string[]
    label?: string; note?: string; source_url_count?: number; fact_id_count?: number
  }
  datasets: PrepDataset[]
  watchlist: { items: string[]; missing_material: string[] }
  research?: {
    status: string; note?: string
    tasks?: { task_id?: string; title?: string
              entities?: { entity?: string; entity_id?: string; facts?: number }[]
              periods?: string[] }[]
  }
}
type PrepFieldStatus = { status: string; missing?: string[]; note?: string; previous_period?: string }
type PrepPlanPeriod = {
  period: string; status: string; via?: string; dataset_id?: string
  entity?: string; source_url?: string; note?: string
  material?: boolean; computable?: boolean; fields?: Record<string, PrepFieldStatus>
}
type PrepPlan = {
  items: { code: string; entity?: string; periods: PrepPlanPeriod[]; note?: string }[]
  totals: { covered: number; partial?: number; material_only?: number; missing: number
            codes: number; periods: number }
  errors?: string[]
  reading: string
}
type PrepCollectCompany = {
  code: string; status: string; category?: string; detail?: string
  rows?: number; periods?: string[]; note?: string
}
type PrepCollect = {
  status: string; source: string
  dataset: { dataset_id: string; name: string; row_count?: number; codes?: string[]; periods?: string[] } | null
  companies: PrepCollectCompany[]
  counters?: {
    adapter_calls?: number; network_attempts?: number; network_denied?: number
    network_ok?: number; network_failed?: number
    network_requests?: number | null; network_requests_note?: string
    pre_connect_failures?: number
  }
  metering?: { recorded: boolean; recording?: { ok?: boolean; reason?: string; detail?: string } }
  note?: string
}

export default function StockScreen() {
  const [fields, setFields] = useState<FieldsPayload | null>(null)
  const [pool, setPool] = useState<PoolEntity[]>([])
  const [source, setSource] = useState<SourceMeta | null>(null)
  const [poolError, setPoolError] = useState('')
  // R1.2：数据来源选择（'' = 官方披露缓存）与上传状态
  const [datasets, setDatasets] = useState<DatasetSummary[]>([])
  const [officialSource, setOfficialSource] = useState<OfficialSource | null>(null)
  const [datasetId, setDatasetId] = useState('')
  const [uploadBusy, setUploadBusy] = useState('')
  const [uploadMsg, setUploadMsg] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [poolQuery, setPoolQuery] = useState('')
  const [manualCode, setManualCode] = useState('')
  const [extraCodes, setExtraCodes] = useState<string[]>([])
  const [asOf, setAsOf] = useState(todayISO())
  const [rules, setRules] = useState<RuleDraft[]>([
    { field: 'revenue_growth', op: '>=', value: '0', unit: '%' },
    { field: 'gross_margin', op: '>=', value: '20', unit: '%' },
    { field: 'debt_ratio', op: '<=', value: '60', unit: '%' },
  ])
  // B2：结果分栏筛选 + 分页（每栏默认 20 家，超出点「显示更多」）
  const [resultFilter, setResultFilter] = useState<'all' | 'pass' | 'fail' | 'insufficient'>('all')
  const [groupPages, setGroupPages] = useState<Record<string, number>>({})
  const PAGE_SIZE = 20
  const [preview, setPreview] = useState<PreviewPayload | null>(null)
  const [run, setRun] = useState<Run | null>(null)
  const [viewingOriginal, setViewingOriginal] = useState(false)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [openEvidence, setOpenEvidence] = useState('')
  const [evidence, setEvidence] = useState<Evidence | null>(null)
  const [evLoading, setEvLoading] = useState(false)
  const [evError, setEvError] = useState('')
  const [busy, setBusy] = useState('')
  // 自助研究权限（服务端判定）：只读账号不显示写入口，避免点出 403 死路
  const [cap, setCap] = useState<{ can_research: boolean; role: string; read_only_reason: string } | null>(null)
  const [err, setErr] = useState('')
  const [notice, setNotice] = useState('')
  const [histOpen, setHistOpen] = useState(false)
  const [cmpA, setCmpA] = useState('')
  const [cmpB, setCmpB] = useState('')
  const [cmp, setCmp] = useState<CompareResult | null>(null)
  const [cmpBusy, setCmpBusy] = useState(false)
  const [cmpErr, setCmpErr] = useState('')
  const [presets, setPresets] = useState<Preset[]>([])
  // B3 持续研究：个人清单（观察对象，不是持仓）、规则模板、成本账本
  const [watch, setWatch] = useState<WatchItem[]>([])
  const [watchNote, setWatchNote] = useState<Record<string, string>>({})
  const [watchBusy, setWatchBusy] = useState('')
  const [watchMsg, setWatchMsg] = useState('')
  // W2：简报面板（查看变化）——清单 rev + 跟踪基准 + 两份记录选择 + 只读投影
  const [watchRev, setWatchRev] = useState<number | string>('')
  const [watchBaseline, setWatchBaseline] = useState('')
  const [briefOpen, setBriefOpen] = useState(false)
  const [briefBase, setBriefBase] = useState('')
  const [briefCurrent, setBriefCurrent] = useState('')
  const [briefBusy, setBriefBusy] = useState('')
  const [brief, setBrief] = useState<Brief | null>(null)
  const [briefErr, setBriefErr] = useState('')
  const [briefMsg, setBriefMsg] = useState('')
  const [tplPreview, setTplPreview] = useState<TplPreview | null>(null)
  const [tplErr, setTplErr] = useState('')
  const [tplMsg, setTplMsg] = useState('')
  const tplFile = useRef<HTMLInputElement | null>(null)
  const [cost, setCost] = useState<CostLedger | null>(null)
  // C1 数据准备：材料 → 预览 → 数据集 → 筛选（取数只接受来源 ID + 公司 + 期间）
  const [prepMaterials, setPrepMaterials] = useState<PrepMaterials | null>(null)
  const [prepSources, setPrepSources] = useState<PrepSource[]>([])
  const [prepPlan, setPrepPlan] = useState<PrepPlan | null>(null)
  const [prepSource, setPrepSource] = useState('official_cache')
  const [prepPeriods, setPrepPeriods] = useState('2023,2024')
  const [prepResult, setPrepResult] = useState<PrepCollect | null>(null)
  const [prepBusy, setPrepBusy] = useState('')
  const [prepMsg, setPrepMsg] = useState('')
  const [costOpen, setCostOpen] = useState(false)
  const [presetSel, setPresetSel] = useState('')
  const [presetName, setPresetName] = useState('')
  const [presetBusy, setPresetBusy] = useState('')
  const [presetMsg, setPresetMsg] = useState('')
  // R1.1：期间政策（默认统一报告期 = 池内最新可用年度；缺期/冲突 → 资料不足，不降年）
  const [periodMode, setPeriodMode] = useState<'uniform' | 'pinned' | 'latest_each'>('uniform')
  const [periodPin, setPeriodPin] = useState('')
  // 展示排序（只改展示顺序，不改变结果身份与哈希）
  const [sortField, setSortField] = useState('')
  const [sortDir, setSortDir] = useState<'desc' | 'asc'>('desc')

  const fieldById = useMemo(
    () => new Map((fields?.fields ?? []).map(f => [f.field_id, f])), [fields])
  const periodPolicy = (): PeriodPolicy =>
    periodMode === 'pinned' ? { mode: 'pinned', report_period: periodPin.trim() }
      : { mode: periodMode, report_period: '' }

  // 数据来源切换是并发的：只让**最后一次**请求的结果落到界面上，
  // 否则先发后至的旧来源会覆盖新来源的目录与来源标注（来源与池子必须一致）。
  const poolSeq = useRef(0)
  const loadPool = async (did: string = '') => {
    const seq = ++poolSeq.current
    try {
      const url = did ? `/api/screen/pool?dataset=${encodeURIComponent(did)}` : '/api/screen/pool'
      const d = await (await fetch(url)).json()
      if (seq !== poolSeq.current) return
      if (d.error) throw new Error(d.error)
      const next: PoolEntity[] = d.available_pool ?? []
      setPool(next)
      setSource(d.source ?? null)
      setPoolError('')
      // 换数据来源后：勾选状态按新目录清理；手输芯片保留（执行时计入资料不足，不编造）
      setSelected(prev => prev.filter(c => next.some(p => p.code === c)))
      return next
    } catch (e: any) {
      if (seq !== poolSeq.current) return null
      setPoolError(`股票池目录加载失败：${e?.message ?? e}`)
      return null
    }
  }
  const loadDatasets = async () => {
    try {
      const d = await (await fetch('/api/screen/datasets?limit=50')).json()
      setDatasets(d.datasets ?? [])
      setOfficialSource(d.official_source ?? null)
    } catch { /* 保持上一次清单，不编造 */ }
  }
  const changeSource = async (did: string) => {
    setDatasetId(did); setPreview(null); setUploadMsg('')
    await loadPool(did)
  }
  const readFileText = (file: File) => new Promise<string>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error(
      '读取文件超过 10 秒没有返回：请确认文件没有损坏，或先「下载模板」另存后重新选择')), 10000)
    file.text().then(
      t => { window.clearTimeout(timer); resolve(t) },
      e => { window.clearTimeout(timer); reject(e) },
    )
  })
  const downloadTemplate = async () => {
    setUploadMsg('')
    try {
      const res = await fetch('/api/screen/template')
      if (!res.ok) throw new Error(await res.text().catch(() => '模板下载失败'))
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = 'weavemind-screen-template.csv'
      document.body.appendChild(a); a.click(); a.remove()
      URL.revokeObjectURL(url)
      setUploadMsg('模板已下载（CSV，Excel 可直接打开）：请删掉两行示例后填入自己的数据，再上传')
    } catch (e: any) { setUploadMsg(`模板下载失败：${e?.message ?? e}`) }
  }
  const onUploadFile = async (file: File | null, input?: HTMLInputElement | null) => {
    if (guardReadOnly()) return
    if (!file) return
    setUploadBusy('upload'); setUploadMsg('')
    try {
      // 先读出文本再清空 input：先清空会让浏览器释放 File 句柄，读取悬空（界面卡在「上传中…」）。
      // 读取本身也给超时：读不动就明确报错，不让用户对着「上传中…」干等。
      const text = await readFileText(file)
      if (input) input.value = ''
      const name = file.name.replace(/\.[^.]+$/, '')
      const { ok, d } = await post('/api/screen/datasets', {
        name, filename: file.name, content: text,
      })
      if (!ok) throw new Error(d.error || '上传失败')
      await loadDatasets()
      const ds = d.dataset as DatasetSummary
      setDatasetId(ds.dataset_id)
      await loadPool(ds.dataset_id)
      setUploadMsg(`已导入「${ds.name}」：${ds.row_count} 行、${(ds.codes ?? []).length} 家公司、`
        + `期间 ${(ds.periods ?? []).join('、') || '无'}${d.reused ? '（同一份数据已存在：直接复用，不重复入库）' : ''}。`
        + '它只在本账户可见，且始终标为「用户提供」，不是官方数据。')
    } catch (e: any) { setUploadMsg(`上传失败：${e?.message ?? e}`) } finally { setUploadBusy('') }
  }
  const loadFields = async () => {
    try {
      const d = await (await fetch('/api/screen/fields')).json()
      setFields(d)
    } catch { /* 读不到就保持空（选择器会显示为空，不编字段） */ }
  }
  const loadRuns = async () => {
    try {
      const d = await (await fetch('/api/screen/runs?limit=20')).json()
      setRuns(d.runs ?? [])
    } catch { /* 保持上一次列表，不编 */ }
  }
  const loadPresets = async () => {
    try {
      const d = await (await fetch('/api/screen/presets?limit=100')).json()
      setPresets(d.presets ?? [])
    } catch { /* 读不到就保持空列表，不编预设 */ }
  }
  const loadCapabilities = async () => {
    try {
      const d = await (await fetch('/api/screen/capabilities')).json()
      if (d && typeof d.can_research === 'boolean') setCap(d)
    } catch { /* 拿不到就不收写入口，服务端仍会 403 并给原因 */ }
  }
  useEffect(() => { loadPool(); loadFields(); loadRuns(); loadPresets(); loadDatasets(); loadCapabilities(); loadWatch(); loadPrep() }, [])

  // 带超时的请求：网络/服务端卡住时要给用户一个终点，而不是永远的「执行中…」「对比中…」
  const fetchJSON = async (path: string, init?: RequestInit, ms = 20000) => {
    const ctl = new AbortController()
    const timer = window.setTimeout(() => ctl.abort(), ms)
    try {
      const res = await fetch(path, { ...init, signal: ctl.signal })
      const d = await res.json().catch(() => ({}))
      return { ok: res.ok, status: res.status, d }
    } catch (e: any) {
      if (e?.name === 'AbortError') {
        throw new Error(`请求超过 ${Math.round(ms / 1000)} 秒没有返回（网络或服务端卡住）：请重试`)
      }
      throw e
    } finally { window.clearTimeout(timer) }
  }

  const post = async (path: string, body: any) => {
    try {
      return await fetchJSON(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    } catch (e: any) {
      return { ok: false, status: 0, d: { error: String(e?.message ?? e) } }
    }
  }

  // 第 1 步：股票池（目录勾选 + 手输代码）与资料截止日
  const toggleCode = (code: string) => setSelected(prev =>
    prev.includes(code) ? prev.filter(c => c !== code) : [...prev, code])
  const addManual = () => {
    const code = manualCode.trim().toUpperCase()
    if (!code) return
    if (!selected.includes(code) && !extraCodes.includes(code)) setExtraCodes(prev => [...prev, code])
    setManualCode('')
  }
  const effectivePool = useMemo(
    () => [...new Set([...selected, ...extraCodes])], [selected, extraCodes])
  const inCatalog = (code: string) => pool.some(p => p.code === code)

  // 预设：保存/载入/删除（预设只是偏好，永不改动已冻结结果）
  const applyPreset = async (pid: string) => {
    setPresetSel(pid); setPresetMsg('')
    if (!pid) return
    setPresetBusy('load')
    try {
      const d = await (await fetch(`/api/screen/preset?id=${encodeURIComponent(pid)}`)).json()
      if (d.error) throw new Error(d.error)
      const p = d.preset as Preset
      const codes = p.pool ?? []
      const did = String(p.dataset_id || '').trim()
      let catalog = pool
      let sourceNote = ''
      if (did) {
        const known = datasets.find(x => x.dataset_id === did)
        setDatasetId(did)
        const nextPool = await loadPool(did)
        catalog = nextPool ?? pool
        sourceNote = known
          ? `含数据来源「${known.name}」（用户提供数据）`
          : `但它记录的数据来源 ${did.slice(0, 8)}… 已不在可用列表里（可能已删除）：当前会按「不可用来源」处理，请重新选择数据来源`
      } else {
        sourceNote = '该预设没有记录数据来源（旧版预设）：沿用界面上当前来源，执行前请确认来源'
      }
      setSelected(codes.filter(c => catalog.some(x => x.code === c)))
      setExtraCodes(codes.filter(c => !catalog.some(x => x.code === c)))
      if (p.as_of) setAsOf(p.as_of)
      setRules((p.rules ?? []).map(r => ({
        field: r.field, op: r.op, value: String(r.value),
        unit: r.unit || (fieldById.get(r.field)?.kind === 'amount' ? '元' : ''),
      })))
      setPresetName(p.name)
      setPresetMsg(`已载入预设「${p.name}」：${sourceNote}；规则需你确认后才会执行（载入不等于执行）。`)
    } catch (e: any) { setPresetMsg(`载入预设失败：${e?.message ?? e}`) } finally { setPresetBusy('') }
  }
  const readOnly = !!cap && !cap.can_research
  const guardReadOnly = () => {
    if (!readOnly) return false
    setErr(cap?.read_only_reason || '当前账号是只读角色，不能执行写操作')
    return true
  }
  const savePreset = async () => {
    if (guardReadOnly()) return
    const name = presetName.trim() || presets.find(p => p.preset_id === presetSel)?.name || ''
    if (!name) { setPresetMsg('给预设起个名字再保存'); return }
    if (!effectivePool.length) { setPresetMsg('股票池为空：先勾选或输入代码'); return }
    setPresetBusy('save'); setPresetMsg('')
    try {
      const payloadRules = rulePayload().filter(r => r.field)
      const body: any = { name, pool: effectivePool, rules: payloadRules, as_of: asOf }
      if (datasetId) body.dataset_id = datasetId
      if (presetSel) body.preset_id = presetSel
      const { ok, d } = await post('/api/screen/preset', body)
      if (!ok) throw new Error(d.error || '保存失败')
      setPresetSel(d.preset.preset_id)
      setPresetName(d.preset.name)
      await loadPresets()
      setPresetMsg(`已${presetSel ? '更新' : '保存'}预设「${d.preset.name}」：记住池${d.preset.dataset_id ? '、规则与数据来源' : '与规则'}，不绑定任何结果。`)
    } catch (e: any) { setPresetMsg(String(e?.message ?? e)) } finally { setPresetBusy('') }
  }
  const removePreset = async () => {
    if (guardReadOnly()) return
    if (!presetSel) return
    setPresetBusy('delete'); setPresetMsg('')
    try {
      const { ok, d } = await post('/api/screen/preset/delete', { preset_id: presetSel })
      if (!ok) throw new Error(d.error || '删除失败')
      setPresetSel(''); setPresetName('')
      await loadPresets()
      setPresetMsg('预设已删除（已冻结的结果不受影响）。')
    } catch (e: any) { setPresetMsg(String(e?.message ?? e)) } finally { setPresetBusy('') }
  }

  // 「过期/不可重算」：实现版本或输入文件变了 → 原版只能查看，不能冒充现状
  const staleReasons = useMemo(() => {
    const out: string[] = []
    if (!run) return out
    if (fields?.impl_version && run.impl_version && fields.impl_version !== run.impl_version) {
      out.push(`实现版本已变：这份由 ${run.impl_version} 生成，当前是 ${fields.impl_version}。旧结果不会按新实现重算，只能查看原版。`)
    }
    if (source?.sha256 && run.source?.sha256 && source.sha256 !== run.source.sha256) {
      out.push(`输入文件已变：这份用的是 ${short12(run.source.sha256)}…，当前缓存是 ${short12(source.sha256)}…。要看新数据下的结果，请「用当前数据重新筛选」。`)
    }
    return out
  }, [run, fields, source])

  const doPreview = async () => {
    if (guardReadOnly()) return
    if (!effectivePool.length) { setErr('请先勾选或输入至少一个股票代码'); return }
    setBusy('preview'); setErr('')
    try {
      if (periodMode === 'pinned' && !periodPin.trim()) {
        setErr('「指定年度」需要填年份（如 2024）；也可以用「统一报告期」由系统取池内最新年度')
        setBusy(''); return
      }
      const { ok, d } = await post('/api/screen/preview',
        { pool: effectivePool, as_of: asOf, period_policy: periodPolicy(),
          dataset_id: datasetId || undefined })
      if (!ok) throw new Error(d.error || '预览失败')
      setPreview(d)
    } catch (e: any) { setNotice(''); setErr(String(e?.message ?? e)); setPreview(null) } finally { setBusy('') }
  }

  // 第 2 步：规则（字段 + 比较符 + 阈值 + 单位；未知字段/比较符由后端拒绝，前端不猜）
  const addRule = () => setRules(prev => [...prev, { field: '', op: '>=', value: '', unit: '' }])
  const updateRule = (i: number, patch: Partial<RuleDraft>) => setRules(prev =>
    prev.map((r, idx) => idx === i ? { ...r, ...patch } : r))
  const removeRule = (i: number) => setRules(prev => prev.filter((_, idx) => idx !== i))
  // 只有「字段和阈值都空」的行才算没填（发送前丢弃）；填了阈值没选字段的行照发，
  // 让后端给出明确的「字段未知」错误，而不是前端悄悄删掉一条用户以为生效的规则。
  const activeRules = useMemo(
    () => rules.filter(r => r.field !== '' || String(r.value) !== ''), [rules])
  const rulePayload = () => activeRules.map(r => ({
    field: r.field, op: r.op,
    value: r.value === '' ? '' : Number(r.value),
    unit: fieldById.get(r.field)?.kind === 'amount' ? (r.unit || '元') : undefined,
  }))

  // 第 3 步：执行（结果冻结 + 复用语义）
  const doRun = async () => {
    if (guardReadOnly()) return
    if (!effectivePool.length) { setErr('请先勾选或输入至少一个股票代码'); return }
    if (!activeRules.length) { setErr('至少需要一条筛选条件（选字段 + 填阈值）'); return }
    if (periodMode === 'pinned' && !periodPin.trim()) {
      setErr('「指定年度」需要填年份（如 2024）；也可以用「统一报告期」由系统取池内最新年度')
      return
    }
    setBusy('run'); setErr(''); setEvidence(null); setOpenEvidence('')
    try {
      const body: any = {
        pool: effectivePool, as_of: asOf, rules: rulePayload(),
        period_policy: periodPolicy(), dataset_id: datasetId || undefined,
      }
      if (sortField) body.sort = { field: sortField, direction: sortDir }
      const { ok, d } = await post('/api/screen/run', body)
      if (!ok) throw new Error(d.error || '筛选失败')
      const fresh = d.run as Run
      const sameAsShown = run !== null && fresh.run_id === run.run_id
      const reused = !!fresh.reused
      setRun(fresh)
      setViewingOriginal(false)
      loadRuns()
      setErr('')
      if (reused && sameAsShown) setNotice('数据与规则都没变：复用当前这份已冻结结果，未重算。')
      else if (reused) setNotice(`输入、规则、截止日都与历史某次完全相同（哈希一致）：直接打开那份原版（${fresh.run_id.slice(0, 8)}…），未重算。`)
      else if (run !== null) setNotice('已生成新结果（旧结果原样保留，可在历史结果里重开）。')
      else setNotice('')
    } catch (e: any) { setNotice(''); setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  // 查看原版（GET，永不重算）与历史重开
  const openRun = async (runId: string) => {
    setBusy('original'); setErr(''); setEvidence(null); setOpenEvidence('')
    try {
      const q = sortField ? `&sort=${encodeURIComponent(`${sortField}:${sortDir}`)}` : ''
      const resp = await fetchJSON(`/api/screen/run?id=${encodeURIComponent(runId)}${q}`)
      const d = resp.d
      if (!resp.ok) throw new Error(d.error || '读取原版失败')
      setRun(d.run as Run)
      setViewingOriginal(true)
      // 表单回填为原版参数：目录里有的回勾，目录里没有的落手输芯片——
      // 这样「用当前数据重新筛选」发的是同一池子，差异只来自数据与规则。
      const origPool: string[] = (d.run as Run).pool ?? []
      setSelected(origPool.filter(c => pool.some(p => p.code === c)))
      setExtraCodes(origPool.filter(c => !pool.some(p => p.code === c)))
      setAsOf((d.run as Run).as_of)
      const pol = (d.run as Run).period_policy
      if (pol && pol.mode) { setPeriodMode(pol.mode); setPeriodPin(pol.report_period || '') }
      setRules(((d.run as Run).rules ?? []).map((r: any) => ({
        field: r.field, op: r.op, value: String(r.value ?? ''), unit: r.unit ?? '',
      })))
      setNotice('')
      loadRuns()
    } catch (e: any) { setErr(String(e?.message ?? e)) } finally { setBusy('') }
  }

  // 人工复筛对比：只读两份冻结结果，不重算、不改任何一份
  const runCompare = async () => {
    if (!cmpA || !cmpB) { setCmpErr('先选两份结果：一份作基准、一份作对照'); return }
    if (cmpA === cmpB) { setCmpErr('同一份不能和自己对比：请选两份不同的结果'); return }
    setCmpBusy(true); setCmpErr(''); setCmp(null)
    try {
      const { ok, d } = await fetchJSON(
        `/api/screen/compare?a=${encodeURIComponent(cmpA)}&b=${encodeURIComponent(cmpB)}`)
      if (!ok) throw new Error(d.error || '对比失败')
      setCmp(d as CompareResult)
    } catch (e: any) { setCmpErr(String(e?.message ?? e)) } finally { setCmpBusy(false) }
  }

  // ── B3：个人研究清单（观察对象，不是持仓；只在本作用域可见）──
  // 统一入口：items/rev/baseline_run 一起落地（简报面板要拿 rev 做乐观并发、拿基准做默认 base）
  const applyWatchlist = (wl?: { items?: WatchItem[]; rev?: number | string
                                 baseline_run?: string } | null) => {
    const items: WatchItem[] = wl?.items ?? []
    setWatch(items)
    setWatchNote(Object.fromEntries(items.map(w => [w.code, w.note ?? ''])))
    setWatchRev(wl?.rev ?? '')
    setWatchBaseline(String(wl?.baseline_run ?? ''))
  }
  const loadWatch = async () => {
    try {
      const { ok, d } = await fetchJSON('/api/screen/watchlist')
      if (!ok) return
      applyWatchlist(d.watchlist)
    } catch { /* 清单读取失败不阻断筛选 */ }
  }

  // 清单 → 股票池（观察对象回到筛选，不自动执行）
  const focusWatchInRun = (code: string) => {
    if (pool.some(p => p.code === code)) {
      setSelected(prev => (prev.includes(code) ? prev : [...prev, code]))
      setWatchMsg(`已把 ${code} 勾进第 1 步的股票池；点「执行筛选」才会生成结果。`)
    } else if (!extraCodes.includes(code)) {
      setExtraCodes(prev => [...prev, code])
      setWatchMsg(`目录里没有 ${code}：已作为手输代码放进股票池（若数据里没有这家，会如实标「资料不足」）。`)
    }
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const watchHas = (code: string) => watch.some(w => w.code === code)

  const toggleWatch = async (code: string, name?: string) => {
    if (readOnly) { setWatchMsg(cap?.read_only_reason || '只读账号不能维护研究清单'); return }
    setWatchBusy(code); setWatchMsg('')
    try {
      const action = watchHas(code) ? 'remove' : 'add'
      const { ok, d } = await fetchJSON('/api/screen/watchlist', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, code, name: name || '' }),
      })
      if (!ok) throw new Error(d.error || '清单操作失败')
      applyWatchlist(d.watchlist)
      setWatchMsg(action === 'add' ? `已加入清单：${code}（观察对象，不是持仓）` : `已移出清单：${code}`)
    } catch (e: any) { setWatchMsg(String(e?.message ?? e)) } finally { setWatchBusy('') }
  }

  const saveWatchNote = async (code: string) => {
    if (readOnly) return
    const note = (watchNote[code] ?? '').slice(0, 500)
    setWatchBusy(code); setWatchMsg('')
    try {
      // 乐观并发：带上「上次看到的该条 version」；期间被另一页面改过 → 409，不覆盖对方
      const base = watch.find(w => w.code === code)?.version
      const { ok, d } = await fetchJSON('/api/screen/watchlist', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'note', code, note, if_version: base }),
      })
      if (!ok) {
        if (d.code === 'version_conflict') {
          setWatchMsg('这条被另一个页面改过（版本冲突，本次没有写入）：已重新读取，请确认后再改一次。')
          await loadWatch()
          return
        }
        throw new Error(d.error || '备注保存失败')
      }
      applyWatchlist(d.watchlist)
      setWatchMsg(`备注已保存：${code}`)
    } catch (e: any) { setWatchMsg(String(e?.message ?? e)) } finally { setWatchBusy('') }
  }

  // ── W2：查看变化（自选变化简报）──
  // 只读投影：零模型调用、零外部请求；**查看简报不推进基准**，基准只能显式设置。
  const runOption = (r: RunSummary) =>
    `${fmtTime(r.created_at)} · 截止 ${r.as_of || '—'}` +
    ` · 过${r.counts?.pass ?? 0}/败${r.counts?.fail ?? 0}/不足${r.counts?.insufficient ?? 0}` +
    ` · ${r.source?.synthetic ? '合成演示' : r.source?.user_provided ? '用户提供' : '官方缓存'}` +
    `${r.usable === false ? ' · 完整性未通过（可选但会失败）' : ''} · ${short12(r.run_id)}…`

  const findRun = (token: string): RunSummary | null => {
    if (!token) return null
    return runs.find(r => r.run_id === token)
      || runs.find(r => r.run_id.startsWith(token)) || null
  }

  const openBrief = () => {
    const next = !briefOpen
    setBriefOpen(next)
    if (!next) return
    setBriefErr(''); setBriefMsg('')
    const usable = runs.filter(r => r.usable !== false)
    const cur = briefCurrent || usable[0]?.run_id || ''
    if (!briefCurrent && cur) setBriefCurrent(cur)
    if (!briefBase) {
      const bl = findRun(watchBaseline)
      const baseRun = (bl && bl.usable !== false && bl.run_id !== cur) ? bl : null
      const fallback = usable.find(r => r.run_id !== cur)
      setBriefBase(baseRun?.run_id || fallback?.run_id || '')
    }
  }

  const generateBrief = async () => {
    setBriefErr(''); setBriefMsg('')
    if (!briefBase || !briefCurrent) { setBriefErr('请先选「上次记录」和「本次记录」。'); return }
    if (briefBase === briefCurrent) {
      setBriefErr('两份选的是同一条记录：同一份结果之间的「变化」没有意义，后端也会拒绝。')
      return
    }
    setBriefBusy('brief'); setBrief(null)
    try {
      const { ok, d, status } = await fetchJSON(
        `/api/screen/brief?base=${encodeURIComponent(briefBase)}&current=${encodeURIComponent(briefCurrent)}`,
        undefined, 30000)
      if (!ok) {
        const errs = Array.isArray(d?.errors) && d.errors.length ? d.errors.join('；') : ''
        setBriefErr(errs || d?.error || `简报生成失败（HTTP ${status}）`)
        return
      }
      setBrief(d as Brief)
      if (d?.watchlist?.rev !== undefined) setWatchRev(d.watchlist.rev)
      if (d?.watchlist?.baseline_run !== undefined) setWatchBaseline(String(d.watchlist.baseline_run))
      setBriefMsg('简报已生成（只读投影，没有重算、没有改任何一份结果）。')
    } catch (e: any) {
      setBriefErr(String(e?.message ?? e))
    } finally { setBriefBusy('') }
  }

  const setBriefBaseline = async (clear = false) => {
    if (readOnly) { setBriefErr(cap?.read_only_reason || '只读账号不能设置跟踪基准'); return }
    setBriefErr(''); setBriefMsg('')
    if (!clear && !briefCurrent) { setBriefErr('请先选「本次记录」再设为基准。'); return }
    setBriefBusy(clear ? 'clear' : 'baseline')
    try {
      const { ok, d, status } = await post('/api/screen/watchlist', clear
        ? { action: 'clear_baseline', if_rev: watchRev }
        : { action: 'set_baseline', run_id: briefCurrent, if_rev: watchRev })
      if (!ok) {
        if (status === 409 || d?.code === 'version_conflict') {
          setBriefErr('清单已被另一个页面修改（版本冲突，本次没有写入）：已重新读取，请确认后再点一次。')
          await loadWatch()
          return
        }
        setBriefErr(d?.error || `设置跟踪基准失败（HTTP ${status}）`)
        return
      }
      applyWatchlist(d.watchlist)
      setBriefMsg(clear ? '已清除跟踪基准。' : `已把「本次记录」设为跟踪基准：${short12(briefCurrent)}…`)
    } catch (e: any) {
      setBriefErr(String(e?.message ?? e))
    } finally { setBriefBusy('') }
  }

  // ── B3：规则模板（只含规则/期间策略/版本/说明；导入只校验+预览，不执行）──
  const exportTemplate = async () => {
    setTplErr(''); setTplMsg('')
    try {
      const payload = {
        rules: rulePayload(),
        period_policy: { mode: periodMode, report_period: periodMode === 'pinned' ? periodPin : '' },
      }
      const resp = await fetchJSON('/api/screen/rules/export', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!resp.ok) throw new Error(resp.d.error || '导出失败')
      const blob = new Blob([JSON.stringify(resp.d.template, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'screen-rules-template.json'
      document.body.appendChild(a); a.click(); a.remove()
      URL.revokeObjectURL(url)
      setTplMsg('规则模板已导出：只含条件、期间策略与版本，不含股票池/备注/结果/账号信息。')
    } catch (e: any) { setTplErr(String(e?.message ?? e)) }
  }

  const importTemplate = async (file: File) => {
    setTplErr(''); setTplMsg(''); setTplPreview(null)
    let parsed: any
    try {
      parsed = JSON.parse(await file.text())
    } catch {
      setTplErr('模板不是合法 JSON：请选择本产品导出的 .json 文件（未做任何应用）')
      return
    }
    try {
      const { ok, d } = await fetchJSON('/api/screen/rules/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ template: parsed }),
      })
      if (!ok) throw new Error(d.error || '模板校验未通过')
      setTplPreview({ ...(d.preview as TplPreview), warnings: d.warnings ?? [] })
      setTplMsg('模板已校验（未执行、未改表单）：确认无误后点「应用这些条件」。')
    } catch (e: any) { setTplErr(String(e?.message ?? e)) }
  }

  const applyTemplate = () => {
    if (!tplPreview) return
    setRules(tplPreview.rules.map(r => ({
      field: r.field, op: r.op, value: String(r.value ?? ''),
      unit: r.unit ?? '',
    })))
    const pol = tplPreview.period_policy
    if (pol?.mode) { setPeriodMode(pol.mode as any); setPeriodPin(pol.report_period || '') }
    setTplMsg('已把模板条件填进表单（仍未执行；点「执行筛选」才会生成结果）。')
    setTplPreview(null)
  }

  // ── B3：成本与请求分账 ──
  const loadCost = async () => {
    try {
      const { ok, d } = await fetchJSON('/api/screen/cost')
      if (ok) setCost(d.cost as CostLedger)
    } catch { /* 账本读取失败不影响筛选 */ }
  }

  // ── C1：数据准备（先有材料，再筛选；已有数据内搜索≠取数） ──
  const loadPrep = async () => {
    try {
      const d = await (await fetch('/api/screen/prepare/materials')).json()
      if (d.error) throw new Error(d.error)
      setPrepMaterials(d.materials ?? null)
      setPrepSources(d.sources ?? [])
    } catch { /* 读不到就保持空材料，不编数据 */ }
  }
  const prepCodes = () => [...new Set([
    ...effectivePool,
    ...((prepMaterials?.watchlist?.items) ?? []),
  ])]
  const prepPeriodList = () => prepPeriods.split(/[,，\s]+/).map(x => x.trim()).filter(Boolean)
  const runPrepPlan = async () => {
    if (guardReadOnly()) return
    setPrepBusy('plan'); setPrepMsg(''); setPrepPlan(null)
    try {
      const { ok, d } = await post('/api/screen/prepare/plan',
        { codes: prepCodes(), periods: prepPeriodList() })
      if (!ok) throw new Error(d.error || '覆盖预览失败')
      setPrepPlan(d as PrepPlan)
      setPrepMsg('已按当前材料预览覆盖与缺口：缺的不是「搜索过」，是没有材料，需要单独准备。')
    } catch (e: any) { setPrepMsg(`覆盖预览失败：${e?.message ?? e}`) } finally { setPrepBusy('') }
  }
  const runPrepCollect = async (probe: boolean) => {
    if (guardReadOnly()) return
    const codes = prepCodes()
    if (!codes.length) { setPrepMsg('先选公司（勾选股票池，或把公司加入研究清单），再取数'); return }
    setPrepBusy(probe ? 'probe' : 'collect'); setPrepMsg(''); setPrepResult(null)
    try {
      const { ok, d } = await fetchJSON('/api/screen/prepare/collect', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: prepSource, codes, periods: prepPeriodList(), probe_only: probe }),
      }, probe ? 60000 : 300000)
      if (!ok) throw new Error((d as any).error || '取数失败')
      const res = d as PrepCollect
      setPrepResult(res)
      await loadDatasets(); await loadPrep(); await loadCost()
      if (res.dataset?.dataset_id) { setDatasetId(res.dataset.dataset_id); await loadPool(res.dataset.dataset_id) }
      const got = res.companies.filter(c => c.status === 'collected').length
      setPrepMsg(res.dataset
        ? `已取到 ${got} 家的行并生成数据集：只物化成功公司的行，未成功的不冒充；数据集已切为当前来源。`
        : `没有生成数据集：${res.note || '先看逐公司状态'}`)
    } catch (e: any) {
      setPrepBusy(''); setPrepMsg(`取数失败：${e?.message ?? e}`); return
    } finally { setPrepBusy('') }
  }

  const toggleCost = () => {
    const next = !costOpen
    setCostOpen(next)
    if (next) loadCost()
  }

  // 证据跳转：绑 (run_id, entity_id)，打开的是这一份结果里的证据
  const toggleEvidence = async (row: ScreenRow) => {
    if (!run) return
    if (openEvidence === row.entity_id) { setOpenEvidence(''); setEvidence(null); return }
    setOpenEvidence(row.entity_id); setEvLoading(true); setEvError('')
    try {
      const resp = await fetchJSON(
        `/api/screen/evidence?run=${encodeURIComponent(run.run_id)}&entity=${encodeURIComponent(row.entity_id)}`)
      const d = resp.d
      if (!resp.ok) throw new Error(d.error || '证据读取失败')
      setEvidence(d.evidence as Evidence)
    } catch (e: any) { setEvError(String(e?.message ?? e)); setEvidence(null) } finally { setEvLoading(false) }
  }

  const sourceLine = (s: SourceMeta | null | undefined) => {
    if (!s) return null
    const prov = s.provenance
    return (
      <span className="text-xs text-ink-faint">
        数据来源：{s.label || '未标'}｜{s.row_count} 行｜
        {s.synthetic ? <span className="text-state-issue">合成演示数据（不进真实候选）</span>
          : s.user_provided ? <span className="text-state-issue">用户提供（未独立核验）</span>
          : s.official ? <span className="text-state-success">{prov?.kind_label || '服务端整理的宽表'}</span>
          : '来源未标注'}｜文件指纹 {short12(s.sha256)}…
        {prov && (
          <span>｜核验：{prov.human_review?.status === 'not_reviewed' || !prov.human_review
            ? <span className="text-ink-faint">人工复核未开始（接口只显示状态，不代签）</span>
            : <span className="text-state-success">人工复核已登记</span>}</span>
        )}
      </span>
    )
  }

  const stepDot = (n: number, done: boolean) => (
    <span className={`inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-semibold shrink-0
      ${done ? 'bg-brand text-white' : 'bg-surface-raised text-ink-faint border border-line'}`}>{n}</span>
  )

  const renderRuleRow = (r: RuleDraft, i: number) => {
    const spec = fieldById.get(r.field)
    const isAmount = spec?.kind === 'amount'
    return (
      <div key={i} className="grid grid-cols-1 sm:grid-cols-[1.3fr_.55fr_.7fr_.6fr_auto] gap-2 items-start
        border border-line rounded-lg p-2 bg-surface-sunken/60">
        <label className="text-xs text-ink-faint block">
          字段
          <select value={r.field} onChange={e => {
            const f = fieldById.get(e.target.value)
            updateRule(i, { field: e.target.value, unit: f?.kind === 'amount' ? '元' : '' })
          }}
            className="mt-1 w-full rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink">
            <option value="">选择字段…</option>
            {(fields?.fields ?? []).map(f => <option key={f.field_id} value={f.field_id}>{f.label}</option>)}
          </select>
          {spec && <span className="mt-1 block leading-snug">{spec.note}</span>}
        </label>
        <label className="text-xs text-ink-faint block">
          比较
          <select value={r.op} onChange={e => updateRule(i, { op: e.target.value })}
            className="mt-1 w-full rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink">
            {(fields?.ops ?? ['>=', '<=', '>', '<']).map(op =>
              <option key={op} value={op}>{OP_LABEL[op] || op}</option>)}
          </select>
        </label>
        <label className="text-xs text-ink-faint block">
          阈值
          <input type="number" step="any" value={r.value}
            onChange={e => updateRule(i, { value: e.target.value })}
            placeholder="数字"
            className="mt-1 w-full rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink tabular-nums" />
        </label>
        <label className="text-xs text-ink-faint block">
          单位
          <select value={isAmount ? (r.unit || '元') : (spec?.unit || '%')} disabled={!isAmount}
            onChange={e => updateRule(i, { unit: e.target.value })}
            className="mt-1 w-full rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink disabled:text-ink-faint disabled:bg-surface-raised/50">
            {isAmount
              ? (fields?.amount_units ?? ['元', '万', '亿']).map(u => <option key={u} value={u}>{u}</option>)
              : <option value="%">%</option>}
          </select>
        </label>
        <button type="button" onClick={() => removeRule(i)} aria-label={`删除条件 ${i + 1}`}
          className="mt-5 p-1.5 rounded-lg text-ink-faint hover:text-red-700 hover:bg-red-500/10 transition-colors">
          <X className="w-4 h-4" />
        </button>
      </div>
    )
  }

  const renderRow = (row: ScreenRow) => {
    const open = openEvidence === row.entity_id
    return (
      <div key={row.entity_id} className="border border-line rounded-xl overflow-hidden bg-surface">
        <button type="button" onClick={() => toggleEvidence(row)} aria-expanded={open}
          className="w-full flex flex-wrap items-center gap-2 px-4 py-3 text-left hover:bg-surface-raised/40 transition-colors">
          {open ? <ChevronDown className="w-4 h-4 text-brand shrink-0" />
            : <ChevronRight className="w-4 h-4 text-ink-faint shrink-0" />}
          <span className="text-sm font-medium text-ink">{row.entity}</span>
          <span className="text-xs text-ink-faint tabular-nums">{row.entity_id}</span>
          {verdictBadge(row.verdict)}
          <span className="text-xs text-ink-faint">评估期间 {row.eval_period || '—'}</span>
          <span className="text-xs text-ink-faint ml-auto hidden sm:inline">底稿哈希 {short12(row.dataset_hash)}…</span>
        </button>
        <div className="px-4 pb-3">
          <div className="flex flex-wrap items-center gap-2 pb-2">
            <button type="button" onClick={() => toggleWatch(row.entity_id, row.entity)}
              disabled={watchBusy === row.entity_id || readOnly}
              title={readOnly ? '只读账号不能维护研究清单' : '加入/移出个人研究清单（观察对象，不是持仓）'}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-line-strong
                text-xs text-ink-muted hover:text-brand hover:border-brand/40 transition-colors disabled:opacity-50">
              {watchHas(row.entity_id)
                ? <><CheckCircle2 className="w-3.5 h-3.5 text-state-success" /> 已在清单</>
                : <><Plus className="w-3.5 h-3.5" /> 加入清单</>}
            </button>
            <Link to={`/screen/company?code=${encodeURIComponent(row.entity_id)}` +
                `&run=${encodeURIComponent(run?.run_id || '')}`}
              title="打开公司页：绑定这一份冻结结果（不跳「最近任务」）"
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-line-strong
                text-xs text-ink-muted hover:text-brand hover:border-brand/40 transition-colors">
              <Eye className="w-3.5 h-3.5" /> 看公司
            </Link>
            {watchHas(row.entity_id) && (
              <span className="text-xs text-ink-faint">清单在下方可写备注；观察对象，不是持仓</span>
            )}
          </div>
          <div className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
            {row.readings.map(r => {
              const st = readingStatus(r.status)
              return (
                <div key={r.field} className="border border-line rounded-lg px-3 py-2 bg-surface-sunken/60">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs text-ink-muted">{r.label}</span>
                    <span className={`text-xs font-medium ${st.cls}`}>{st.label}</span>
                  </div>
                  <div className="text-sm text-ink tabular-nums mt-0.5" title={r.value != null ? String(r.value) : undefined}>
                    {r.display || '—'}
                  </div>
                  {r.reason && <div className="text-xs text-ink-faint mt-1 leading-snug">{r.reason}</div>}
                  {r.formula && <div className="text-xs text-ink-faint/80 mt-0.5 tabular-nums">{r.formula}</div>}
                </div>
              )
            })}
          </div>
          {row.reasons.length > 0 && (
            <ul className="mt-2 space-y-1">
              {row.reasons.map((reason, i) => (
                <li key={i} className="text-xs text-ink-muted flex gap-1.5 leading-snug">
                  {row.verdict === 'pass'
                    ? <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 shrink-0 text-state-success" />
                    : <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0 text-state-issue" />}
                  {reason}
                </li>
              ))}
            </ul>
          )}
          {open && (
            <div className="mt-3 border-t border-line pt-3" aria-live="polite">
              <CompanyTrend entities={run?.entities} entityId={row.entity_id} />
              {evLoading && <div className="flex items-center gap-2 text-xs text-ink-faint py-2">
                <Loader2 className="w-4 h-4 animate-spin" /> 正在载入证据…</div>}
              {evError && <Alert tone="danger" title="证据载入失败">{evError}</Alert>}
              {evidence && evidence.entity_id === row.entity_id && <EvidencePanel ev={evidence} />}
            </div>
          )}
        </div>
      </div>
    )
  }

  const rows = run?.rows
  const noRun = !run

  return (
    <div className="max-w-5xl mx-auto p-4 md:p-6 space-y-4" id="workspace-content">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink flex items-center gap-2">
            <Filter className="w-5 h-5 text-brand" /> 可解释选股
          </h1>
          <p className="text-xs text-ink-faint mt-1 leading-relaxed">
            选自己的股票池和资料截止日 → 确认少量财务规则 → 看带理由与缺口的候选 → 查看对应公司证据 → 结果冻结保存，可重开同一份原版。
            不做回测，不给综合评分；筛选不调用模型。
          </p>
        </div>
        <div className="sm:ml-auto">{sourceLine(source)}</div>
      </div>

      {poolError && <Alert tone="danger" title="股票池目录不可用" >{poolError}</Alert>}
      {err && <Alert tone="danger" title="操作未完成">{err}</Alert>}
      {notice && <Alert tone="info">{notice}</Alert>}
      {readOnly && <Alert tone="info" title="只读账号：可看、可比、不可执行">
        {cap?.read_only_reason}
      </Alert>}

      {/* ── 第 1 步：股票池 + 截止日 ── */}
      <Card>
        <SectionTitle icon={stepDot(1, effectivePool.length > 0 && !!preview)}
          title="股票池与资料截止日"
          extra="先预览输入里真实可用的资料，再确认规则" />
        <div className="space-y-3">
          <div className="rounded-lg border border-line bg-surface-raised/40 p-3">
            <div className="flex flex-col sm:flex-row sm:flex-wrap gap-2 sm:items-end">
              <label className="text-xs text-ink-faint">
                数据来源
                <select value={datasetId} onChange={e => changeSource(e.target.value)}
                  className="mt-1 block w-64 rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink">
                  <option value="">
                    {officialSource?.label || '官方披露缓存'}
                    {officialSource ? `（${officialSource.row_count} 行）` : ''}
                  </option>
                  {datasets.map(ds => (
                    <option key={ds.dataset_id} value={ds.dataset_id}>
                      {ds.usable === false ? '（不可读）'
                        : (ds.official || ds.provenance_kind === 'official_cache_materialized')
                          ? `官方缓存物化：${ds.name}`
                          : ds.provenance_kind === 'research_material'
                            ? `研究材料物化：${ds.name}`
                            : `我上传：${ds.name}`}
                      {ds.row_count != null ? `（${ds.row_count} 行）` : ''}
                    </option>
                  ))}
                </select>
              </label>
              <button type="button" onClick={downloadTemplate}
                className="px-2.5 py-1.5 rounded-lg border border-line text-ink-muted hover:bg-surface-raised text-xs transition-colors">
                下载模板（CSV）
              </button>
              <label className={`px-2.5 py-1.5 rounded-lg border border-line text-xs transition-colors ${readOnly ? 'text-ink-faint/60 cursor-not-allowed' : 'text-ink-muted hover:bg-surface-raised cursor-pointer'}`}
                title={readOnly ? '只读账号不能上传数据' : undefined}>
                {uploadBusy === 'upload' ? '上传中…' : '上传 CSV 数据'}
                <input type="file" accept=".csv,text/csv" className="hidden" disabled={readOnly}
                  onChange={e => onUploadFile(e.target.files?.[0] ?? null, e.currentTarget)} />
              </label>
            </div>
            <details className="mt-2 rounded-lg border border-line bg-surface-raised/40 px-3 py-2">
              <summary className="text-xs text-ink-muted cursor-pointer select-none">
                字段说明（中文）：一列填什么、缺了会怎样
              </summary>
              <div className="mt-2 space-y-1">
                {CSV_FIELDS.map(f => (
                  <div key={f.name} className="flex gap-2 text-xs leading-snug">
                    <code className="w-40 shrink-0 text-ink tabular-nums">{f.name}</code>
                    <span className="w-8 shrink-0">
                      {f.required ? <span className="text-state-issue">必填</span> : <span className="text-ink-faint">可选</span>}
                    </span>
                    <span className="text-ink-muted flex-1">
                      {f.meaning}{f.note ? <span className="text-ink-faint">（{f.note}）</span> : null}
                    </span>
                  </div>
                ))}
                <p className="pt-1 text-xs text-ink-faint leading-snug">
                  缺字段不等于 0：算不出的条件会记「资料不足」并说明缺哪几项，不会替你补数。
                  模板里的两行是示例，上传前请删掉。
                </p>
              </div>
            </details>
            {officialSource && officialSource.usable === false && (
              <p className="mt-2 text-xs text-state-issue leading-snug">
                官方缓存不可用：{(officialSource.errors || []).join('；') || '服务端未配置数据'}
                （可以下载模板用 CSV 上传自己的数据，或等管理员配置缓存）
              </p>
            )}
            <p className="mt-2 text-xs text-ink-faint leading-snug">
              {datasetId
                ? <>当前来源：我上传的数据集（<span className="text-state-issue">用户提供，未独立核验；不是官方数据</span>）。可用主体来自这份文件，一次导入可反复筛选。</>
                : <>当前来源：官方披露缓存（由服务端配置）。想用自己的数据：下载模板 → 填好 → 上传，上传不联网、不调用模型。</>}
              {officialSource?.synthetic && !datasetId
                ? <span className="text-state-issue">　当前缓存是合成演示数据，不进真实候选。</span> : null}
            </p>
            {uploadMsg && <p className="mt-1 text-xs text-ink-muted leading-snug">{uploadMsg}</p>}

            {/* ── C1：数据准备（先确认材料与缺口，再决定取不取数）── */}
            <details className="mt-2 rounded-lg border border-line bg-surface/60 px-3 py-2">
              <summary className="text-xs text-ink-muted cursor-pointer select-none">
                数据准备：我已有材料 / 缺哪几格 / 从哪来的（在已有数据里搜索 ≠ 取数）
              </summary>
              <div className="mt-2 space-y-2">
                <p className="text-xs text-ink-muted leading-snug">
                  {prepMaterials ? (<>
                    官方披露缓存：
                    {prepMaterials.official_cache.available
                      ? `${prepMaterials.official_cache.row_count ?? 0} 行 / ${(prepMaterials.official_cache.codes ?? []).length} 家`
                      : <span className="text-state-issue">不可用</span>}
                    {(prepMaterials.research?.status === 'ok')
                      ? `；研究任务材料 ${prepMaterials.research.tasks?.length ?? 0} 项（已准入事实/底稿）`
                      : (prepMaterials.research?.note
                        ? `；研究任务材料：${prepMaterials.research.note}` : '')}
                    ；我的数据集 {prepMaterials.datasets.length} 份
                    {prepMaterials.datasets.some(d => d.source === 'official_cache')
                      ? `（其中 ${prepMaterials.datasets.filter(d => d.source === 'official_cache').length} 份由官方缓存物化）`
                      : ''}；
                    研究清单 {prepMaterials.watchlist.items.length} 只
                    {prepMaterials.watchlist.missing_material.length > 0
                      ? <span className="text-state-issue">（其中 {prepMaterials.watchlist.missing_material.length} 只还没有任何材料）</span>
                      : null}
                  </>) : '材料清单读取中…'}
                </p>
                <div className="flex flex-col sm:flex-row sm:flex-wrap gap-2 sm:items-end">
                  <label className="text-xs text-ink-faint">
                    取数来源
                    <select value={prepSource} onChange={e => setPrepSource(e.target.value)}
                      className="mt-1 block w-56 rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink">
                      {prepSources.map(x => (
                        <option key={x.id} value={x.id}>
                          {x.label}{x.requires_network ? '（联网）' : '（本地）'}
                        </option>
                      ))}
                      {prepSources.length === 0 && <option value="official_cache">官方披露缓存（本地）</option>}
                    </select>
                  </label>
                  <label className="text-xs text-ink-faint">
                    期间（年，逗号分隔）
                    <input value={prepPeriods} onChange={e => setPrepPeriods(e.target.value)}
                      placeholder="2023,2024"
                      className="mt-1 block w-32 rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink tabular-nums" />
                  </label>
                  <button type="button" onClick={runPrepPlan} disabled={!!prepBusy}
                    className="px-2.5 py-1.5 rounded-lg border border-line text-xs text-ink-muted hover:bg-surface-raised disabled:opacity-50 transition-colors">
                    {prepBusy === 'plan' ? '预览中…' : '预览覆盖与缺口'}
                  </button>
                  <button type="button" onClick={() => runPrepCollect(true)} disabled={!!prepBusy}
                    className="px-2.5 py-1.5 rounded-lg border border-line text-xs text-ink-muted hover:bg-surface-raised disabled:opacity-50 transition-colors"
                    title="只试第一家：先看这家能不能取到，再决定整批">
                    {prepBusy === 'probe' ? '试取中…' : '先试一家'}
                  </button>
                  <button type="button" onClick={() => runPrepCollect(false)} disabled={!!prepBusy}
                    className="px-2.5 py-1.5 rounded-lg bg-accent/15 border border-accent/30 text-xs text-ink hover:bg-accent/25 disabled:opacity-50 transition-colors">
                    {prepBusy === 'collect' ? '取数中…' : '整批取数并生成数据集'}
                  </button>
                </div>
                <p className="text-xs text-ink-faint leading-snug">
                  对象范围：当前勾选的股票池 + 研究清单（共 {prepCodes().length} 只）。
                  只按「来源 ID + 公司 + 期间」取数，不接受客户端给的网址或文件路径；
                  相同来源同一家被策略拒绝一次就停批，不逐个硬试。
                </p>
                {prepMsg && <p className="text-xs text-ink-muted leading-snug">{prepMsg}</p>}

                {prepPlan && (
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs tabular-nums">
                      <thead>
                        <tr className="text-ink-faint">
                          <th className="text-left font-normal py-1 pr-3">公司</th>
                          {(prepPlan.items[0]?.periods ?? []).map(pt => (
                            <th key={pt.period} className="text-left font-normal py-1 pr-3">{pt.period}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {prepPlan.items.map(it => (
                          <tr key={it.code} className="border-t border-line/60 text-ink">
                            <td className="py-1 pr-3 whitespace-nowrap">
                              {it.code}<span className="text-ink-faint"> {it.entity ?? ''}</span>
                            </td>
                            {it.periods.map(pt => (
                              <td key={pt.period} className="py-1 pr-3 whitespace-nowrap">
                                {pt.status === 'covered'
                                  ? <span className="text-emerald-700">五字段可全算</span>
                                  : pt.status === 'partial'
                                    ? <span className="text-amber-700">部分可算</span>
                                    : pt.status === 'material_only'
                                      ? <span className="text-amber-700">有材料/算不出</span>
                                      : <span className="text-ink-faint">缺材料</span>}
                                {pt.fields
                                  ? <span className="text-ink-faint">
                                      {Object.entries(pt.fields)
                                        .filter(([, v]) => v.status !== 'ok')
                                        .map(([k]) => k)
                                        .join('/')}
                                    </span>
                                  : null}
                                {pt.via ? <span className="text-ink-faint"> · {pt.via}</span> : null}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <p className="mt-1 text-xs text-ink-faint leading-snug">
                      五字段可全算 {prepPlan.totals.covered} 格、部分可算 {prepPlan.totals.partial ?? 0} 格、
                      有材料但算不出 {prepPlan.totals.material_only ?? 0} 格、缺材料 {prepPlan.totals.missing} 格。
                      {prepPlan.errors && prepPlan.errors.length > 0
                        ? <span className="text-state-issue">输入有误：{prepPlan.errors.join('；')}</span>
                        : null}
                      {prepPlan.reading}
                    </p>
                  </div>
                )}

                {prepResult && (
                  <div className="rounded-lg border border-line bg-surface-raised/40 p-2 space-y-1">
                    <p className="text-xs text-ink leading-snug">
                      结果：{prepResult.status}
                      {prepResult.dataset
                        ? `；数据集「${prepResult.dataset.name}」${prepResult.dataset.row_count ?? 0} 行，已切为当前来源`
                        : '；未生成数据集'}
                    </p>
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs tabular-nums">
                        <thead><tr className="text-ink-faint">
                          <th className="text-left font-normal py-0.5 pr-3">公司</th>
                          <th className="text-left font-normal py-0.5 pr-3">状态</th>
                          <th className="text-left font-normal py-0.5 pr-3">行</th>
                          <th className="text-left font-normal py-0.5">说明</th>
                        </tr></thead>
                        <tbody>
                          {prepResult.companies.map(c => (
                            <tr key={c.code} className="border-t border-line/60 text-ink">
                              <td className="py-0.5 pr-3 whitespace-nowrap">{c.code}</td>
                              <td className="py-0.5 pr-3 whitespace-nowrap">
                                {c.status === 'collected' ? <span className="text-emerald-700">已取到</span>
                                  : c.status === 'denied_by_policy' ? <span className="text-state-issue">被策略拒绝</span>
                                    : c.status === 'skipped_policy_blocked' ? <span className="text-ink-faint">未尝试（前一家被拒已停批）</span>
                                      : c.status === 'missing' ? <span className="text-amber-700">来源里没有</span>
                                        : c.status === 'not_attempted' ? <span className="text-ink-faint">未尝试</span>
                                          : <span className="text-amber-700">{c.status}</span>}
                              </td>
                              <td className="py-0.5 pr-3">{c.rows ?? '—'}</td>
                              <td className="py-0.5 text-ink-faint">{c.detail ?? c.note ?? c.category ?? ''}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    {prepResult.counters && (
                      <p className="text-xs text-ink-faint leading-snug">
                        适配器调用 {prepResult.counters.adapter_calls ?? prepResult.counters.network_attempts ?? 0} 次、
                        成功 {prepResult.counters.network_ok ?? 0}、失败 {prepResult.counters.network_failed ?? 0}、
                        被策略拒绝 {prepResult.counters.network_denied ?? 0}（未真正连接）；
                        真实 HTTP 请求数 {prepResult.counters.network_requests === null
                          || prepResult.counters.network_requests === undefined
                          ? '未知' : prepResult.counters.network_requests} 次
                        {prepResult.counters.network_requests_note
                          ? `（${prepResult.counters.network_requests_note}）` : ''}。
                      </p>
                    )}
                    {prepResult.metering && !prepResult.metering.recorded && (
                      <p className="text-xs text-state-issue leading-snug">
                        注意：业务结果已返回，但成本计量未记录（
                        {prepResult.metering.recording?.reason ?? 'unknown'}）；
                        账本修复前不会继续累加，也不会覆盖原账本。
                      </p>
                    )}
                    {prepResult.note && <p className="text-xs text-ink-faint leading-snug">{prepResult.note}</p>}
                  </div>
                )}

                <details className="rounded-lg border border-line bg-surface-raised/40 px-2 py-1.5">
                  <summary className="text-xs text-ink-muted cursor-pointer select-none">
                    数据来源表：谁提供、要不要联网、授权与费用（不知道就写未知）
                  </summary>
                  <div className="mt-1.5 overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead><tr className="text-ink-faint">
                        <th className="text-left font-normal py-1 pr-3">来源</th>
                        <th className="text-left font-normal py-1 pr-3">类型</th>
                        <th className="text-left font-normal py-1 pr-3">联网</th>
                        <th className="text-left font-normal py-1 pr-3">授权</th>
                        <th className="text-left font-normal py-1 pr-3">费用</th>
                        <th className="text-left font-normal py-1">说明</th>
                      </tr></thead>
                      <tbody>
                        {prepSources.map(x => (
                          <tr key={x.id} className="border-t border-line/60 text-ink">
                            <td className="py-1 pr-3 whitespace-nowrap">{x.label}</td>
                            <td className="py-1 pr-3 whitespace-nowrap text-ink-muted">{x.kind}</td>
                            <td className="py-1 pr-3 whitespace-nowrap">{x.requires_network ? '要' : '不要'}</td>
                            <td className="py-1 pr-3 whitespace-nowrap text-ink-muted">{x.licence}</td>
                            <td className="py-1 pr-3 whitespace-nowrap text-ink-muted">{x.fee}</td>
                            <td className="py-1 text-ink-faint leading-snug">{x.note ?? x.available_note ?? ''}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              </div>
            </details>
          </div>
          <div>
            <div className="flex flex-col sm:flex-row sm:items-center gap-2 mb-1.5">
              <div className="text-xs text-ink-faint flex-1">
                勾选公司（来自所选数据来源的目录；不在目录里的代码可手输，执行后计入「资料不足」，不编造读数）
              </div>
              {pool.length > 0 && (
                <div className="flex items-center gap-1.5">
                  <span className="text-xs text-ink-faint tabular-nums">已选 {selected.length} / {pool.length}</span>
                  <input value={poolQuery} onChange={e => setPoolQuery(e.target.value)}
                    placeholder="按名称或代码找公司"
                    className="w-36 bg-surface-raised border border-line-strong rounded-lg px-2 py-1 text-xs text-ink" />
                  <button type="button" onClick={() => setSelected(pool.map(p => p.code))}
                    className="px-2 py-1 rounded-lg border border-line text-xs text-ink-muted hover:bg-surface-raised">全选</button>
                  <button type="button" onClick={() => setSelected([])}
                    className="px-2 py-1 rounded-lg border border-line text-xs text-ink-muted hover:bg-surface-raised">清空</button>
                </div>
              )}
            </div>
            {pool.length === 0
              ? <EmptyState title="这个数据来源里还没有可用主体"
                  description={datasetId
                    ? '这份上传数据里没有可用主体：检查 code 是否是 A 股代码（如 600519.SH）、每行是否有金额字段。'
                    : '管理员可把披露缓存放到 data/screen_cache/official_disclosure_cache.json；或用上方「下载模板」上传你自己的数据。'} />
              : (() => {
                  const q = poolQuery.trim().toLowerCase()
                  const shown = q ? pool.filter(p => (p.entity || '').toLowerCase().includes(q) || (p.code || '').toLowerCase().includes(q)) : pool
                  if (shown.length === 0) {
                    return <p className="text-xs text-ink-muted leading-snug">没有匹配「{poolQuery}」的公司：目录里共 {pool.length} 家，可清空搜索或手输代码。</p>
                  }
                  return <div className="grid gap-2 sm:grid-cols-2">
                  {shown.map(p => (
                    <label key={p.code}
                      className={`flex items-start gap-2.5 rounded-lg border px-3 py-2 cursor-pointer transition-colors
                        ${selected.includes(p.code) ? 'border-brand bg-brand-soft/60' : 'border-line hover:bg-surface-raised/50'}`}>
                      <input type="checkbox" checked={selected.includes(p.code)}
                        onChange={() => toggleCode(p.code)} className="mt-0.5 accent-[#876728]" />
                      <span className="min-w-0">
                        <span className="text-sm text-ink">{p.entity}</span>
                        <span className="text-xs text-ink-faint tabular-nums ml-1.5">{p.code}</span>
                        <span className="block text-xs text-ink-faint mt-0.5 leading-snug">
                          {p.rows} 行 · {p.periods.length} 个年度期间 · 可用时点 {p.first_available || '—'} ~ {p.last_available || '—'}
                          {p.pending_rows > 0 && <span className="text-state-issue"> · {p.pending_rows} 行待核验</span>}
                        </span>
                      </span>
                    </label>
                  ))}
                </div> })()}
          </div>
          <div className="flex flex-col sm:flex-row gap-2 sm:items-end">
            <label className="text-xs text-ink-faint">
              手输代码（不在目录也允许）
              <div className="flex gap-1.5 mt-1">
                <input value={manualCode} onChange={e => setManualCode(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addManual() } }}
                  placeholder="如 600519.SH"
                  className="w-44 rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink tabular-nums" />
                <button type="button" onClick={addManual}
                  className="px-2.5 rounded-lg border border-line text-ink-muted hover:bg-surface-raised text-xs transition-colors">
                  添加
                </button>
              </div>
            </label>
            <label className="text-xs text-ink-faint sm:ml-auto">
              资料截止日
              <input type="date" value={asOf} onChange={e => setAsOf(e.target.value)}
                className="mt-1 block rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink tabular-nums" />
              <span className="mt-1 block leading-snug">
                只纳入「可用时点 ≤ 截止日」的资料；缺可用时点 → 待核验（不参与比较）。
              </span>
            </label>
          </div>
          <div className="border-t border-line pt-3 space-y-2">
            <div className="text-xs text-ink-faint">
              期间政策（决定「拿哪一年的数比较」；缺期或冲突的公司列为资料不足，**不会**自动换到更早年度）：
            </div>
            <div className="flex flex-col sm:flex-row gap-2 sm:items-end">
              <label className="text-xs text-ink-faint">
                比较口径
                <select value={periodMode} onChange={e => setPeriodMode(e.target.value as any)}
                  className="mt-1 block w-56 rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink">
                  {(fields?.period_modes ?? [
                    { mode: 'uniform', label: '统一报告期（池内最新可用年度）', comparable: true },
                    { mode: 'pinned', label: '指定年度', comparable: true },
                    { mode: 'latest_each', label: '逐家最新（不可横比）', comparable: false },
                  ]).map(m => <option key={m.mode} value={m.mode}>{m.label}</option>)}
                </select>
              </label>
              <label className="text-xs text-ink-faint">
                指定年度
                <input value={periodPin} onChange={e => setPeriodPin(e.target.value)}
                  disabled={periodMode !== 'pinned'} placeholder="如 2024"
                  className="mt-1 block w-32 rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink tabular-nums
                    disabled:text-ink-faint disabled:bg-surface-raised/50" />
              </label>
              {preview && (
                <span className="text-xs text-ink-faint leading-snug sm:max-w-xs">
                  默认统一报告期（池内最新可用年度）：{preview.suggested_report_period || '无法确定'}
                  {'　·　共同年度：'}
                  {(preview.common_periods ?? []).join('、') || '无'}
                </span>
              )}
            </div>
          </div>
          <div className="border-t border-line pt-3 space-y-2">
            <div className="text-xs text-ink-faint">
              预设（股票池 + 规则 + 数据来源）：保存后下次直接载入；预设只是偏好，不改变任何已冻结结果。
            </div>
            <div className="flex flex-col sm:flex-row gap-2 sm:items-end">
              <label className="text-xs text-ink-faint">
                已存预设
                <select value={presetSel} onChange={e => applyPreset(e.target.value)}
                  disabled={presetBusy !== ''}
                  className="mt-1 block w-64 rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink">
                  <option value="">（不载入）</option>
                  {presets.map(p => (
                    <option key={p.preset_id} value={p.preset_id}>
                      {p.name} · {p.pool.length} 只 · {p.rules.length} 条{p.dataset_id ? ' · 含数据来源' : ''}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-xs text-ink-faint">
                预设名称
                <input value={presetName} onChange={e => setPresetName(e.target.value)}
                  placeholder="如 高毛利低负债"
                  className="mt-1 block w-48 rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink" />
              </label>
              <div className="flex gap-1.5">
                <button type="button" onClick={savePreset} disabled={presetBusy !== '' || readOnly}
                  title={readOnly ? '只读账号不能保存预设' : undefined}
                  className="px-2.5 py-1.5 rounded-lg border border-brand/40 text-brand-strong hover:bg-brand-soft
                    text-xs transition-colors disabled:opacity-50">
                  {presetBusy === 'save' ? '保存中…' : presetSel ? '更新预设' : '保存为预设'}
                </button>
                {presetSel && (
                  <button type="button" onClick={removePreset} disabled={presetBusy !== '' || readOnly}
                    className="px-2.5 py-1.5 rounded-lg border border-line text-ink-muted hover:text-red-700
                      text-xs transition-colors disabled:opacity-50">
                    删除预设
                  </button>
                )}
              </div>
            </div>
            {presetMsg && <div className="text-xs text-ink-muted">{presetMsg}</div>}
          </div>
          {extraCodes.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {extraCodes.map(code => (
                <span key={code} className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border
                  ${inCatalog(code) ? 'border-brand/40 text-brand bg-brand-soft/60'
                    : 'border-amber-500/30 text-amber-700 bg-amber-500/10'}`}>
                  {code}
                  {inCatalog(code) ? '' : <span className="text-ink-faint">（目录里没有）</span>}
                  <button type="button" onClick={() => setExtraCodes(prev => prev.filter(c => c !== code))}
                    aria-label={`移除 ${code}`} className="ml-0.5 hover:text-red-700"><X className="w-3 h-3" /></button>
                </span>
              ))}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" onClick={doPreview} disabled={busy === 'preview' || readOnly}
              title={readOnly ? '只读账号不能预览/执行' : undefined}
              className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg border border-brand/40 text-brand-strong
                hover:bg-brand-soft text-sm transition-colors disabled:opacity-50">
              {busy === 'preview' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Eye className="w-4 h-4" />}
              预览输入
            </button>
            {preview && <span className="text-xs text-ink-faint">
              快照 {short12(preview.snapshot_id)} · 可用主体 {preview.entities_ready ?? 0} · PIT 排除 {(preview.excluded ?? []).length} 行 · 待核验 {(preview.pending ?? []).length} 行
            </span>}
          </div>
          {preview && <PreviewPanel p={preview} />}
        </div>
      </Card>

      {/* ── 第 2 步：规则 ── */}
      <Card>
        <SectionTitle icon={stepDot(2, activeRules.length > 0 && activeRules.every(r => r.field && r.value !== ''))}
          title="筛选规则（少量、可解释）"
          extra="未知字段/比较符会被直接拒绝，不自动替换为相近字段" />
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-xs text-ink-faint">示例（点一下填入，值可改）：</span>
            {RULE_EXAMPLES.map(ex => (
              <button key={ex.name} type="button"
                onClick={() => setRules(ex.rules.map(r => ({ ...r })))}
                title={`${ex.desc}（填入后仍可逐条改值/删条件）`}
                className="px-2 py-1 rounded-full border border-line text-xs text-ink-muted hover:border-brand/40 hover:text-brand-strong transition-colors">
                {ex.name}
              </button>
            ))}
          </div>
          {rules.map(renderRuleRow)}
          <button type="button" onClick={addRule}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-dashed border-line-strong
              text-ink-muted hover:text-brand hover:border-brand/40 text-xs transition-colors">
            <Plus className="w-3.5 h-3.5" /> 添加条件
          </button>
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button type="button" onClick={doRun} disabled={busy === 'run' || readOnly}
              title={readOnly ? '只读账号不能执行筛选' : undefined}
              className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-brand text-white hover:bg-brand-strong
                text-sm font-medium transition-colors disabled:opacity-50">
              {busy === 'run' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              执行筛选
            </button>
            <span className="text-xs text-ink-faint">
              执行会生成一份冻结结果（run_id + 结果哈希）；同一输入/规则重复执行会复用，不重算。
            </span>
          </div>

          {/* ── B3：规则模板只含条件/期间策略/版本；导入先校验+预览，不自动执行 ── */}
          <div className="pt-3 mt-1 border-t border-line space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-ink-muted">规则模板（可分享，不含股票池/备注/结果）：</span>
              <button type="button" onClick={exportTemplate} disabled={readOnly || activeRules.length === 0}
                title={readOnly ? '只读账号不能导出' : (activeRules.length === 0 ? '先填一条完整条件' : undefined)}
                className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-line-strong text-xs
                  text-ink-muted hover:text-brand hover:border-brand/40 transition-colors disabled:opacity-50">
                导出模板
              </button>
              <button type="button" onClick={() => tplFile.current?.click()} disabled={readOnly}
                title={readOnly ? '只读账号不能导入' : undefined}
                className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-line-strong text-xs
                  text-ink-muted hover:text-brand hover:border-brand/40 transition-colors disabled:opacity-50">
                导入模板
              </button>
              <input ref={tplFile} type="file" accept="application/json,.json" className="hidden"
                onChange={e => {
                  const f = e.target.files?.[0]
                  if (f) importTemplate(f)
                  e.currentTarget.value = ''
                }} />
              <span className="text-xs text-ink-faint">导入只校验+预览，点「应用」才填表单，不会执行</span>
            </div>
            {tplErr && <Alert tone="danger" title="模板未通过">{tplErr}</Alert>}
            {tplMsg && <div className="text-xs text-ink-muted">{tplMsg}</div>}
            {tplPreview && (
              <div className="border border-line rounded-lg p-3 bg-surface-sunken/60 space-y-1.5">
                <div className="text-xs text-ink-muted">
                  预览：{tplPreview.rules.length} 条条件 · 期间策略 {tplPreview.period_policy?.mode || 'uniform'}
                  {tplPreview.period_policy?.report_period ? ` ${tplPreview.period_policy.report_period}` : ''}
                </div>
                <ul className="text-xs text-ink-muted space-y-0.5">
                  {tplPreview.rules.map((r, i) => (
                    <li key={i} className="tabular-nums">
                      {fieldById.get(r.field)?.label || r.field} {r.op} {r.value}{r.unit ? ` ${r.unit}` : ''}
                    </li>
                  ))}
                </ul>
                {(tplPreview.warnings ?? []).map((w, i) => (
                  <div key={i} className="text-xs text-state-issue">{w}</div>
                ))}
                <button type="button" onClick={applyTemplate}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-brand text-white
                    hover:bg-brand-strong text-xs font-medium transition-colors">
                  应用这些条件（不执行）
                </button>
              </div>
            )}
          </div>
        </div>
      </Card>

      {/* ── 第 3 步：结果 ── */}
      <Card>
        <SectionTitle icon={stepDot(3, !!run)}
          title="筛选结果（三态候选，带理由与缺口）"
          extra={run ? `截止日 ${run.as_of} · run_id ${run.run_id} · ${fmtTime(run.created_at)}` : '执行后显示'} />
        {noRun ? (
          <EmptyState icon={<FlaskConical className="w-5 h-5" />}
            title="还没有筛选结果"
            description="确认股票池与规则后点「执行筛选」。结果会冻结保存，可随时重开同一份原版。" />
        ) : (
          <div className="space-y-4">
            {viewingOriginal && (
              <Alert tone="info" title="正在查看原版结果">
                这份结果冻结于 {fmtTime(run.created_at)}，与其输入/规则/实现哈希绑定，永不重算。
                要看当前数据下的最新筛选，请回第 2 步点「执行筛选」（会生成新结果，不覆盖这一份）。
              </Alert>
            )}
            {staleReasons.length > 0 && (
              <Alert tone="warn" title="这份原版与当前实现/输入不一致（只能查看，不冒充现状）">
                <ul className="list-disc pl-4 space-y-0.5">
                  {staleReasons.map((x, i) => <li key={i}>{x}</li>)}
                </ul>
              </Alert>
            )}
            {run.source?.synthetic && (
              <Alert tone="warn" title="本次结果基于合成演示数据">
                不进入真实候选、不进入交付与验收证据；真实结果请使用真实披露缓存。
              </Alert>
            )}
            {run.source?.user_provided && (
              <Alert tone="warn" title="本次结果包含用户提供的数据">
                用户提供数据按「未独立核验」处理；证据面板会标注 verify_state。
              </Alert>
            )}
            <div className="flex flex-wrap items-center gap-2 border border-line rounded-xl px-3 py-2 bg-surface-sunken/60">
              <span className="text-xs text-ink-muted">
                期间政策：
                {run.period_policy?.mode === 'pinned' ? `指定年度 ${run.report_period || '—'}`
                  : run.period_policy?.mode === 'latest_each' ? '逐家最新'
                  : `统一报告期 ${run.report_period || '（无法确定）'}`}
              </span>
              {!run.comparable && (
                <span className="text-xs text-state-issue">各主体评估期间不同：结果不可直接横比</span>
              )}
              <label className="text-xs text-ink-faint ml-auto flex items-center gap-1">
                展示排序
                <select value={sortField} onChange={e => {
                  const f = e.target.value
                  setSortField(f)
                  if (run) {
                    const q = f ? `?id=${encodeURIComponent(run.run_id)}&sort=${encodeURIComponent(`${f}:${sortDir}`)}`
                      : `?id=${encodeURIComponent(run.run_id)}`
                    fetch(`/api/screen/run${q}`).then(r => r.json()).then(d => {
                      if (d.run) setRun(d.run as Run)
                    }).catch(() => { /* 排序失败保持原顺序，不改结果 */ })
                  }
                }}
                  className="rounded-lg border border-line bg-surface px-1.5 py-1 text-xs text-ink">
                  <option value="">不排序（按代码）</option>
                  {(fields?.fields ?? []).map(f => <option key={f.field_id} value={f.field_id}>{f.label}</option>)}
                </select>
                <select value={sortDir} onChange={e => {
                  const dir = e.target.value as 'desc' | 'asc'
                  setSortDir(dir)
                  if (run && sortField) {
                    fetch(`/api/screen/run?id=${encodeURIComponent(run.run_id)}&sort=${encodeURIComponent(`${sortField}:${dir}`)}`)
                      .then(r => r.json()).then(d => { if (d.run) setRun(d.run as Run) })
                      .catch(() => {})
                  }
                }} disabled={!sortField}
                  className="rounded-lg border border-line bg-surface px-1.5 py-1 text-xs text-ink disabled:text-ink-faint">
                  <option value="desc">降序</option>
                  <option value="asc">升序</option>
                </select>
              </label>
            </div>
            {run.view?.applied && (
              <div className="text-xs text-ink-faint">
                排序只影响展示顺序，不改变结果身份与哈希（结果哈希 {short12(run.result_hash)}… 与未排序时一致）。
              </div>
            )}
            {run.notes.filter(n => !n.startsWith('数据来源')).map((n, i) => (
              <Alert key={i} tone={n.includes('冲突') || n.includes('混合') ? 'warn' : 'info'}>{n}</Alert>
            ))}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <StatCard label="通过" value={String(run.counts.pass)} tone="success"
                hint="全部规则满足" />
              <StatCard label="未通过" value={String(run.counts.fail)} tone="issue"
                hint="有读数但不满足规则" />
              <StatCard label="资料不足" value={String(run.counts.insufficient)} tone="default"
                hint="缺读数/冲突/待核验，不计入未通过" />
              <StatCard label="合计" value={String(run.counts.total)} />
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-ink-faint">只看：</span>
              {([['all', '全部'], ['pass', '通过'], ['fail', '未通过'], ['insufficient', '资料不足']] as const).map(([key, label]) => (
                <button key={key} type="button" onClick={() => setResultFilter(key)}
                  aria-pressed={resultFilter === key}
                  className={`px-2.5 py-1 rounded-full border text-xs transition-colors ${resultFilter === key
                    ? 'border-brand/50 bg-brand-soft text-brand-strong'
                    : 'border-line text-ink-muted hover:bg-surface-raised'}`}>
                  {label}{key !== 'all' ? ` ${run.counts[key] ?? 0}` : ` ${run.counts.total ?? 0}`}
                </button>
              ))}
            </div>
            {(['pass', 'fail', 'insufficient'] as const)
              .filter(group => resultFilter === 'all' || resultFilter === group)
              .map(group => {
                const list = rows?.[group] ?? []
                const page = groupPages[group] ?? 1
                const shown = list.slice(0, PAGE_SIZE * page)
                return (
                  <div key={group}>
                    <div className="flex items-center gap-2 mb-2">
                      {verdictBadge(group)}
                      <span className="text-xs text-ink-faint">
                        {group === 'pass' ? '全部规则满足的公司' : group === 'fail' ? '有明确读数、但不满足规则' : '缺读数或冲突，不猜结论'}
                      </span>
                      <span className="text-xs text-ink-faint ml-auto tabular-nums">{list.length} 家</span>
                    </div>
                    {list.length === 0 ? (
                      <div className="border border-dashed border-line rounded-xl px-4 py-4 text-xs text-ink-faint">
                        {group === 'pass' && '本次条件下没有通过的公司。这是筛选结果，不是「暂无数据」——见下方未通过与资料不足的明细。'}
                        {group === 'fail' && '没有因规则不满足而落选的公司。'}
                        {group === 'insufficient' && '没有资料不足的公司：每家都有满足比较所需的读数。'}
                      </div>
                    ) : (
                      <div className="space-y-2">
                        {shown.map(renderRow)}
                        {shown.length < list.length && (
                          <button type="button"
                            onClick={() => setGroupPages(prev => ({ ...prev, [group]: page + 1 }))}
                            className="w-full rounded-lg border border-dashed border-line py-2 text-xs text-ink-muted hover:bg-surface-raised transition-colors">
                            显示更多（还剩 {list.length - shown.length} 家）
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
              <span className="text-xs text-ink-faint mr-auto">
                结果已冻结：run_id {run.run_id} · 结果哈希 {short12(run.result_hash)}… · 快照 {short12(run.snapshot_id)}…
              </span>
              <button type="button" onClick={() => openRun(run.run_id)} disabled={busy === 'original'}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-line text-ink-muted
                  hover:bg-surface-raised text-xs transition-colors disabled:opacity-50">
                <ShieldCheck className="w-3.5 h-3.5" /> 查看原版
              </button>
              <button type="button" onClick={doRun} disabled={busy === 'run' || readOnly}
                title={readOnly ? '只读账号不能重新执行筛选' : undefined}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-brand/40 text-brand-strong
                  hover:bg-brand-soft text-xs transition-colors disabled:opacity-50">
                <RefreshCw className={`w-3.5 h-3.5 ${busy === 'run' ? 'animate-spin' : ''}`} />
                用当前数据重新筛选
              </button>
            </div>
          </div>
        )}
      </Card>

      {/* ── B3：我的研究清单（观察对象，不是持仓）── */}
      <Card>
        <SectionTitle icon={<Star className="w-4 h-4 text-brand" />}
          title="我的研究清单（观察对象，不是持仓）"
          extra={watch.length ? `${watch.length} 只` : undefined} />
        <p className="text-xs text-ink-faint leading-relaxed pb-2">
          从筛选结果点「加入清单」即可；备注只在本账号可见，不改变任何筛选结果，也不代表持仓或已执行组合。
        </p>
        <div className="flex flex-wrap items-center gap-2 pb-2">
          <button type="button" onClick={openBrief} aria-expanded={briefOpen}
            title="生成「本次记录相对上次记录」的变化简报（只读投影，零模型调用；查看不会推进基准）"
            className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-line-strong
              text-xs text-ink-muted hover:text-brand hover:border-brand/40 transition-colors">
            {briefOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <GitCompareArrows className="w-3.5 h-3.5" />}
            {briefOpen ? '收起「查看变化」' : '查看变化（相对上次记录）'}
          </button>
          <span className="text-xs text-ink-faint tabular-nums">
            清单版本 rev {watchRev !== '' ? String(watchRev) : '—'}
            {watchBaseline ? ` · 跟踪基准 ${short12(watchBaseline)}…` : ' · 还没有跟踪基准'}
          </span>
        </div>
        {watchMsg && <div className="text-xs text-ink-muted pb-2">{watchMsg}</div>}
        {watch.length === 0 ? (
          <EmptyState icon={<Star className="w-5 h-5" />}
            title="清单还是空的"
            description="在结果里点「加入清单」，就能把想继续跟踪的公司放在这里，写下简短备注。" />
        ) : (
          <div className="space-y-2">
            {watch.map(item => (
              <div key={item.code} className="border border-line rounded-lg px-3 py-2 bg-surface-sunken/40">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-ink">{item.name || item.code}</span>
                  <span className="text-xs text-ink-faint tabular-nums">{item.code}</span>
                  <button type="button" onClick={() => focusWatchInRun(item.code)}
                    title="把这家公司勾进第 1 步的股票池（不自动执行筛选）"
                    className="text-xs text-brand hover:underline">加入股票池</button>
                  <Link to={`/screen/company?code=${encodeURIComponent(item.code)}`}
                    title="打开公司页：只有代码时先选记录或数据集，不自动打开不明来源记录"
                    className="text-xs text-brand hover:underline">看公司</Link>
                  <button type="button" onClick={() => toggleWatch(item.code, item.name)}
                    disabled={watchBusy === item.code || readOnly}
                    className="ml-auto text-xs text-ink-muted hover:text-red-700 disabled:opacity-50">移出清单</button>
                </div>
                <div className="flex flex-wrap items-center gap-2 pt-1.5">
                  <input
                    value={watchNote[item.code] ?? ''}
                    onChange={e => setWatchNote(prev => ({ ...prev, [item.code]: e.target.value }))}
                    onBlur={() => { if ((watchNote[item.code] ?? '') !== (item.note ?? '')) saveWatchNote(item.code) }}
                    placeholder="简短研究备注（最多 500 字，只在本账号可见）"
                    maxLength={500}
                    disabled={readOnly}
                    className="flex-1 min-w-[12rem] px-2 py-1 rounded-lg border border-line bg-surface
                      text-xs text-ink placeholder:text-ink-faint focus:outline-none focus:border-brand/50" />
                  <span className="text-xs text-ink-faint tabular-nums">{(watchNote[item.code] ?? '').length}/500</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* ── W2：查看变化（自选变化简报，只读投影；查看不推进基准）── */}
      {briefOpen && (
        <Card>
          <SectionTitle icon={<GitCompareArrows className="w-4 h-4 text-brand" />}
            title="查看变化（本次记录相对上次记录）"
            extra={`清单 rev ${watchRev !== '' ? String(watchRev) : '—'}`} />
          <p className="text-xs text-ink-faint leading-relaxed pb-2">
            简报是对两份冻结结果的只读投影：零模型调用、零外部请求；不重算、不改旧结果。
            查看简报不会推进基准；基准只能显式设置或清除，条件/期间/实现口径不同会标「不可直接横比」。
          </p>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block min-w-0">
              <span className="text-xs text-ink-faint block pb-1">
                上次记录（base{watchBaseline ? ' · 当前跟踪基准' : ''}）
              </span>
              <select value={briefBase} onChange={e => setBriefBase(e.target.value)}
                aria-label="选择上次记录（简报基准）"
                className="w-full px-2 py-1.5 rounded-lg border border-line bg-surface text-xs text-ink
                  focus:outline-none focus:border-brand/50">
                <option value="">— 选择一份本人冻结结果 —</option>
                {runs.map(r => <option key={r.run_id} value={r.run_id}>{runOption(r)}</option>)}
              </select>
            </label>
            <label className="block min-w-0">
              <span className="text-xs text-ink-faint block pb-1">本次记录（current）</span>
              <select value={briefCurrent} onChange={e => setBriefCurrent(e.target.value)}
                aria-label="选择本次记录"
                className="w-full px-2 py-1.5 rounded-lg border border-line bg-surface text-xs text-ink
                  focus:outline-none focus:border-brand/50">
                <option value="">— 选择一份本人冻结结果 —</option>
                {runs.map(r => <option key={r.run_id} value={r.run_id}>{runOption(r)}</option>)}
              </select>
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-2 pt-2">
            <button type="button" onClick={generateBrief}
              disabled={briefBusy === 'brief' || !briefBase || !briefCurrent}
              className="px-3 py-1.5 rounded-lg bg-brand text-white text-xs font-medium hover:opacity-90 disabled:opacity-40">
              {briefBusy === 'brief'
                ? <span className="inline-flex items-center gap-1"><Loader2 className="w-3.5 h-3.5 animate-spin" /> 对比中…</span>
                : '生成简报'}
            </button>
            <button type="button" onClick={() => setBriefBaseline(false)}
              disabled={briefBusy !== '' || !briefCurrent || readOnly}
              title="把「本次记录」设为跟踪基准（简报的 base）；期间被另一页面改过会 409，不覆盖"
              className="px-3 py-1.5 rounded-lg border border-line-strong text-xs text-ink-muted hover:text-brand disabled:opacity-40">
              {briefBusy === 'baseline' ? <Loader2 className="w-3.5 h-3.5 animate-spin inline" /> : '把「本次记录」设为跟踪基准'}
            </button>
            {watchBaseline && (
              <button type="button" onClick={() => setBriefBaseline(true)}
                disabled={briefBusy !== '' || readOnly}
                className="px-3 py-1.5 rounded-lg border border-line-strong text-xs text-ink-muted hover:text-red-700 disabled:opacity-40">
                {briefBusy === 'clear' ? <Loader2 className="w-3.5 h-3.5 animate-spin inline" /> : '清除基准'}
              </button>
            )}
          </div>
          {briefErr && <div className="pt-2"><Alert tone="danger" title="简报 / 基准操作未完成">{briefErr}</Alert></div>}
          {briefMsg && <div className="text-xs text-ink-muted pt-2" role="status">{briefMsg}</div>}
          {brief && brief.status !== 'failed' && (
            <div className="border-t border-line mt-3 pt-3 space-y-3" aria-live="polite">
              {(brief.comparability?.not_comparable_reasons?.length ?? 0) > 0 && (
                <Alert tone="warn" title="不可直接横比：先核对口径，再解读差异">
                  <ul className="list-disc pl-4">
                    {(brief.comparability?.not_comparable_reasons ?? []).map((r, i) => <li key={i}>{r}</li>)}
                  </ul>
                </Alert>
              )}
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                {([
                  ['条件读数变化', brief.overview?.conditions_changed],
                  ['数据/来源更新', brief.overview?.data_changed],
                  ['范围变化', brief.overview?.scope_changed],
                  ['资料不足', brief.overview?.insufficient],
                  ['缺记录', brief.overview?.missing],
                  ['未观察到变化', brief.overview?.unchanged],
                ] as [string, number | undefined][]).map(([k, v]) => (
                  <div key={k} className="border border-line rounded-lg px-3 py-2 bg-surface-sunken/40">
                    <div className="text-xs text-ink-faint">{k}</div>
                    <div className="text-lg font-semibold text-ink tabular-nums">{v ?? 0}</div>
                  </div>
                ))}
              </div>
              {(brief.overview?.rules_changed || brief.overview?.impl_version_changed
                || brief.overview?.source_changed || brief.overview?.period_policy_changed) && (
                <div className="text-xs text-ink-faint flex flex-wrap gap-x-3 gap-y-1">
                  {brief.overview?.rules_changed && <span className="text-state-issue">条件不同：读数差异不能当成经营变化</span>}
                  {brief.overview?.impl_version_changed && <span className="text-state-issue">计算实现版本变了</span>}
                  {brief.overview?.source_changed && <span className="text-state-issue">来源数据集/文件变了</span>}
                  {brief.overview?.period_policy_changed && <span className="text-state-issue">期间策略/报告期不同</span>}
                </div>
              )}
              <div className="grid gap-2 sm:grid-cols-2">
                {(['base', 'current'] as const).map(side => {
                  const s = brief.identity?.[side]
                  return (
                    <div key={side} className="border border-line rounded-lg px-3 py-2 bg-surface-sunken/40">
                      <div className="text-xs font-medium text-ink-muted pb-1">
                        {side === 'base' ? '上次记录（base）' : '本次记录（current）'}
                      </div>
                      <div className="text-xs text-ink-faint tabular-nums leading-relaxed break-all">
                        冻结 {fmtTime(s?.created_at)} · 截止 {s?.as_of || '—'} · 哈希 {short12(s?.result_hash)}… ·
                        实现 {s?.impl_version || '—'}
                        {s?.source?.synthetic ? ' · 合成演示' : s?.source?.user_provided ? ' · 用户提供' : ''}
                      </div>
                      {s?.run_id && (
                        <button type="button" onClick={() => openRun(s.run_id!)}
                          className="text-xs text-brand hover:underline pt-1 inline-flex items-center gap-1">
                          <ExternalLink className="w-3 h-3" /> 打开这一份结果
                        </button>
                      )}
                    </div>
                  )
                })}
              </div>
              {(brief.cards ?? []).length === 0 ? (
                <EmptyState title="两份记录里没有可比的公司"
                  description="清单/记录覆盖为空：先确认股票池、数据集与范围。" />
              ) : (
                <div className="space-y-2">
                  {(brief.cards ?? []).map(card => (
                    <div key={card.entity_id}
                      className="border border-line rounded-xl px-3 py-2.5 bg-surface space-y-1.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-ink">{card.entity || card.entity_id}</span>
                        <span className="text-xs text-ink-faint tabular-nums">{card.entity_id}</span>
                        <span className="text-xs text-ink-muted tabular-nums">
                          {briefStatus(card.status_transition?.[0])} → {briefStatus(card.status_transition?.[1])}
                        </span>
                        {card.no_observed_change && (
                          <span className="px-2 py-0.5 rounded-full text-xs bg-line-strong/20 text-ink-muted">
                            未观察到变化
                          </span>
                        )}
                        {Array.from(new Set(card.change_kinds ?? [])).map((k) => (
                          <span key={k}
                            className="px-2 py-0.5 rounded-full text-xs bg-brand/10 text-brand border border-brand/25">
                            {brief.change_kinds_legend?.[k] || k}
                          </span>
                        ))}
                      </div>
                      {(card.warnings ?? []).map((w, i) => (
                        <div key={i} className="text-xs text-amber-700 flex gap-1.5 leading-snug">
                          <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />{w}
                        </div>
                      ))}
                      {(card.value_changes ?? []).length > 0 && (
                        <div className="overflow-x-auto">
                          <table className="w-full text-xs">
                            <thead>
                              <tr className="text-left text-ink-faint border-b border-line/70">
                                <th className="py-1 pr-3 font-medium">科目</th>
                                <th className="py-1 pr-3 font-medium">期间</th>
                                <th className="py-1 pr-3 font-medium">上次 → 本次</th>
                                <th className="py-1 pr-3 font-medium">变化</th>
                                <th className="py-1 font-medium">说明</th>
                              </tr>
                            </thead>
                            <tbody>
                              {(card.value_changes ?? []).map((v, i) => (
                                <tr key={`${v.metric}-${v.period}-${i}`}
                                  className="border-b border-line/40 last:border-0">
                                  <td className="py-1 pr-3 text-ink-muted whitespace-nowrap">{v.metric_label || v.metric}</td>
                                  <td className="py-1 pr-3 tabular-nums text-ink-faint whitespace-nowrap">{v.period || '—'}</td>
                                  <td className="py-1 pr-3 tabular-nums text-ink whitespace-nowrap">
                                    {v.change === 'changed'
                                      ? `${fmtNum(v.base?.value)} → ${fmtNum(v.current?.value)} ${v.current?.unit || v.base?.unit || ''}`
                                      : v.change === 'new_in_current' ? '上次没有这条观察'
                                      : v.change === 'removed_from_current' ? '本次没有这条观察'
                                      : `${fmtNum(v.base?.value)} → ${fmtNum(v.current?.value)}`}
                                  </td>
                                  <td className="py-1 pr-3 tabular-nums whitespace-nowrap">
                                    {v.change === 'changed' && v.delta != null ? (
                                      <span className={v.delta > 0 ? 'text-state-success'
                                        : v.delta < 0 ? 'text-state-failure' : 'text-ink-muted'}>
                                        {fmtDelta(v.delta)} {v.delta_pct_points != null ? '（百分点）' : ''}
                                        {v.base?.value ? ` · 相对 ≈${fmtDelta(v.delta / Math.abs(Number(v.base.value)) * 100)}%` : ''}
                                      </span>
                                    ) : v.change === 'not_comparable'
                                      ? <span className="text-amber-700">口径不同，不相减</span>
                                      : <span className="text-ink-faint">—</span>}
                                  </td>
                                  <td className="py-1 text-ink-faint leading-snug">{v.note || ''}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )}
                      {(card.reading_changes ?? []).length > 0 && (
                        <div className="grid gap-1.5 sm:grid-cols-2">
                          {(card.reading_changes ?? []).map((rc, i) => (
                            <div key={`${rc.field}-${i}`}
                              className="border border-line rounded-lg px-2.5 py-1.5 bg-surface-sunken/60">
                              <div className="text-xs text-ink-muted">{rc.label}</div>
                              <div className="text-xs text-ink tabular-nums pt-0.5">
                                {rc.base?.display || `—（${briefStatus(rc.base?.status)}）`} →{' '}
                                {rc.current?.display || `—（${briefStatus(rc.current?.status)}）`}
                              </div>
                              {(rc.base?.reason || rc.current?.reason) && (
                                <div className="text-xs text-ink-faint pt-0.5 leading-snug">
                                  {rc.base?.reason ? `上次：${rc.base.reason}` : ''}
                                  {rc.base?.reason && rc.current?.reason ? '；' : ''}
                                  {rc.current?.reason ? `本次：${rc.current.reason}` : ''}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                      {card.evidence && (
                        <div className="flex flex-wrap gap-2 text-xs">
                          {card.evidence.base?.run_id && (
                            <Link to={`/screen/company?code=${encodeURIComponent(card.entity_id)}` +
                                `&run=${encodeURIComponent(card.evidence.base.run_id)}`}
                              className="text-brand hover:underline">看公司（上次记录）</Link>
                          )}
                          {card.evidence.current?.run_id && (
                            <Link to={`/screen/company?code=${encodeURIComponent(card.entity_id)}` +
                                `&run=${encodeURIComponent(card.evidence.current.run_id)}`}
                              className="text-brand hover:underline">看公司（本次记录）</Link>
                          )}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
              {((brief.overview?.base_only_companies?.length ?? 0) > 0
                || (brief.overview?.current_only_companies?.length ?? 0) > 0) && (
                <div className="text-xs text-ink-faint leading-relaxed">
                  只在上次记录：{brief.overview?.base_only_companies?.join('、') || '—'} ·
                  只在本次记录：{brief.overview?.current_only_companies?.join('、') || '—'}
                  （范围差异不算「失去资格」，先确认范围/数据集）
                </div>
              )}
              {brief.change_kinds_legend && (
                <div className="text-xs text-ink-faint flex flex-wrap gap-x-3 gap-y-1">
                  <span className="text-ink-muted">变化类型：</span>
                  {Object.entries(brief.change_kinds_legend).map(([k, label]) => (
                    <span key={k}>{label}</span>
                  ))}
                </div>
              )}
              <div className="text-xs text-ink-faint tabular-nums">
                brief_id {short12(brief.brief_id)}… · 模型调用 {brief.cost?.model_calls ?? 0} 次 ·
                {brief.cost?.network_requests || '外部请求未知'}
              </div>
              {(brief.notes ?? []).length > 0 && (
                <ul className="text-xs text-ink-faint list-disc pl-4 space-y-0.5">
                  {(brief.notes ?? []).map((n, i) => <li key={i}>{n}</li>)}
                </ul>
              )}
            </div>
          )}
          {brief && brief.status === 'failed' && (
            <div className="pt-3">
              <Alert tone="danger" title="简报生成失败（基准未推进）">
                {(brief.errors ?? []).join('；') || '服务端拒绝：不重试覆盖，先核对两份记录。'}
              </Alert>
            </div>
          )}
        </Card>
      )}

      {/* ── B3：成本与请求（不猜数字：没有报价就写未知）── */}
      <Card>
        <SectionTitle icon={<Coins className="w-4 h-4 text-brand" />}
          title="成本与请求（本账户）"
          extra={cost?.updated_at ? `更新于 ${fmtTime(cost.updated_at)}` : undefined} />
        <button type="button" onClick={toggleCost}
          className="text-xs text-brand hover:underline inline-flex items-center gap-1 py-1">
          {costOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
          {costOpen ? '收起分账明细' : '展开分账明细'}
        </button>
        {costOpen && cost && (
          <div className="space-y-2 pt-1">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {[
                ['执行请求', `${cost.runs_total} 次`],
                ['确定性计算', `${cost.compute_runs} 次 · ${cost.compute_ms} ms`],
                ['复用已冻结结果', `${cost.cache_hits} 次`],
                ['模型调用', `${cost.model_calls} 次`],
                ['数据准备请求', `${cost.prepare_requests} 次`],
                ['输入预览', `${cost.previews} 次`],
                ['适配器调用', `${cost.adapter_calls ?? 0} 次`],
                ['真实 HTTP 请求', cost.network_requests === null ? '未知' : `${cost.network_requests} 次`],
                ['外部费用', cost.external_fee === 'unknown' ? '未知（无报价）' : cost.external_fee],
              ].map(([k, v]) => (
                <div key={k} className="border border-line rounded-lg px-3 py-2 bg-surface-sunken/40">
                  <div className="text-xs text-ink-faint">{k}</div>
                  <div className="text-sm text-ink tabular-nums">{v}</div>
                </div>
              ))}
            </div>
            <p className="text-xs text-ink-faint leading-relaxed">
              筛选是确定性计算：本次链路模型调用恒为 0、出站抓取为 0；「外部费用」没有报价就记未知，
              本地 API 额度为零不等于整体运营成本为零。
            </p>
          </div>
        )}
      </Card>

      {/* ── 历史结果（保存并重开同一份） ── */}
      <Card>
        <SectionTitle icon={<RefreshCw className="w-4 h-4 text-brand" />}
          title="历史结果（原版只读）"
          extra={runs.length ? `${runs.length} 份` : undefined} />
        {!histOpen ? (
          <button type="button" onClick={() => setHistOpen(true)}
            className="text-xs text-brand hover:underline inline-flex items-center gap-1 py-2">
            <ChevronRight className="w-3.5 h-3.5" /> 展开历史结果列表
          </button>
        ) : runs.length === 0 ? (
          <EmptyState title="还没有已保存的结果" description="执行一次筛选后，结果会按 run_id 冻结保存。" />
        ) : (
          <>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-ink-faint border-b border-line">
                  <th className="py-2 pr-3 font-medium">生成时间</th>
                  <th className="py-2 pr-3 font-medium">run_id</th>
                  <th className="py-2 pr-3 font-medium">截止日</th>
                  <th className="py-2 pr-3 font-medium">期间口径</th>
                  <th className="py-2 pr-3 font-medium">通过/未通过/不足</th>
                  <th className="py-2 pr-3 font-medium">数据来源</th>
                  <th className="py-2 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {runs.map(r => (
                  <tr key={r.run_id} className="border-b border-line/60 last:border-0">
                    <td className="py-2 pr-3 text-xs text-ink-muted tabular-nums whitespace-nowrap">{fmtTime(r.created_at)}</td>
                    <td className="py-2 pr-3 text-xs text-ink-faint tabular-nums">{short12(r.run_id)}…</td>
                    <td className="py-2 pr-3 text-xs text-ink-muted tabular-nums">{r.as_of}</td>
                    <td className="py-2 pr-3 text-xs whitespace-nowrap">
                      {r.period_policy?.mode === 'latest_each'
                        ? <span className="text-state-issue">逐家最新（不可横比）</span>
                        : <span className="text-ink-muted tabular-nums">
                          {r.period_policy?.mode === 'pinned' ? '指定 ' : '统一 '}
                          {r.report_period || '—'}
                        </span>}
                    </td>
                    <td className="py-2 pr-3 text-xs tabular-nums">
                      <span className="text-state-success">{r.counts?.pass ?? 0}</span>
                      <span className="text-ink-faint"> / </span>
                      <span className="text-state-issue">{r.counts?.fail ?? 0}</span>
                      <span className="text-ink-faint"> / </span>
                      <span className="text-ink-muted">{r.counts?.insufficient ?? 0}</span>
                    </td>
                    <td className="py-2 pr-3 text-xs text-ink-faint">
                      {r.source?.synthetic ? '合成演示' : r.source?.user_provided ? '用户提供' : '真实披露缓存'}
                      {r.integrity && r.integrity !== 'ok' && <span className="text-state-failure">（{r.integrity === 'hash_mismatch' ? '哈希不一致' : '清单缺失'}）</span>}
                    </td>
                    <td className="py-2">
                      <button type="button" onClick={() => openRun(r.run_id)} disabled={busy === 'original'}
                        className="text-xs text-brand hover:underline disabled:opacity-50">打开原版</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="border-t border-line mt-3 pt-3 space-y-3">
            <div className="text-xs text-ink-faint">
              人工复筛对比（只读两份冻结结果）：谁进、谁出、谁的读数变了。规则 / 股票池 / 期间口径
              只要有一样不同，就标「不可直接归因」——差异不能当成筛选条件的效果。
            </div>
            <div className="flex flex-col sm:flex-row gap-2 sm:items-end">
              <label className="text-xs text-ink-faint">
                基准结果（先的那份）
                <select value={cmpA} onChange={e => setCmpA(e.target.value)}
                  className="mt-1 block w-72 rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink">
                  <option value="">（选一份）</option>
                  {runs.map(r => (
                    <option key={r.run_id} value={r.run_id}>
                      {fmtTime(r.created_at)} · {short12(r.run_id)} · 通过 {r.counts?.pass ?? 0}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-xs text-ink-faint">
                对照结果（后的那份）
                <select value={cmpB} onChange={e => setCmpB(e.target.value)}
                  className="mt-1 block w-72 rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink">
                  <option value="">（选一份）</option>
                  {runs.map(r => (
                    <option key={r.run_id} value={r.run_id}>
                      {fmtTime(r.created_at)} · {short12(r.run_id)} · 通过 {r.counts?.pass ?? 0}
                    </option>
                  ))}
                </select>
              </label>
              <button type="button" onClick={runCompare} disabled={cmpBusy}
                className="px-3 py-1.5 rounded-lg bg-brand text-white text-xs hover:bg-brand-strong
                  transition-colors disabled:opacity-50">
                {cmpBusy ? '对比中…' : '对比这两份'}
              </button>
            </div>
            {cmpErr && <Alert tone="danger" title="对比未完成">{cmpErr}</Alert>}
            {cmp && (
              <div className="space-y-3">
                <Alert tone={cmp.attribution.code === 'input_changed' ? 'info' : 'warn'}
                  title={`归因：${cmp.attribution.label}`}>
                  <div className="space-y-1">
                    <div>{cmp.attribution.note}</div>
                    {cmp.attribution.reasons.map((r, i) => (
                      <div key={i} className="text-xs">· {r}</div>
                    ))}
                  </div>
                </Alert>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  {cmp.conditions.map(c => (
                    <div key={c.key}
                      className={`border rounded-lg px-3 py-2 text-xs ${c.same
                        ? 'border-line bg-surface-sunken/40'
                        : 'border-state-issue/50 bg-surface-sunken/70'}`}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-ink">{c.label}</span>
                        <span className={c.same ? 'text-state-success' : 'text-state-issue'}>
                          {c.same ? '相同'
                            : ['rules', 'pool', 'period_policy', 'eval_periods',
                               'impl_version', 'field_map_version'].includes(c.key)
                              ? '不同 → 不可直接归因'
                              : '不同'}
                        </span>
                      </div>
                      {!c.same && (
                        <div className="mt-1 space-y-0.5 text-ink-muted">
                          <div>基准：{c.base}</div>
                          <div>对照：{c.other}</div>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
                <div className="space-y-1 text-xs text-ink-muted">
                  <div className="flex flex-wrap gap-x-5 gap-y-1">
                    <span className="text-ink-faint">股票池：</span>
                    <span>新增 <b className="text-ink tabular-nums">{cmp.summary.pool_added}</b></span>
                    <span>移除 <b className="text-ink tabular-nums">{cmp.summary.pool_removed}</b></span>
                    <span className="text-ink-faint">｜结果里：</span>
                    <span>新增 <b className="text-ink tabular-nums">{cmp.summary.entered}</b></span>
                    <span>取消 <b className="text-ink tabular-nums">{cmp.summary.exited}</b></span>
                    <span>两份都在 <b className="text-ink tabular-nums">{cmp.summary.stayed}</b></span>
                  </div>
                  <div className="flex flex-wrap gap-x-5 gap-y-1">
                    <span className="text-ink-faint">筛选结论（只算两份都在的公司）：</span>
                    <span>新通过（原未通过） <b className="text-state-success tabular-nums">{cmp.summary.became_pass}</b></span>
                    <span>不再通过 <b className="text-ink tabular-nums">{cmp.summary.left_pass}</b></span>
                    <span>读数有变化 <b className="text-ink tabular-nums">{cmp.summary.stayed_changed}</b></span>
                    <span>期间不可比 <b className={cmp.summary.not_comparable ? 'text-state-issue' : 'text-ink'}>{cmp.summary.not_comparable}</b></span>
                  </div>
                  <div className="flex flex-wrap gap-x-5 gap-y-1">
                    <span className="text-ink-faint">资料变化（不算经营改善）：</span>
                    <span>新变资料不足 <b className={cmp.summary.insufficient_new ? 'text-state-issue' : 'text-ink'}>{cmp.summary.insufficient_new}</b></span>
                    <span>资料不足已补齐 <b className={cmp.summary.insufficient_resolved ? 'text-state-success' : 'text-ink'}>{cmp.summary.insufficient_resolved}</b>
                      {cmp.summary.insufficient_resolved > 0 && (
                        <span className="text-ink-faint">（其中补齐后通过 {cmp.summary.pass_from_insufficient} 家）</span>
                      )}</span>
                  </div>
                </div>
                {(cmp.entered.length > 0 || cmp.exited.length > 0) && (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
                    <div className="border border-line rounded-lg px-3 py-2">
                      <div className="text-ink mb-1">新进入（{cmp.entered.length}）</div>
                      {cmp.entered.length === 0 ? <div className="text-ink-faint">无</div> : cmp.entered.map(e => (
                        <div key={e.code} className="text-ink-muted">
                          {e.entity} {e.code} · {e.verdict_label}{e.eval_period ? ` · ${e.eval_period}` : ''}
                          <div className="text-ink-faint">{e.explain}</div>
                        </div>
                      ))}
                    </div>
                    <div className="border border-line rounded-lg px-3 py-2">
                      <div className="text-ink mb-1">退出（{cmp.exited.length}）</div>
                      {cmp.exited.length === 0 ? <div className="text-ink-faint">无</div> : cmp.exited.map(e => (
                        <div key={e.code} className="text-ink-muted">
                          {e.entity} {e.code} · {e.verdict_label}{e.eval_period ? ` · ${e.eval_period}` : ''}
                          <div className="text-ink-faint">{e.explain}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                <div className="space-y-1">
                  <div className="text-xs text-ink-faint">
                    两份都在的公司：读数只比可筛选字段（{cmp.field_whitelist.map(f => f.label).join('、')}），
                    其它数字不算读数变化。
                  </div>
                  {cmp.stayed.length === 0 && <div className="text-xs text-ink-faint">没有两份都出现的公司。</div>}
                  {cmp.stayed.map(r => (
                    <div key={r.code} className="border border-line rounded-lg px-3 py-2 text-xs space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-ink">{r.entity} {r.code}</span>
                        <span className="text-ink-faint">{r.base_verdict_label} → {r.other_verdict_label}</span>
                        {!r.comparable && <span className="text-state-issue">期间不同：读数不可直接比较</span>}
                        {r.comparable && r.field_changes.length === 0 && !r.verdict_changed && (
                          <span className="text-state-success">读数与结论都没变</span>
                        )}
                      </div>
                      {r.field_changes.map(f => (
                        <div key={f.field} className="text-ink-muted">
                          {f.label}：{f.base.display} → {f.other.display}
                          {f.kind === 'state' && <span className="text-ink-faint">（这一项在两份里「有数/无数」不同，不是数值变化）</span>}
                        </div>
                      ))}
                      {r.note && <div className="text-ink-faint">{r.note}</div>}
                    </div>
                  ))}
                </div>
                <div className="text-xs text-ink-faint">{cmp.reading}　{cmp.note}</div>
              </div>
            )}
          </div>
          </>
        )}
      </Card>
    </div>
  )
}

// ── 子面板 ─────────────────────────────────────────────────────────────

function PreviewPanel({ p }: { p: PreviewPayload }) {
  const excluded = p.excluded ?? []
  const pending = p.pending ?? []
  return (
    <div className="border border-line rounded-xl overflow-hidden">
      <div className="px-4 py-2.5 bg-surface-raised/50 text-xs text-ink-muted">
        输入预览 · 截至 {p.as_of || '—'}
        {p.snapshot_id ? ` · 快照 ${short12(p.snapshot_id)}` : ''}
        {typeof p.entities_without_periods === 'number' && p.entities_without_periods > 0
          ? ` · ${p.entities_without_periods} 家主体在截止日前无可用年度期间` : ''}
      </div>
      <div className="p-4 space-y-3 text-xs text-ink-muted">
        {(p.entities ?? []).map(pe => (
          <div key={pe.entity_id} className="border border-line rounded-lg px-3 py-2 bg-surface-sunken/60">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm text-ink">{pe.entity}</span>
              <span className="tabular-nums text-ink-faint">{pe.entity_id}</span>
              <span>{pe.observations} 条观察</span>
              <span className="ml-auto">可用时点 {(pe.available_at ?? []).join('、') || '—'}</span>
            </div>
            <div className="mt-1 text-ink-faint">
              年度期间：{(pe.periods ?? []).join('、') || '—'}
              {(pe.units ?? []).length > 0 && ` · 单位 ${pe.units.join('/')}`}
              {pe.caliber && ` · 口径 ${pe.caliber}`}
            </div>
            {(pe.conflicts ?? []).length > 0 && (
              <div className="mt-1 text-state-failure">冲突：{(pe.conflicts ?? []).join('；')}</div>
            )}
            {(pe.gaps ?? []).length > 0 && (
              <div className="mt-1 text-ink-faint">缺口：{(pe.gaps ?? []).join('；')}</div>
            )}
          </div>
        ))}
        {(p.missing_pool_codes ?? []).length > 0 && (
          <Alert tone="warn" title={`目录里没有：${(p.missing_pool_codes ?? []).join('、')}`}>
            执行后这些代码会计入「资料不足」（不编造读数、不顶替）。若整池都不在输入里，执行会被直接拒绝。
          </Alert>
        )}
        {excluded.length > 0 && (
          <div>
            <div className="mb-1 text-ink-muted">按截止日排除（不纳入，不倒灌 {excluded.length} 行）：</div>
            <ul className="space-y-0.5">
              {excluded.map((x, i) => (
                <li key={i} className="text-ink-faint leading-snug">{x.reason}</li>
              ))}
            </ul>
          </div>
        )}
        {pending.length > 0 && (
          <Alert tone="warn" title={`${pending.length} 行待核验（缺可用时点，不参与比较）`}>
            <ul className="space-y-0.5">
              {pending.map((x, i) => (
                <li key={i} className="leading-snug">{x.reason}</li>
              ))}
            </ul>
          </Alert>
        )}
      </div>
    </div>
  )
}

function EvidencePanel({ ev }: { ev: Evidence }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-faint">
        <CheckCircle2 className="w-3.5 h-3.5 text-brand" />
        <span className="text-ink-muted">证据绑定这一份冻结结果：run_id {short12(ev.run_id)}… · 结果哈希 {short12(ev.result_hash)}…</span>
        {ev.integrity !== 'ok' && <span className="text-state-failure">完整性：{ev.integrity === 'hash_mismatch' ? '哈希不一致' : '清单缺失'}</span>}
      </div>
      {ev.readings.map(r => (
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
                    <th className="py-1 pr-3 font-medium">口径</th>
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
                        {inp.value != null ? `${inp.value.toLocaleString('zh-CN')} ${inp.unit}` : '—'}
                        <span className="text-ink-faint">（{inp.caliber}）</span>
                      </td>
                      <td className="py-1 pr-3 text-ink-faint">{inp.caliber}</td>
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
          {r.unresolved_inputs.length > 0 && (
            <div className="mt-1.5 text-xs text-state-issue">
              有 {r.unresolved_inputs.length} 条引用的观察不在本结果里（fact_id：{r.unresolved_inputs.join('、')}）——不回填。
            </div>
          )}
          {r.reason && <div className="mt-1.5 text-xs text-ink-muted leading-snug">{r.reason}</div>}
        </div>
      ))}
      {(ev.excluded ?? []).length > 0 && (
        <div className="text-xs text-ink-muted">
          <div className="mb-1">被截止日规则排除的行（不纳入，不倒灌）：</div>
          <ul className="space-y-0.5">
            {(ev.excluded ?? []).map((o, i) => (
              <li key={i} className="text-ink-faint leading-snug">
                {o.period} · {o.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
      {(ev.pending_pit ?? []).length > 0 && (
        <Alert tone="warn" title={`有 ${(ev.pending_pit ?? []).length} 行待核验`}>
          <ul className="space-y-0.5">
            {(ev.pending_pit ?? []).map((o, i) => <li key={i} className="leading-snug">{o.reason}</li>)}
          </ul>
        </Alert>
      )}
      {ev.source_urls.length > 0 && (
        <div className="text-xs text-ink-faint">
          来源文档（{ev.source_urls.length} 份，均为官方披露页）：
          <ul className="mt-1 space-y-0.5">
            {ev.source_urls.map(u => (
              <li key={u}><a href={u} target="_blank" rel="noreferrer"
                className="text-brand hover:underline break-all">{u}</a></li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
