// S2（2026-10-22 真机核查，最典型的"原始 JSON 直出"）：设置页顶部红框把上游 400 的
// **原始报文硬截断 120 字后直接显示**，实机原文（recheck/settings.txt:25-27，三个字段各一遍）：
//   `· API Key（embedding.api_key）：Embedding API HTTP 400: {"code":"MODEL_CAPABILITY_NOT_SUPPORTED",
//     "message":"当前模型不支持该能力：embeddings","data":{"capability":`
// ① JSON 在 `"capability":` 处被切断，引号与大括号都不闭合，也没有 `…`；
// ② 同一条 400 因三个字段共用同一端点而重复三遍，用户以为是三个不同错误；
// ③ 没有一句中文结论，也没有"该怎么办"。
//
// 只读定位（未改后端）：真正的截断发生在服务端——`health_registry.py:317`
//   `str(health.get("last_error") or "降级")[:120]`
// 由 `/api/config/requirements` 的 `health_reason` 原样带出（web_ui.py:8269）。
// 因此前端**拿不到**被切掉的尾部；本模块只做三件能做的事：
//   ①从**已有片段**里解析出上游错误码与说明文字，给出中文结论；
//   ②按相同原因**合并**重复项，并列出受影响字段；
//   ③完整原文放折叠详情，并**如实说明**它可能已被服务端截断（不补全、不猜测、不编造）。
// 想拿到不截断的报文只能改后端（超出本子任务写范围，见交付说明）。

export interface HealthReasonView {
  /** 服务端回传的原文（本函数不截断） */
  raw: string
  /** 一句中文结论：出了什么事 */
  conclusion: string
  /** 可操作的下一步 */
  advice: string
  /** 上游错误码（解析到才有） */
  code: string
  /** 上游 message（解析到才有） */
  message: string
  /** 上游 HTTP 状态码（解析到才有） */
  httpStatus: number | null
  /** 报文里的能力名（如 embeddings） */
  capability: string
  /** 原文看起来不完整（JSON 括号不闭合 / 尾随逗号）→ 需说明"服务端已截断" */
  rawIncomplete: boolean
  /** 原文不完整时的说明（否则空串） */
  rawNote: string
}

/** 从可能被截断的文本里取 `"key":"value"`（值里含转义引号时退化为不匹配，宁可少取不编造）。 */
function jsonStringField(text: string, field: string): string {
  const re = new RegExp(`"${field}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`)
  const m = re.exec(text)
  if (!m) return ''
  try {
    return JSON.parse(`"${m[1]}"`) as string
  } catch {
    return m[1]
  }
}

/** 报文是否不完整：有 `{` 但括号不配对，或尾部停在分隔符上。 */
function looksIncomplete(text: string): boolean {
  if (!text.includes('{')) return false
  let depth = 0
  for (const ch of text) {
    if (ch === '{') depth += 1
    else if (ch === '}') depth -= 1
  }
  if (depth !== 0) return true
  return /[,:]\s*$/.test(text)
}

/** 上游报文/原因 → 中文结论与处理建议（纯函数，零依赖）。 */
export function healthReasonView(reason?: string | null): HealthReasonView {
  const raw = String(reason ?? '').trim()
  const httpMatch = /HTTP\s+(\d{3})/.exec(raw)
  const httpStatus = httpMatch ? Number(httpMatch[1]) : null
  const code = jsonStringField(raw, 'code')
  const message = jsonStringField(raw, 'message')
  const capability = jsonStringField(raw, 'capability') || jsonStringField(raw, 'capabilities')
  const rawIncomplete = looksIncomplete(raw)

  let conclusion: string
  if (message) {
    conclusion = httpStatus ? `上游返回 HTTP ${httpStatus}：${message}` : message
    if (code) conclusion += `（错误码 ${code}）`
  } else if (httpStatus) {
    conclusion = `上游返回 HTTP ${httpStatus}，但报文中没有可读的说明文字`
    if (code) conclusion += `（错误码 ${code}）`
  } else if (raw) {
    // 无 HTTP 码也无 JSON：只截到第一句作结论，并显式标注截断（不把断句当全文）。
    const firstSentence = raw.split(/[。；;\n]/)[0].trim()
    conclusion = firstSentence.length > 160 ? `${firstSentence.slice(0, 159)}…（结论已截断）` : firstSentence
  } else {
    conclusion = '未取得失败原因（服务端未回传 health_reason）'
  }

  const advice = adviceFor({ httpStatus, code, message, capability, raw })
  const rawNote = rawIncomplete
    ? '注意：上面这段是服务端回传的原文，尾部已被服务端截断（JSON 不闭合），前端拿不到被切掉的部分；'
      + '完整报文请在后端日志中查看。此处不补全、不猜测。'
    : ''
  return { raw, conclusion, advice, code, message, httpStatus, capability, rawIncomplete, rawNote }
}

