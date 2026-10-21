// S1（2026-10-22 真机核查）：Skill 卡「自动沉淀教训」正文曾用
// `String(l.issue || '').slice(0, 80)` **硬截断 80 字且不加省略号**，把一段完整的
// 教训切成断句当完整内容直出。实机反例（/api/skills，id 77b09c3a）原文 168 字：
//   「来源声明检查 17 条，虚假标注 17 条（与换算日期、数值逐字一致、中属于其他主体的数值、
//    的分级判定标准、」列填写对应 [n] 编号：）；建议补录域名媒体映射：主流财经；…」
// 页面上只显示到「…建议补录域名媒体映射」——括号未闭合、句子断裂。
//
// 修法（截断类缺陷的统一原则）：①优先**不截断**——整段完整显示，按分号拆成多行
// （教训本来就是多条并列的问题清单）；②确实需要限长时（maxChars>0）截断必须加省略号，
// 并由调用方用 `truncated`/`shownLength` 向用户说明「已截断」。
//
// 另一实测事实：/api/skills 里部分教训的 `skill` 字段为空串（如 ui-768ef2586a 的三条），
// 它们会**同时出现在多张 Skill 卡片下**（真机 skills.txt 第 31 与 52 行逐字相同）。
// 这里如实把「未绑定」标注出来，不把通用教训冒充成本 Skill 的教训。

export type LessonScope = 'bound' | 'unbound' | 'unknown'

/**
 * 后端落库上限（只读确认）：`skill_registry.py:147` 写入时执行 `str(issue)[:300]`，
 * 截断处**不带省略号**，所以页面上看到的 300 字教训可能本身就不是完整原文。
 * 前端拿不到被截掉的尾部，也不该把它冒充成完整内容。
 */
export const LESSON_STORAGE_CAP = 300

const SENTENCE_TAIL = /[。！？…」』）)\]】]$/

/** 正文是否"像被落库截断"：达到上限字数且结尾不是句末标点。 */
export function lessonTailCapped(text: string): boolean {
  const t = String(text ?? '').trim()
  return t.length >= LESSON_STORAGE_CAP && !SENTENCE_TAIL.test(t)
}

export interface SkillLessonView {
  /** 完整正文（不截断时的显示原文） */
  fullText: string
  /** 按分号/换行拆出的并列段落（段内保留原文，不二次截断） */
  segments: string[]
  /** 原文字符数 */
  length: number
  /** 是否被截断（maxChars>0 且超长时为 true） */
  truncated: boolean
  /** 实际展示的字符数（含省略号） */
  shownLength: number
  scope: LessonScope
  /** 绑定口径说明（bound 时为空串） */
  scopeNote: string
  /** 结尾疑似被后端落库上限截掉（true 时 `tailNote` 非空） */
  tailCapped: boolean
  /** 对"读到的可能不是完整原文"的如实说明 */
  tailNote: string
}

/** 按分号（中英文）与换行拆并列段；空段丢弃，段内不做任何改写。 */
export function splitLessonSegments(issue: string): string[] {
  return String(issue ?? '')
    .split(/[；;\n]+/)
    .map(s => s.trim())
    .filter(Boolean)
}

/** 教训正文 → 展示视图。maxChars<=0（默认）表示不截断。 */
export function skillLessonView(
  lesson: { issue?: unknown; skill?: unknown } | null | undefined,
  maxChars = 0,
): SkillLessonView {
  const fullText = String(lesson?.issue ?? '').trim()
  const scope: LessonScope = lesson === null || lesson === undefined || !('skill' in lesson)
    ? 'unknown'
    : String(lesson.skill ?? '').trim() === ''
      ? 'unbound'
      : 'bound'
  const scopeNote = scope === 'unbound'
    ? '未绑定具体 Skill（后端 skill 字段为空）：同一条教训会出现在多张 Skill 卡片下，不代表只属于本 Skill'
    : ''
  const capped = lessonTailCapped(fullText)
  const tailNote = capped
    ? '（原文即止于此：教训落库时被后端按 300 字截断且未加省略号，前端无法补全后半句）'
    : ''
  if (maxChars > 0 && fullText.length > maxChars) {
    const text = fullText.slice(0, maxChars - 1) + '…'
    return {
      fullText,
      segments: splitLessonSegments(text),
      length: fullText.length,
      truncated: true,
      shownLength: text.length,
      scope,
      scopeNote,
      tailCapped: capped,
      tailNote,
    }
  }
  return {
    fullText,
    segments: splitLessonSegments(fullText),
    length: fullText.length,
    truncated: false,
    shownLength: fullText.length,
    scope,
    scopeNote,
    tailCapped: capped,
    tailNote,
  }
}

/** 截断说明（仅 truncated 时非空）：告诉用户读到的不是全文。 */
export function lessonTruncatedNote(view: SkillLessonView): string {
  return view.truncated ? `（已截断，全文 ${view.length} 字）` : ''
}
