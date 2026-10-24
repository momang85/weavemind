// S6（2026-10-22 真机核查 G1）：同一应用里三种时间形态并存——
//   历史页 `2026/10/21 21:25:41`（本地）、审计/设置页 `2026-10-21T12:22:10.177861+00:00`
//   （UTC + 微秒直出）、记忆页 `2026-07-23T07:56`（ISO 无时区，slice 到 16 位）。
// 用户无法判断"任务到底什么时候跑的"，还会把 UTC 读成本地。
//
// 统一口径（本模块只做包装，底层实现仍是 lib/format.formatLocalTime，避免两套）：
//   1) 一律按**本地时区**渲染；后端给的是带 +00:00 的 UTC 就换算成本地。
//   2) 显式标注「（本地时间）」，让用户不必猜时区含义。
//   3) 取不到/非法值显示「时间未知」，绝不渲染 "Invalid Date"，也不再用 '-' 顶数（G3）。
import { formatLocalTime } from './format.ts'

const UNKNOWN = '时间未知'
/** 只有时分秒的本地时钟串（后端日志的另一种写法，本就无日期，原样保留）。 */
const CLOCK_ONLY = /^\d{1,2}:\d{2}:\d{2}(\.\d+)?$/

/** 本地时间 + 时区含义标注：`2026/8/21 13:38:35（本地时间）`；未知值显示「时间未知」。 */
export function formatLocalTimeLabel(value?: string | number | null): string {
  const t = formatLocalTime(value)
  return t === UNKNOWN ? UNKNOWN : `${t}（本地时间）`
}

/**
 * 紧凑型本地时间（列表行内窄列）：`2026-07-23 07:56（本地时间）`。
 *
 * 直接由 Date 的本地字段拼接，**不解析 toLocaleString 的输出**（不同 locale 下
 * 年月日顺序会变，靠正则取字段会错位）；也不再产生 `2026-07-23T07:56` 这种
 * 无时区 ISO 残片（记忆页旧写法 `String(ts).slice(0, 16)` 的实机反例）。
 */
export function formatLocalTimeShort(value?: string | number | null): string {
  if (value === null || value === undefined || value === '') return UNKNOWN
  const raw = String(value).trim()
  if (CLOCK_ONLY.test(raw)) return `${raw}（本地时间）`
  const d = new Date(raw)
  if (isNaN(d.getTime())) return UNKNOWN
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}（本地时间）`
}