function adviceFor(ctx: {
  httpStatus: number | null; code: string; message: string; capability: string; raw: string
}): string {
  const { httpStatus, code, message, capability, raw } = ctx
  const joined = `${code} ${message} ${raw}`
  if (/CAPABILITY_NOT_SUPPORTED|不支持该能力/.test(joined)) {
    const cap = capability || 'embeddings'
    return `当前的模型/端点不提供 ${cap} 能力。请在下方对应区块换成支持 ${cap} 的模型`
      + `（或改用支持该能力的端点），保存后点『测试连通性』复验。`
      + (cap === 'embeddings'
        ? '修好之前向量检索不可用：记忆命中率会退化为关键词兜底统计（见「记忆与进化」页顶部说明）。'
        : '')
  }
  if (httpStatus === 401 || httpStatus === 403) {
    return '请核对 API Key 与端点是否匹配（密钥失效或无该权限），保存后点『测试连通性』复验。'
  }
  if (httpStatus === 402 || /insufficient|balance|credit|额度|余额|欠费/i.test(raw)) {
    return '上游额度不足：请为该端点充值，或换一个有额度的密钥/端点，然后点『测试连通性』复验。'
  }
  if (httpStatus === 404) {
    return '端点路径不存在：请核对 Base URL（是否多了/少了路径段或版本号），保存后点『测试连通性』复验。'
  }
  if (httpStatus !== null && httpStatus >= 500) {
    return '上游服务异常：可先点『测试连通性』重试；若持续失败，请更换端点。'
  }
  if (/timeout|timed out|超时|unreachable|Connection|连接/i.test(raw)) {
    return '网络不可达或超时：请检查本机网络与 Base URL 是否可访问，然后点『测试连通性』复验。'
  }
  return '请按下面的服务端原始报文核对配置（密钥 / 端点 / 模型名），修改后点『测试连通性』复验。'
}

/** `标签（配置路径）`——同一处的字段写法，页内一致。 */
export function requirementFieldLabel(label?: string | null, path?: string | null): string {
  const l = String(label ?? '').trim()
  const p = String(path ?? '').trim()
  if (l && p) return `${l}（${p}）`
  return l || p || '未标注字段'
}

export interface DepIssueGroup {
  /** 分组键：同一原因归一组 */
  key: string
  reason: string
  /** 受影响字段的 path */
  paths: string[]
  /** 受影响字段的可读写法：`API Key（embedding.api_key）` */
  fields: string[]
  view: HealthReasonView
}

/**
 * 把「降级项」按**相同原因**合并：实机上 3 个字段共用同一条 400，页面却显示成 3 条，
 * 看起来像 3 个不同故障。合并后仍如实列出全部受影响字段（不隐藏数量）。
 */
export function groupDegradedRequirements(
  items: { path: string; label?: string | null; reason?: string | null }[],
): DepIssueGroup[] {
  const groups: DepIssueGroup[] = []
  const index = new Map<string, DepIssueGroup>()
  for (const it of items) {
    const reason = String(it.reason ?? '').trim()
    const key = reason || `__empty__:${it.path}`
    let g = index.get(key)
    if (!g) {
      g = {
        key,
        reason,
        paths: [],
        fields: [],
        view: healthReasonView(reason),
      }
      index.set(key, g)
      groups.push(g)
    }
    g.paths.push(it.path)
    g.fields.push(requirementFieldLabel(it.label, it.path))
  }
  return groups
}
