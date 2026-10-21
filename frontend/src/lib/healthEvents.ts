// 健康页事件流的两条显示口径（P2 实机复验 2026-10-22）。
//
// 实机反例 ①：事件消息是后端事件体**原样**透出的 JSON，页面直接 `<p>{e.message}</p>`：
//   `{"service": "worker_guardian", "timestamp": "…", "workers_tracked": 11, "workers_healthy"`
//   → 用户看到花括号与引号，而且被后端截断（没收尾的 `}`）。
// 实机反例 ②：事件时间直接显示后端 ISO（`2026-10-21T12:54:02+00:00`），比本地墙钟早 8 小时。

/** 后端事件的**字段名 → 中文**（结构化事件体不该以原始形态给用户看）。 */
export const EVENT_FIELD_ZH: Record<string, string> = {
  service: '服务', timestamp: '时间', event: '事件', level: '级别',
  workers_tracked: '跟踪 worker 数', workers_healthy: '健康 worker 数',
  workers_unhealthy: '异常 worker 数', workers_missing: '缺失 worker 数',
  status: '状态', reason: '原因', detail: '详情', message: '消息',
  task_id: '任务', agent: '执行者', capability: '能力', count: '条数',
  restarts: '重启次数', pid: '进程号', port: '端口', mode: '模式',
}

/** 后端来源的英文短句 → 中文（前端自产事件已直接写中文，这里只兜后端与历史文案）。 */
export const EVENT_TEXT_ZH: Record<string, string> = {
  'Evolution started': '进化轮次已启动',
  'Triggering evolution...': '正在触发进化…',
  'Failed - backend unreachable': '失败——后端不可达',
}

/** 事件**类别**徽章 → 中文（页面其余部分已全中文，留着 `GUARDIAN` 会显得是漏翻）。 */
export const EVENT_TYPE_ZH: Record<string, string> = {
  scale: '扩容', crash: '崩溃', recovery: '恢复',
  guardian: '守护进程', evolution: '进化', llm: '模型',
}

/** 事件类别 → 中文；未知类别原样返回（不编）。 */
export function eventTypeLabel(type: unknown): string {
  const t = String(type ?? '').trim()
  if (!t) return ''
  return EVENT_TYPE_ZH[t.toLowerCase()] || t
}

/** 事件消息 → 一行可读中文；结构化事件体逐字段读，被截断就如实标注。 */
export function readableEventMessage(msg: unknown): string {
  const s = String(msg ?? '').trim()
  if (!s) return s
  if (!s.startsWith('{')) return EVENT_TEXT_ZH[s] ?? s
  const closed = /\}\s*$/.test(s)
  const body = s.replace(/^\{+/, '').replace(/\}+$/, '')
  const pairs: string[] = []
  const re = /"([A-Za-z_][\w]*)"\s*:\s*("(?:[^"\\]|\\.)*"|[-+0-9.eE]+|true|false|null)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(body)) !== null) {
    let v = m[2]
    if (v.startsWith('"')) v = v.slice(1, -1)
    pairs.push(`${EVENT_FIELD_ZH[m[1]] || m[1]} ${v}`)
  }
  const tail = closed ? '' : '（事件体过长，后端已截断）'
  // 逐字段读完后统一把时间戳换成本地墙钟：写入方各自格式化（带 +00:00 或裸串），
  // 不归一就会出现"行头本地时间 / 事件体 UTC"相差 8 小时的同一行自相矛盾。
  if (pairs.length) return localizeTimestamps(pairs.join(' · ')) + tail
  return `结构化事件（未能逐字段解析）：${localizeTimestamps(body.slice(0, 120))}${tail}`
}

/** 事件时间 → **浏览器本地墙钟**（`MM-DD HH:MM:SS`）；解析不了就原样返回，不编。 */
export function formatEventTime(ts: unknown): string {
  const s = String(ts ?? '').trim()
  if (!s) return ''
  if (/^\d{1,2}:\d{2}(:\d{2})?$/.test(s)) return s          // 已经是时钟串
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}


/* ── 事件体里的时间戳归一 ──────────────────────────────────────────────
 * 实机反例（2026-10-21 22:01，健康页事件时间线）：
 *   行头显示 `10-21 22:01:49`（本地），事件体里却写
 *   「服务 worker_guardian · 时间 2026-10-21T14:01:49.686895+00:00 · 跟踪 worker 数 11」
 *   —— 同一条事件里两个时间差 8 小时，用户无从判断该信哪个。
 *
 * 根因：事件体的时间戳由各写入方自己格式化，全仓约 20 处用
 * `datetime.now(timezone.utc).isoformat()`（带 +00:00），另有 `strftime(...)`（裸串=本地墙钟）。
 * 逐个改写入方风险大且会漏掉未来的写入方，所以**统一在展示层归一**：
 * 不改存量数据（下游仍按原样解析），只把展示文本里的时间戳换成用户所在时区的墙钟。
 *
 * 两种形态分别处理，避免猜时区：
 *   · 带 `Z` 或 `±hh:mm` 偏移 → 明确是 UTC/带偏移，用 Date 换算成本地墙钟；
 *   · 裸串（无偏移）→ 本仓写入方用 `strftime` 生成，就是本地墙钟，只改格式不换算。
 */
const ISO_ANY =
  /\d{4}-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?/g

const pad2 = (n: number) => String(n).padStart(2, '0')

/**
 * 把一段文本里的时间戳换成本地墙钟 `MM-DD HH:MM:SS`。
 *
 * 单遍扫描 + 显式范围校验：不合法的时间（`2026-13-45T99:99:99Z` 之类）**原样保留**，
 * 绝不走"只改格式"的旁路把它粉饰成看起来正常的 `13-45 99:99:99`——坏数据要看得见。
 * 裸串（无偏移）按本仓约定即本地墙钟，只改格式不做换算；带 `Z`/偏移的用 Date 换算。
 */
export function localizeTimestamps(text: string): string {
  if (!text) return text
  return text.replace(
    ISO_ANY,
    (raw, mo: string, da: string, hh: string, mi: string, ss: string, frac: string | undefined, off: string | undefined) => {
      if (+mo < 1 || +mo > 12 || +da < 1 || +da > 31) return raw
      if (+hh > 23 || +mi > 59 || +ss > 59) return raw
      if (!off) return `${mo}-${da} ${hh}:${mi}:${ss}`
      const ms = frac ? `.${frac.slice(0, 3).padEnd(3, '0')}` : ''
      const d = new Date(`${raw.slice(0, 10)}T${hh}:${mi}:${ss}${ms}${off}`)
      if (Number.isNaN(d.getTime())) return raw
      return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} `
        + `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
    },
  )
}
