// P1-2：正文首屏"数据卡"的取数口径与卡头文案（纯函数，可在 Node 里直接断言）。
//
// 实机反例 ①（2026-10-21 真机复验 README P1-2）：卡片标题写
//   「关键数据速览  取自报告正文【财务对照】表（指标 × 期间 × 口径 × 来源）」
// 卡片内容却是 检索步总数 / 有效覆盖率 / 降级执行步 / 零来源步（『数据来源摘要』表，
// 表头只有「指标 | 值」），且最后一格被截成 `1（由非检索模型完成，来源可能为…`。
// 原因：判定太宽（表头含"指标"即算对照表）+ 卡头文案写死。
//
// 实机反例 ②（2026-10-22 真机复验，任务 ui-8b28c95ab1）：正文没有财务对照表，只有
//   `| 年度 | 数值 | 单位 | 报表名称 | 行项目 | 页码/表格编号 | 原始来源 URL | 状态 |`
// 的**指标缺口表**。旧实现仍然判成 `metrics`，且 kIdx 落在「数值」列上，于是卡片长成
//   「未获取（缺口）」/「人民币（单位待核）」×2 —— 卡头冒充『财务对照』、指标名与期间全丢、
//   键值还错列（名字列是「行项目」，值列是「数值」）。
// 现在：名字列只在 指标/科目/行项目/项目/名称/条目 里取；对照表必须同时有名字列、数值列
//   与（表头期间列 或 口径列）；只有缺口表时按**缺口**如实标注来源。

export interface TopStat { k: string; v: string }

/** 结论卡的**来源**：
 *  `metrics`=报告正文的『财务对照』表（指标 × 期间 × 口径 × 来源）；
 *  `metrics_gap`=正文只有**指标缺口表**（本期指标全部未获取到披露值），按缺口如实标注；
 *  `first_table`=退回到正文里第一张表（**未必是财务/结论表**，标签必须如实说明）；
 *  `none`=正文没有可提取的表。 */
export type TopStatsSource = 'metrics' | 'metrics_gap' | 'first_table' | 'none'

/** 结论卡解析结果：数值卡 + 来源 + **用到的期间** + 表内单位。 */
export interface TopStatsResult {
  stats: TopStat[]
  source: TopStatsSource
  periods: string[]
  unit: string
}

const PERIOD_HEADER_RE = /^(20\d{2})\s*(?:年|年度|年报|A|H[12]|Q[1-4])?$/
/** 行级期间单元格（`| 年度 |` 列里的 `2023` / `2024年`）。 */
const YEAR_CELL_RE = /^(19|20)\d{2}\s*(?:年|年度)?$/
/** 指标名/行项目列的候选表头（**按优先级**；数值列绝不在此列）。 */
const NAME_COL_RES = [/^指标/, /^科目/, /行项目/, /^项目/, /名称/, /条目/]
/** 数值列的候选表头（按优先级）。 */
const VALUE_COL_RES = [/^数值/, /^值$/, /金额/]

/** 表头单元格 → 期间标签（`2024`/`2024年` → `2024年`）；不是期间列返回空串。 */
export function periodOfHeader(cell: unknown): string {
  const c = String(cell ?? '').trim().replace(/\*\*/g, '')
    .replace(/[（(][^）)]*[）)]/g, '').replace(/\s+/g, '')
  const m = PERIOD_HEADER_RE.exec(c)
  return m ? `${m[1]}年` : ''
}

/** 正文里声明的金额单位（`单位：亿元`）；读不到就是空串，不猜。 */
export function reportUnitOf(md: string): string {
  const m = /单位\s*[:：]\s*([^\s；;，,。、）)]{1,8})/.exec(String(md || ''))
  return m ? m[1].trim() : ''
}

/** **指标名/行项目**列下标；没有名字列返回 -1。
 *
 *  这一步是实机反例 ② 的关键：旧实现用「指标|数值」找名字列，遇到
 *  `| 年度 | 数值 | 单位 | … | 行项目 | … |` 就把**数值列**当成了名字列。
 */
export function nameColOf(hdr: unknown): number {
  const cells = (Array.isArray(hdr) ? hdr : []).map(c => String(c ?? '').trim())
  for (const re of NAME_COL_RES) {
    const i = cells.findIndex(h => re.test(h))
    if (i >= 0) return i
  }
  return -1
}

/** **数值**列下标；`exclude` 用来避开已被认作名字列的列。 */
export function valueColOf(hdr: unknown, exclude = -1): number {
  const cells = (Array.isArray(hdr) ? hdr : []).map(c => String(c ?? '').trim())
  for (const re of VALUE_COL_RES) {
    const i = cells.findIndex((h, idx) => idx !== exclude && re.test(h))
    if (i >= 0) return i
  }
  return -1
}

