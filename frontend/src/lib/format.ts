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
