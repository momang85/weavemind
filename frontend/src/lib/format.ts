// 展示层格式化工具（唯一实现，避免各页各写一套）。
//
// 目前两项：
// 1) `normalizeDeliverySummary`：后端生成的"交付结果说明"里曾写
//    `**状态**：SUCCESS（8/8 个步骤成功）`——它描述的是**步骤**，却用了"状态"这个词，
//    与卡片上的验收徽章（如"有缺口"）口径矛盾（实测同一张卡两处状态不一致）。
//    后端已改为只写步骤，但**历史任务里存的旧文本不会自动更新**，故在此做显示归一化：
//    数字不动，只把标签纠正为"步骤"。
// 2) 时长/百分比格式化：统一口径，避免 0 秒显示成 0s 却读不出来源。

/** 交付摘要口径归一：`**状态**：X（N/M 个步骤成功）` → `**步骤**：N/M 成功`。 */
export function normalizeDeliverySummary(md?: string): string {
  if (!md) return ''
  return md.replace(
    /\*\*状态\*\*：\s*[A-Z_]+（(\d+)\/(\d+) 个步骤成功）/g,
    '**步骤**：$1/$2 成功',
  )
}

/** 秒 → 人话时长（未知/未完成时给明确文案，而不是 0s）。 */
export function formatDuration(sec?: number | null): string {
  if (sec === null || sec === undefined || !isFinite(sec) || sec <= 0) return '—'
  const s = Math.round(sec)
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  const rest = s % 60
  if (m < 60) return rest ? `${m} 分 ${rest} 秒` : `${m} 分`
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`
}

/** 比率（0-1）→ 百分比整数；无样本返回"—"。 */
export function formatRatio(ratio?: number | null): string {
  if (ratio === null || ratio === undefined || !isFinite(ratio)) return '—'
  return `${Math.round(ratio * 100)}%`
}

/** 美元金额：小额保留 4 位（便于看清单次成本），大额 2 位。 */
export function formatUsd(usd?: number | null): string {
  if (usd === null || usd === undefined || !isFinite(usd)) return '—'
  return usd >= 1 ? `$${usd.toFixed(2)}` : `$${usd.toFixed(4)}`
}

/**
 * 时间显示统一口径：**本地时区** + 非法/缺失值显示"时间未知"。
 *
 * 此前各处直接 `new Date(x).toLocaleString()`，缺值/脏数据会渲染出字面量
 * "Invalid Date"——用户读到的是一个看起来像值的东西，其实是我们没拿到时间。
 * 例外：后端部分日志只带 `HH:MM:SS`（本就无日期），原样保留好过谎报"未知"。
 */
export function formatLocalTime(value?: string | number | null): string {
  if (value === null || value === undefined || value === '') return '时间未知'
  const raw = String(value).trim()
  if (CLOCK_ONLY.test(raw)) return raw
  const d = new Date(raw)
  if (isNaN(d.getTime())) return '时间未知'
  return d.toLocaleString()
}

/** 只有时分秒的本地时钟串（后端日志的另一种时间戳写法）。 */
const CLOCK_ONLY = /^\d{1,2}:\d{2}:\d{2}(\.\d+)?$/

/**
 * 时间显示（只有时分秒，本地时区）——用于日志行等窄列。
 *
 * 此前日志行取 `timestamp.slice(-8)`：ISO 串尾 8 位是 `59+00:00` 或 `:14.834Z`，
 * 于是界面上出现的是 UTC 残片而非时间。
 */
export function formatLocalClock(value?: string | number | null): string {
  if (value === null || value === undefined || value === '') return '时间未知'
  const raw = String(value).trim()
  if (CLOCK_ONLY.test(raw)) return raw
  const d = new Date(raw)
  if (isNaN(d.getTime())) return '时间未知'
  return d.toLocaleTimeString()
}

/**
 * 预算显示口径：**0 / 缺省 = 不限**（不是"用完了"），余额未知时说未知。
 *
 * 0 值在过去会被显示成 0（读起来像"额度用尽"），而它实际表示"没有设置上限"。
 */
export function formatBudgetLabel(limit?: number | null, left?: number | null): string {
  if (limit === null || limit === undefined || !isFinite(limit) || limit <= 0) return '不限'
  if (left === null || left === undefined || !isFinite(left)) {
    return `限额 ${limit}（剩余未知）`
  }
  return `剩余 ${left}/${limit}`
}
