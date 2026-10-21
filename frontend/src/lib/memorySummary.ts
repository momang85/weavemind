// S3（2026-10-22 真机核查）：记忆页「系统自述」把 **464 条对话**写成「464 项任务」——
// 与同页统计卡（464 对话条数 / 269 策略条数）和指标页（总任务数 570）三方矛盾；且该区块
// 旁写着「可直接用于发布到社交媒体」→ 对外口径错误。实机原文（recheck/memory.txt:40）：
//   「我是织光——一支运行在你本地的 AI 团队。我已完成了 464 项任务、沉淀了 269 条成功策略。…」
//
// 根因：自述由 LLM 生成（`/api/memory/summary`），同一数字的措辞不稳定——另一次加载
// 返回的是「我已积累464条对话、269条成功策略」。前端不能替模型背书，只能：
//   ①把自述里的数字主张**逐个挑出来**，与本页实际有的口径（/api/memory 记忆库条数）对照；
//   ②对不上的**如实告警**，对得上的说明"已核对一致"；
//   ③页面只把本页确实取到的数字当权威口径；任务总数不在本页统计范围——写「未取得」，
//     不编造指标页的数字。
// 只对照、不改写用户读到的自述正文（那是模型输出，前端改写等于篡改来源）。

export interface MemoryCounts {
  conversations: number
  strategies: number
}

export type SummaryClaimKind = 'tasks' | 'conversations' | 'strategies'

export interface SummaryClaim {
  kind: SummaryClaimKind
  value: number
  /** 原文里的短语（如「464 项任务」） */
  phrase: string
}

export interface SummaryAudit {
  claims: SummaryClaim[]
  /** 与本页口径对不上 / 无法核对的告警（中文，逐条） */
  warnings: string[]
  /** 已核对一致的确认（warnings 为空时给） */
  confirmations: string[]
  /** 权威口径一行（页面下注，数字只来自本页实取） */
  authorityNote: string
  /** 对外发布口径提示（替换「可直接用于发布到社交媒体」） */
  publishNote: string
  /** 数字是否全部核对一致 */
  trustworthy: boolean
}

const CLAIM_RE = /(\d[\d,]*)\s*(项任务|个任务|条对话|次对话|条会话|条(?:成功)?策略|条记忆)/g

function kindOf(unit: string): SummaryClaimKind {
  if (/任务/.test(unit)) return 'tasks'
  if (/对话|会话|记忆/.test(unit)) return 'conversations'
  return 'strategies'
}

/** 本页权威口径行：数字只写本页实取的值；任务数不在本页范围 → 明说未取得。 */
export function memoryAuthorityNote(counts: MemoryCounts): string {
  const conv = counts.conversations > 0 ? `${counts.conversations} 条` : '未取得'
  const strat = counts.strategies > 0 ? `${counts.strategies} 条` : '未取得'
  return '本页权威口径（取自 /api/memory 记忆库条数，与上方统计卡同源）：'
    + `对话 ${conv}、策略 ${strat}；「任务」总数不在本页统计范围（见「指标看板」），本页不推断。`
}

/** 自述正文 + 本页实取条数 → 数字主张对照与告警（纯函数）。 */
export function auditMemorySummary(summary: string, counts: MemoryCounts): SummaryAudit {
  const claims: SummaryClaim[] = []
  const seen = new Set<string>()
  const text = String(summary ?? '')
  for (const m of text.matchAll(CLAIM_RE)) {
    const value = Number(m[1].replace(/,/g, ''))
    const kind = kindOf(m[2])
    const phrase = `${m[1]} ${m[2]}`
    const key = `${kind}:${value}`
    if (!seen.has(key)) {
      seen.add(key)
      claims.push({ kind, value, phrase })
    }
  }

  const warnings: string[] = []
  const confirmations: string[] = []
  for (const c of claims) {
    if (c.kind === 'tasks') {
      // 本页没有任务数口径：能判断的只有"它恰好等于对话条数"这一种矛盾形态
      //（实机反例：464 = 对话条数 464，被写成「464 项任务」）。
      if (counts.conversations > 0 && c.value === counts.conversations && c.value !== counts.strategies) {
        warnings.push(
          `自述把「${c.value}」称为「${c.phrase}」，但本页口径里 ${c.value} 是「对话条数」`
          + `（记忆库对话集合的条数），不是任务数；该句由模型生成、未经核对，请勿按此口径对外引用。`,
        )
      } else {
        warnings.push(
          `自述称「${c.phrase}」，本页没有任务数口径可核对（本页只有对话与策略条数）；`
          + `该数字由模型生成、未经核对，对外引用前请先对照「指标看板」的总任务数。`,
        )
      }
    } else if (c.kind === 'conversations') {
      if (counts.conversations > 0 && c.value !== counts.conversations) {
        warnings.push(
          `自述称「${c.phrase}」，与本页记忆库实际条数（${counts.conversations} 条对话）不一致；`
          + `以本页统计卡为准。`,
        )
      } else if (counts.conversations > 0) {
        confirmations.push(`自述的「${c.phrase}」与本页记忆库条数（${counts.conversations} 条对话）一致。`)
      }
    } else {
      if (counts.strategies > 0 && c.value !== counts.strategies) {
        warnings.push(
          `自述称「${c.phrase}」，与本页记忆库实际条数（${counts.strategies} 条策略）不一致；`
          + `以本页统计卡为准。`,
        )
      } else if (counts.strategies > 0) {
        confirmations.push(`自述的「${c.phrase}」与本页记忆库条数（${counts.strategies} 条策略）一致。`)
      }
    }
  }

  const trustworthy = claims.length > 0 && warnings.length === 0
  const authorityNote = memoryAuthorityNote(counts)
  const publishNote = warnings.length > 0
    ? '该自述由 LLM 生成，含未经核对的数字（见上方提示），不可直接用于对外发布；'
      + '对外引用前请以本页统计卡与「指标看板」核对。'
    : trustworthy
      ? '该自述由 LLM 基于记忆库生成，其中的数字已与本页统计卡核对一致；对外发布前仍建议人工复核。'
      : '该自述由 LLM 基于记忆库生成，未包含可与本页核对的数字；'
        + '对外发布前请人工复核措辞与事实，本页不代替复核结论。'

  return { claims, warnings, confirmations, authorityNote, publishNote, trustworthy }
}