/** 行级期间列下标：整列取值都是年份（`| 年度 | … |` 的「年度」列）。 */
export function rowPeriodColOf(
  hdr: unknown, rows: string[][], skip: number[] = [],
): number {
  const cells = (Array.isArray(hdr) ? hdr : []).map(c => String(c ?? '').trim())
  for (let i = 0; i < cells.length; i++) {
    if (skip.includes(i)) continue
    const vals = (rows || []).map(r => String((r || [])[i] ?? '').trim()).filter(Boolean)
    if (vals.length && vals.every(v => YEAR_CELL_RE.test(v))) return i
  }
  return -1
}

/** 这张表是不是『财务对照』表（指标 × 期间 × 口径 × 来源）。
 *
 *  必须同时有**名字列**与（表头期间列 或（数值列 + 口径列））：
 *   - 只含"指标 | 值"的两列表**不是**（那是『数据来源摘要』）——实机反例 ①；
 *   - `| 年度 | 数值 | 单位 | … | 行项目 | … | 状态 |` 这类**缺口表**也不是
 *     （没有表头期间列、没有口径列）——实机反例 ②。
 *  对照表的数值通常直接落在期间列里（`| 指标 | 2023 | 2024 | 口径 | 来源 |`），
 *  所以"有期间列"本身就是数值列的证据，不能强求表头里有「数值」二字。
 */
export function isComparisonTableHeader(hdr: unknown): boolean {
  const cells = (Array.isArray(hdr) ? hdr : []).map(c => String(c ?? '').trim())
  const kIdx = nameColOf(cells)
  if (kIdx < 0) return false
  const hasPeriod = cells.some((h, i) => i !== kIdx && !!periodOfHeader(h))
  if (hasPeriod) return true
  if (valueColOf(cells, kIdx) < 0) return false
  return /口径/.test(cells.join(' '))
}

/** 这张表是不是**指标缺口表**（有名字列、数值列与状态列）。
 *
 *  正文里没有财务对照表时，用它如实说明"哪些指标本次是缺口"，
 *  卡头必须写明是缺口表，**不得**冒充『财务对照』表。
 */
export function isGapIndicatorsTable(hdr: unknown): boolean {
  const cells = (Array.isArray(hdr) ? hdr : []).map(c => String(c ?? '').trim())
  const kIdx = nameColOf(cells)
  if (kIdx < 0) return false
  if (valueColOf(cells, kIdx) < 0) return false
  return cells.some(h => h.includes('状态'))
}

/** 表 → 结论卡：**明确年份**、最新期间在前，不裸取首数值列。 */
export function topStatsOfTable(
  table: string[][], maxCards = 8,
): { stats: TopStat[]; periods: string[] } {
  const hdr = ((table && table[0]) || []).map(h => String(h ?? '').trim())
  const rows = (table || []).slice(1)
  if (!hdr.length || !rows.length) return { stats: [], periods: [] }
  // 名字列：优先 指标/科目/行项目/项目/名称；都没有才退回第一列
  // （**绝不**把「数值」列当名字列——实机反例 ②）。
  let kIdx = nameColOf(hdr)
  if (kIdx < 0) kIdx = 0
  let periodCols = hdr
    .map((h, i): [number, string] => [i, periodOfHeader(h)])
    .filter(([i, p]) => !!p && i !== kIdx)
  if (periodCols.length) {
    periodCols = [...periodCols].sort((a, b) => (a[1] < b[1] ? 1 : a[1] > b[1] ? -1 : 0))
    const periods = periodCols.map(([, p]) => p)
    const stats: TopStat[] = []
    for (const r of rows) {
      const name = (r.length > kIdx ? r[kIdx] : '').slice(0, 24)
      for (const [i, p] of periodCols) {
        const v = r.length > i ? String(r[i] ?? '').trim() : ''
        if (v && v !== '—') {
          stats.push({ k: `${name} · ${p}`, v: v.slice(0, 32) })
          if (stats.length >= maxCards) return { stats, periods }
        }
      }
    }
    return { stats, periods }
  }
  const vIdx = valueColOf(hdr, kIdx)
  const realV = vIdx >= 0 ? vIdx : (kIdx === 0 ? Math.min(1, hdr.length - 1)
                                                 : Math.min(kIdx + 1, hdr.length - 1))
  return {
    stats: rows
      .filter(r => r.length > realV && String(r[realV] ?? '').trim())
      .map(r => ({ k: (r.length > kIdx ? r[kIdx] : '').slice(0, 24),
                   v: String(r[realV]).slice(0, 32) })),
    periods: [],
  }
}

