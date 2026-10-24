// 设置页的用户可见文案（纯函数）：上游回传的英文状态/角色枚举不再直出。
// 实机反例（2026-10-22 核查 recheck/settings.txt:28,393-405）：
//   `余额：主端点 ok · 备用 unreachable`、角色徽章 `admin` / `viewer`、`创建于 -`。

import { resultLabel } from './statusMeta.ts'

/** 上游端点状态：把后端 reason 的英文枚举翻成中文；识别不了的如实标注，不猜。
 *
 *  `message` 是后端新给的「带端点域名的一整句人话」（llm_client._probe_failure_message）。
 *  为什么让它优先于 reason 枚举：同一个探测失败至少有三类互斥成因——端点不认这把
 *  密钥（401）、接入点不认这个模型名（400/404）、真的连不上（超时/DNS）——枚举把
 *  后两类折叠成 unreachable，界面据此只会说「不可达」，而 401 又只会说「密钥无效」。
 *  实测 2026-10-22 的新人实例因此先换 key（其实 key 有效，错在端点）、后查代理
 *  （其实错在模型名），两轮方向都被指反。所以后端给了 message 就原样展示，不再猜。
 */
export function balanceStatusText(ok: unknown, reason?: string | null, message?: string | null): string {
  const raw = String(reason ?? '').trim()
  const key = raw.toLowerCase()
  const msg = String(message ?? '').trim()
  if (ok) return '正常'
  if (/[\u4e00-\u9fff]/.test(msg)) return msg
  if (!raw) return msg || '未回传原因'
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
  if (r === 'researcher') return '研究员（自助研究）'
  return r ? `未知角色（原文：${r}）` : '未标注角色'
}

/**
 * 定时任务「上次」结果的枚举 → 中文（真机反例 2026-10-22：`· submitted` 直出）。
 * 后端只产生两种值（scheduled_jobs.py:262,266）：`submitted` / `error`。
 *
 * **状态→文案只有一套实现**：本函数只补 `submitted` 这一个本域特有取值，
 * 其余（error/fail/ok/deny…）一律委托 `statusMeta.resultLabel`——
 * 免得同一产品里"提交失败/失败/失败(err)"各写各的（前端守卫
 * `test_status_bearing_files_use_shared_implementation` 就是拦这个）。
 */
export function scheduledResultLabel(raw?: string | null): string {
  const r = String(raw ?? '').trim().toLowerCase()
  if (!r) return '无记录'
  if (r === 'submitted') return '已提交入队'
  // 本域把 error 说成"提交失败"（比通用的"失败"更准确：失败的是**提交**这一步）
  if (r === 'error') return '提交失败'
  const known = resultLabel(r)
  // resultLabel 对不认识的枚举原样返回；这里把"未知原文"标出来，不冒充已识别
  return known === r ? `未知结果（原文：${r}）` : known
}
