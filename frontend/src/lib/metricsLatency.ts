// S5（2026-10-22 真机核查）：指标页「平均耗时 21 分 14 秒」与副标签「P95 21 分 14 秒」
// **精确同值**。只读定位（未改后端）：
//   · `/api/metrics` 实机值：`avg_latency_sec: 1274.4`、`p95_latency_sec: 1274.4`，
//     但同一个响应里 `latency_sample_size: 1`，而 `tasks.terminal: 569`
//     （569 来自 task_history 全表；耗时样本只取自本 metrics 进程启动后收到的
//      `elapsed_sec`，重启即清零）。
//   · metrics_collector.py:582 `p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]`：
//     len=1 时下标 `int(0.95)-1 = -1`，取到的就是那唯一样本 —— **P95 等于均值是因为
//     样本只有 1 个，不是把 P95 回落成均值，也不是前端取错字段**。
//   · 结论：属**后端统计口径/样本范围**问题（两个不同分母的数据被并列展示），
//     按子任务约定"只做前端如实标注，不改后端算法"。
//
// 前端修法：卡片主值仍显示均值，副标签与说明里写清"样本几个""P95 是否独立计算"
// "与上方终态任务数不是同一范围"，并在退化（样本 < 2 或 p95 == avg）时明确写出
// "P95 未独立计算"，避免用户读成"耗时分布非常集中"。
import { formatDuration } from './format.ts'

/** 样本数少于该值时，P95 不足以独立说明分布（单样本时 P95 恒等于均值）。 */
export const MIN_P95_SAMPLES = 2

export interface LatencyInput {
  avg_latency_sec?: number | null
  p95_latency_sec?: number | null
  latency_sample_size?: number | null
  latency_scope?: string
}

export interface LatencyCardView {
  /** 卡片主值（均值） */
  value: string
  /** 卡片副标签：P95 + 样本数（或"未独立计算"） */
  sub: string
  /** 卡片内说明（口径 + 为什么同值） */
  hint: string
  /** 样本数（未知为 null） */
  sampleSize: number | null
  /** P95 不足以独立说明分布 */
  degenerate: boolean
}

function num(v: unknown): number | null {
  return typeof v === 'number' && isFinite(v) ? v : null
}

/**
 * 耗时卡片视图（纯函数）。`terminal` 仅用于说明"与哪个数不是同一范围"，
 * **不参与**取值——避免把两个不同分母的数混成一个比例。
 */
export function latencyCardView(m: LatencyInput | null | undefined, terminal?: number | null): LatencyCardView {
  const avg = num(m?.avg_latency_sec)
  const p95 = num(m?.p95_latency_sec)
  const sample = num(m?.latency_sample_size)
  const scopeTail = '口径：平均耗时与 P95 只统计本 metrics 进程启动后收到耗时的主终态任务'
    + '（进程重启即清零），不是 task_history 全表的统计。'
  const terminalNote = typeof terminal === 'number' && terminal > 0
    ? `上方「总任务数」里的终态 ${terminal} 个来自 task_history 全表，与耗时样本不是同一范围。`
    : ''

  if (avg === null) {
    return {
      value: '未取得',
      sub: sample === null ? 'P95 未取得' : `P95 未取得 · 样本 ${sample} 个`,
      hint: `${scopeTail}${terminalNote}样本为空 = 本进程还没结算过带耗时的任务，不是"耗时 0 秒"。`
        + (m?.latency_scope ? `（后端口径原文：${m.latency_scope}）` : ''),
      sampleSize: sample,
      degenerate: false,
    }
  }

  const p95Known = p95 !== null
  const sampleKnown = sample !== null
  const degenerate = (sampleKnown && sample < MIN_P95_SAMPLES)
    || (p95Known && p95 === avg)

  let sub: string
  if (!p95Known) {
    sub = sampleKnown ? `P95 未取得 · 样本 ${sample} 个` : 'P95 未取得'
  } else if (sampleKnown && sample < MIN_P95_SAMPLES) {
    sub = `P95 未独立计算（样本 ${sample} 个）`
  } else if (!sampleKnown) {
    sub = `P95 ${formatDuration(p95)} · 样本数未取得`
  } else {
    sub = `P95 ${formatDuration(p95)} · 样本 ${sample} 个`
  }

  const parts = [scopeTail, terminalNote]
  if (degenerate) {
    parts.push(
      sampleKnown && sample < MIN_P95_SAMPLES
        ? `当前耗时样本只有 ${sample} 个，样本过少：P95 与均值同值只是因为样本太少`
          + `（只有 1 个样本时 P95 就是该样本本身），不代表耗时分布集中，请勿据此判断"任务耗时很稳定"。`
        : '后端本次给出的 P95 与均值完全相同：在样本充足时这不可能，说明 P95 未独立计算，请勿据此判断耗时分布。',
    )
  }
  if (m?.latency_scope) parts.push(`后端口径原文：${m.latency_scope}`)

  return { value: formatDuration(avg), sub, hint: parts.filter(Boolean).join(' '), sampleSize: sample, degenerate }
}
