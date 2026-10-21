// 设置页的用户可见文案（纯函数）：上游回传的英文状态/角色枚举不再直出。
// 实机反例（2026-10-22 核查 recheck/settings.txt:28,393-405）：
//   `余额：主端点 ok · 备用 unreachable`、角色徽章 `admin` / `viewer`、`创建于 -`。

/** 上游端点状态：把后端 reason 的英文枚举翻成中文；识别不了的如实标注，不猜。 */
export function balanceStatusText(ok: unknown, reason?: string | null): string {
  const raw = String(reason ?? '').trim()
  const key = raw.toLowerCase()
  if (!raw) return ok ? '正常' : '未回传原因'
  if (/^ok$/.test(key) || key === 'healthy' || key === 'available') return '正常'
  if (key === 'unreachable') return '不可达'
  if (key === 'timeout' || key === 'timed out' || /timed?\s*out/.test(key)) return '超时'
  if (key === 'not_configured' || key === 'no_key' || /api[_ -]?key/.test(key) && /not set|missing|empty/.test(key)) return '未配置'
  // 含中文的 reason 本就是后端写给人读的话，原样保留（优先于下面的英文关键词猜测，
  // 否则「上游返回 429：请求过快」会被改写成丢失细节的「请求过于频繁」）
  if (/[\u4e00-\u9fff]/.test(raw)) return raw
  if (key.includes('unauthorized') || key.includes('forbidden') || /\b40[13]\b/.test(key)) return '密钥无效或无权限'
  if (key.includes('insufficient') || key.includes('balance') || key.includes('credit') || /\b402\b/.test(key)) return '额度不足'
  if (key.includes('rate') || /\b429\b/.test(key)) return '请求过于频繁'
  if (key.includes('not_found') || /\b404\b/.test(key)) return '端点不存在'
  if (/\b5\d\d\b/.test(key)) return '上游服务异常'
  return `状态未识别（原文：${raw}）`
}

/** 用户角色：内部枚举 → 中文（选择框仍提交原枚举值，不改后端契约）。 */
export function roleLabel(role?: string | null): string {
  const r = String(role ?? '').trim().toLowerCase()
  if (r === 'admin') return '管理员'
  if (r === 'viewer') return '只读用户'
  if (r === 'reviewer') return '复核人'
  return r ? `未知角色（原文：${r}）` : '未标注角色'
}

/**
 * 定时任务「上次」结果的枚举 → 中文（真机反例 2026-10-22：`· submitted` 直出）。
 * 后端只产生两种值（scheduled_jobs.py:262,266）：`submitted` / `error`。
 * 公用的 resultLabel（statusMeta.ts）只认 ok/fail/deny 一族，不覆盖这里的枚举。
 */
export function scheduledResultLabel(raw?: string | null): string {
  const r = String(raw ?? '').trim().toLowerCase()
  if (r === 'submitted') return '已提交入队'
  if (r === 'error') return '提交失败'
  return r ? `未知结果（原文：${r}）` : '无记录'
}
