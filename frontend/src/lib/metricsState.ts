// 指标看板的状态机（纯函数，可在 Node 里直接验证真实行为）。
//
// 背景（架构复核 01:05）：原实现刷新失败时只设置错误文案，**旧的成功率仍留在页面上**——
// "先成功后刷新失败"会继续显示上一次的快照，而文案却说"统计值显示为未知"，两者矛盾。
//
// 口径：**失败即不可用**。旧数据不当作当前值展示（避免用过期快照做判断），
// 只保留"上次成功刷新于 X"这个明确的标签；恢复成功后错误清除。

export const METRICS_UNAVAILABLE_MESSAGE =
  '指标接口不可达（服务未启动或 metrics 收集器未运行）。统计值显示为"未知"，不代表 0。'

export interface MetricsView<T> {
  data: T | null
  error: string
  loading: boolean
  /** 最近一次成功刷新的时间戳标签（仅用于说明"何时取到过数据"，不作为当前值） */
  lastOkAt: string | null
}

export function metricsInitial<T>(): MetricsView<T> {
  return { data: null, error: '', loading: true, lastOkAt: null }
}

export function metricsStart<T>(s: MetricsView<T>): MetricsView<T> {
  return { ...s, loading: true }
}

/** 成功：写入当前值，清掉错误（不保留旧错误文案）。 */
export function metricsSuccess<T>(_s: MetricsView<T>, data: T, at: string): MetricsView<T> {
  return { data, error: '', loading: false, lastOkAt: at }
}

/** 失败：丢弃旧数据（绝不把上次快照当当前值），保留"上次成功刷新"标签。 */
export function metricsFailure<T>(
  s: MetricsView<T>,
  reason: string = METRICS_UNAVAILABLE_MESSAGE,
): MetricsView<T> {
  return { data: null, error: reason, loading: false, lastOkAt: s.lastOkAt }
}