/** 指标缺口表 → 结论卡：名字列作键（带行级期间），数值列作值。 */
export function gapStatsOfTable(
  table: string[][], maxCards = 8,
): { stats: TopStat[]; periods: string[] } {
  const hdr = ((table && table[0]) || []).map(h => String(h ?? '').trim())
  const rows = (table || []).slice(1)
  const kIdx = nameColOf(hdr)
  const vIdx = valueColOf(hdr, kIdx)
  if (kIdx < 0 || vIdx < 0) return { stats: [], periods: [] }
  const pIdx = rowPeriodColOf(hdr, rows, [kIdx, vIdx])
  const stats: TopStat[] = []
  const periods: string[] = []
  for (const r of rows) {
    const name = String((r || [])[kIdx] ?? '').trim()
    const v = String((r || [])[vIdx] ?? '').trim()
    if (!name || !v) continue
    const p = pIdx >= 0 ? String(r[pIdx] ?? '').trim() : ''
    // 行级期间的书写统一成 `2023年`（表格里常写裸 `2023`）
    const pLabel = p ? (/^(19|20)\d{2}$/.test(p) ? `${p}年` : p.replace(/年度?$/, '年')) : ''
    const label = pLabel ? `${name} · ${pLabel}` : name
    if (pLabel && !periods.includes(pLabel)) periods.push(pLabel)
    stats.push({ k: label.slice(0, 40), v: v.slice(0, 32) })
    if (stats.length >= maxCards) break
  }
  return { stats, periods }
}

/** 正文表格 → 结论卡（键截 24 字、值截 32 字）。
 *
 *  与分享页 `web_ui._share_page_structured` 同口径：**优先取『财务对照』表**
 *  （`isComparisonTableHeader`）；没有对照表但有指标缺口表时按缺口标源；
 *  都没有才退回第一张，并由调用方按 `source` 如实标注来源，不把任何一张表都说成"结论"。
 */
export function parseTopStatsWithSource(md: string): TopStatsResult {
  const tables: string[][][] = []
  let cur: string[][] = []
  let header: string[] = []
  for (const line of String(md ?? '').split('\n')) {
    const t = line.trim()
    if (!t.startsWith('|')) {
      if (cur.length) { tables.push(cur); cur = [] }
      header = []
      continue
    }
    const cells = t.replace(/^\|+|\|+$/g, '').split('|').map(c => c.trim())
    if (cells.every(c => !c || /^[-:\s—]*$/.test(c))) continue
    if (!header.length) { header = cells; cur = [cells] } else if (cur.length < 5) cur.push(cells)
  }
  if (cur.length) tables.push(cur)
  const unit = reportUnitOf(md)
  if (!tables.length) return { stats: [], source: 'none', periods: [], unit }
  const metrics = tables.find(t => isComparisonTableHeader(t[0] || []))
  if (metrics) {
    const res = topStatsOfTable(metrics)
    if (res.stats.length) {
      return { stats: res.stats, source: 'metrics', periods: res.periods, unit }
    }
  }
  // 没有财务对照表：正文只有指标缺口表时，按**缺口**如实标注（不冒充结论）
  const gap = tables.find(t => isGapIndicatorsTable(t[0] || []))
  if (gap) {
    const g = gapStatsOfTable(gap)
    if (g.stats.length) {
      return { stats: g.stats, source: 'metrics_gap', periods: g.periods, unit }
    }
  }
  const chosen = tables[0]
  const res = topStatsOfTable(chosen)
  if (!res.stats.length) return { stats: [], source: 'none', periods: [], unit }
  return { stats: res.stats, source: 'first_table', periods: res.periods, unit }
}

/** 兼容包装：只要统计值（旧调用方）。 */
export function parseTopStats(md: string): TopStat[] {
  return parseTopStatsWithSource(md).stats
}

export interface TopStatsCardMeta { title: string; detail: string }

/** 卡头（标题 + 来源说明）与实际取到的表同源生成——内容是什么，卡头就说什么。 */
export function topStatsCardMeta(res: {
  source: TopStatsSource; periods?: string[]; unit?: string
}): TopStatsCardMeta {
  if (res.source === 'metrics') {
    const periods = res.periods || []
    return {
      title: '关键数据速览',
      detail: '取自报告正文『财务对照』表（指标 × 期间'
        + (periods.length ? `：${periods.join('、')}` : '')
        + ' × 口径 × 来源）'
        + (res.unit ? `；表内单位 ${res.unit}（行内自带单位的以其为准）` : ''),
    }
  }
  if (res.source === 'metrics_gap') {
    const periods = res.periods || []
    return {
      title: '关键指标（本次均为缺口）',
      detail: '正文没有『财务对照』表，这里取自正文指标缺口表（指标 × 期间 × 数值'
        + (periods.length ? `：${periods.join('、')}` : '')
        + '）：本次这些指标都未获取到披露值，缺口如实保留，不用估计值代替'
        + (res.unit ? `；表内单位 ${res.unit}` : ''),
    }
  }
  if (res.source === 'first_table') {
    return {
      title: '正文首表前 4 行',
      detail: '报告正文里没有『财务对照』表（指标 × 期间 × 口径 × 来源）：'
        + '这里原样列出正文第一张表的前几行，未判定为结论',
    }
  }
  return { title: '', detail: '' }
}
