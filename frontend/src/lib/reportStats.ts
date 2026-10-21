// P1-8：报告页统计条的口径（纯函数，可在 Node 里直接断言真实行为）。
//
// 实机反例（2026-10-21 真机复验 README P1-8）：一个跑了 23 分钟、
// 终态 SUCCESS_WITH_ISSUES、验收 overall=fail 的任务，统计条写着
//   `耗时 0s`、`成功率 100%`、`Steps 7/7`
// 而左侧计划里同时挂着失败徽章——三个数都读不出来源：
//   1) `耗时` 取的是 `Date.now() - startedAt`（重开已完成的任务 → 0s），
//      真实耗时是任务详情里的 `elapsed_sec`；
//   2) `成功率` 标签描述的是**步骤执行**，却被读成"任务成功"，于是和验收结论并排矛盾；
//   3) `7/7` 把跳过/降级步骤吞掉了，看上去像 7 步全部完整执行。
// 这里只改口径与文案，不改任何验收/终态语义：验收结论仍由后端给的那一个字段说话。

import { formatDuration } from './format.ts'

export interface ReportStatsNumbers {
  totalSteps?: number | null
  successSteps?: number | null
  failedSteps?: number | null
  duration?: number | null
}

/** `search_coverage`（任务详情字段；无检索步时为 null，前端不显示这一块）。
 *  键名以后端为准：`total_search_steps` / `ok_steps` / `degraded_steps` …
 *  （`degraded` 为旧别名，兼容读取，不猜值。） */
export interface ReportCoverageLite {
  degraded_steps?: number | null
  degraded?: number | null
  ok_steps?: number | null
  total_search_steps?: number | null
  coverage_rate?: number | null
}

export interface ReportStatsInput {
  /** 终态摘要（SUCCESS / SUCCESS_WITH_ISSUES / FAILED …） */
  summary?: string | null
  /** 机器验收结论（acceptance.overall） */
  acceptanceOverall?: string | null
  stats?: ReportStatsNumbers | null
  /** 任务详情的真实耗时（秒）——优先于前端算的 stats.duration */
  elapsedSec?: number | null
  /** 报告步骤（status 已按展示口径归一：success/skipped/failed/...） */
  steps?: { status?: string | null }[] | null
  /** search_coverage：搜索步降级/覆盖率（后端字段，任务详情里给） */
  searchCoverage?: ReportCoverageLite | null
  filesCount?: number | null
}

export interface AcceptanceVerdict {
  /** 展示用结论：通过 / 未通过 / 未知 */
  overall: string
  /** 后端原始值（fail/pass/空串） */
  raw: string
  failed: boolean
  known: boolean
}

export interface ReportStatsView {
  elapsed: string
  rateLabel: string
  rateValue: string
  steps: string
  files: string
  /** 口径说明：成功率与验收结论的关系必须写清楚，不能并排无解释 */
  note: string
  acceptance: AcceptanceVerdict
}

const int = (v: unknown): number => {
  const n = typeof v === 'number' ? v : Number(v)
  return isFinite(n) && n > 0 ? Math.floor(n) : 0
}

/** 机器验收结论：只读后端字段，读不到就写"未知"，不猜。 */
export function acceptanceVerdict(
  summary?: string | null, acceptanceOverall?: string | null,
): AcceptanceVerdict {
  const raw = String(acceptanceOverall ?? '').trim()
  const sum = String(summary ?? '').trim().toUpperCase()
  const known = !!raw || !!sum
  // 验收结论优先；没有 acceptance.overall 时用终态摘要兜底（SUCCESS_WITH_ISSUES/FAILED 都不是"通过"）
  const failed = /fail|reject|未通过/i.test(raw)
    || (!raw && /ISSUES|FAILED|ERROR/.test(sum))
  const text = raw || sum
  const overall = !text ? '未知' : (failed ? `未通过（${text}）` : `通过（${text}）`)
  return { overall, raw: text, failed, known }
}

/** `7/7（含 1 跳过、1 降级）`：跳过/降级/失败数必须显式，不能吞成"全绿"。 */
export function stepsTally(opts: {
  success: number; total: number; skipped?: number; degraded?: number; failed?: number
}): string {
  const base = `${opts.success}/${opts.total}`
  const parts: string[] = []
  const failed = int(opts.failed)
  const skipped = int(opts.skipped)
  const degraded = int(opts.degraded)
  if (failed) parts.push(`${failed} 失败`)
  if (skipped) parts.push(`${skipped} 跳过`)
  if (degraded) parts.push(`${degraded} 降级`)
  return parts.length ? `${base}（含 ${parts.join('、')}）` : base
}

export function reportStatsView(input: ReportStatsInput): ReportStatsView {
  const s = input.stats || {}
  const total = int(s.totalSteps)
  const success = int(s.successSteps)
  // 耗时：优先任务详情的 elapsed_sec（后端记的真实秒数）；
  // 前端自算的 stats.duration 只作兜底，两者都拿不到时写 "—" 而不是 "0s"。
  const elapsed = formatDuration(input.elapsedSec) !== '—'
    ? formatDuration(input.elapsedSec)
    : formatDuration(s.duration)
  const rateValue = total > 0 ? `${Math.round((success / total) * 100)}%` : '—'
  const acceptance = acceptanceVerdict(input.summary, input.acceptanceOverall)
  const steps = Array.isArray(input.steps) ? input.steps : []
  const skipped = steps.filter(st => String(st?.status ?? '').toLowerCase() === 'skipped').length
  const failed = int(s.failedSteps) || steps.filter(
    st => String(st?.status ?? '').toLowerCase() === 'failed').length
  const degraded = input.searchCoverage
    ? int(input.searchCoverage.degraded_steps ?? input.searchCoverage.degraded) : 0
  const note = acceptance.failed
    ? `「步骤执行成功率」统计的是步骤执行结果（${success}/${total} 步）；`
      + `机器验收结论为「${acceptance.overall}」——步骤执行成功不等于验收通过，`
      + '验收缺口与依据见下方横幅，不要把 100% 读成"这份交付已通过"。'
    : acceptance.known
      ? `「步骤执行成功率」统计的是步骤执行结果（${success}/${total} 步）；`
        + `机器验收结论为「${acceptance.overall}」。`
      : `「步骤执行成功率」统计的是步骤执行结果（${success}/${total} 步）；`
        + '该任务的机器验收结论尚未读到（未知，不代表通过）。'
  return {
    elapsed,
    // 标签写清是**步骤**口径：旧标签"成功率"会被读成"任务成功"
    rateLabel: '步骤执行成功率',
    rateValue,
    steps: stepsTally({ success, total, skipped, degraded, failed }),
    files: String(int(input.filesCount)),
    note,
    acceptance,
  }
}
