import { useState, useEffect, useMemo, useRef, memo } from 'react'
import { createPortal } from 'react-dom'
import type { ReactNode } from 'react'
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { PrismLight as SyntaxHighlighter } from 'react-syntax-highlighter'
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python'
import javascript from 'react-syntax-highlighter/dist/esm/languages/prism/javascript'
import typescript from 'react-syntax-highlighter/dist/esm/languages/prism/typescript'
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json'
import markdown from 'react-syntax-highlighter/dist/esm/languages/prism/markdown'
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash'
SyntaxHighlighter.registerLanguage('python', python)
SyntaxHighlighter.registerLanguage('py', python)
SyntaxHighlighter.registerLanguage('javascript', javascript)
SyntaxHighlighter.registerLanguage('js', javascript)
SyntaxHighlighter.registerLanguage('typescript', typescript)
SyntaxHighlighter.registerLanguage('ts', typescript)
SyntaxHighlighter.registerLanguage('json', json)
SyntaxHighlighter.registerLanguage('markdown', markdown)
SyntaxHighlighter.registerLanguage('md', markdown)
SyntaxHighlighter.registerLanguage('bash', bash)
SyntaxHighlighter.registerLanguage('shell', bash)
SyntaxHighlighter.registerLanguage('sh', bash)
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
// types used: TaskReport

import { useTaskStore } from '../stores/useTaskStore'
import {
  FileDown, Package, ScrollText, Clock, CheckCircle2, AlertTriangle,
  ChevronDown, ChevronRight, Award, Zap, Download, ExternalLink, Play,
  Share2, Link2, Copy, Check, X, Trash2, CalendarClock, ListTree, Quote, Fingerprint,
} from 'lucide-react'
import { WorkingPaperPanel } from './WorkingPaperPanel'
import ResearchBriefPanel from './ResearchBriefPanel'

/* ===================== 报告结构化解析（纯函数，无新增依赖） ===================== */

interface TocEntry { id: string; text: string; level: 1 | 2 | 3 }
interface FreshnessInfo { text: string }
interface SourceItem { title: string; url: string; domain: string }
interface SourcesInfo { heading: string; items: SourceItem[] }
interface SourcesResult { rest: string; sources: SourcesInfo | null; sectionText: string | null }
interface DisclaimerResult { rest: string; disclaimer: string | null }

/** 剥离最外层 ```markdown ... ``` / ``` ... ``` / ~~~ ... ~~~ 围栏（仅整体包裹时） */
export function stripOuterFence(md: string): string {
  const trimmed = md.trim()
  const m = /^```[a-zA-Z]*[ \t]*\r?\n([\s\S]*?)\r?\n```[a-zA-Z]*[ \t]*$/.exec(trimmed)
    || /^~~~[a-zA-Z]*[ \t]*\r?\n([\s\S]*?)\r?\n~~~[a-zA-Z]*[ \t]*$/.exec(trimmed)
  return m ? m[1] : md
}

/** 标记围栏代码块内的行，避免把代码内容误当标题/来源解析 */
function fenceMask(lines: string[]): boolean[] {
  const mask = new Array<boolean>(lines.length).fill(false)
  let inFence = false
  for (let i = 0; i < lines.length; i++) {
    if (/^\s*(```|~~~)/.test(lines[i])) {
      inFence = !inFence
      mask[i] = true
    } else {
      mask[i] = inFence
    }
  }
  return mask
}

/** 从 React 节点提取纯文本（用于标题锚点） */
function nodeToText(node: ReactNode): string {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(nodeToText).join('')
  if (typeof node === 'object' && 'props' in node && node.props) {
    return nodeToText((node as { props: { children?: ReactNode } }).props.children)
  }
  return ''
}

/** 标题纯文本：去掉内联链接与加粗等 markdown 标记 */
function plainHeading(text: string): string {
  return text
    .replace(/\[([^\]]+)\]\([^)\s]+\)/g, '$1')
    .replace(/[*_`~]/g, '')
    .trim()
}

/** 生成稳定的标题锚点 id（中文友好） */
function slugify(text: string): string {
  const slug = plainHeading(text).toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80)
  return slug || 'section'
}

/** 重复标题追加 -2/-3 保证 id 唯一 */
function assignHeadingIds(texts: string[]): string[] {
  const seen: Record<string, number> = {}
  return texts.map(text => {
    const slug = slugify(text)
    const n = (seen[slug] = (seen[slug] || 0) + 1)
    return n > 1 ? `${slug}-${n}` : slug
  })
}

/** 从 markdown 提取 #/##/### 目录大纲 */
function parseToc(md: string): TocEntry[] {
  const raw: Array<{ text: string; level: 1 | 2 | 3 }> = []
  const mask = fenceMask(md.split('\n'))
  md.split('\n').forEach((line, i) => {
    if (mask[i]) return
    const m = /^(#{1,3})\s+(.+?)\s*#*\s*$/.exec(line)
    if (!m) return
    const text = plainHeading(m[2])
    if (text) raw.push({ text, level: m[1].length as 1 | 2 | 3 })
  })
  const ids = assignHeadingIds(raw.map(r => r.text))
  return raw.map((r, i) => ({ ...r, id: ids[i] }))
}

/** 提取“数据时效”信息（标题区块或含数据截止/快照时间的段落） */
function parseFreshness(md: string): FreshnessInfo | null {
  const lines = md.split('\n')
  const mask = fenceMask(lines)
  for (let i = 0; i < lines.length; i++) {
    if (mask[i]) continue
    const m = /^(#{1,6})\s*(.+)$/.exec(lines[i].trim())
    if (m && /数据时效|数据截止时间/.test(m[2])) {
      const section: string[] = []
      for (let j = i + 1; j < lines.length; j++) {
        if (!mask[j] && /^#{1,6}\s+/.test(lines[j])) break
        const t = lines[j].trim()
        if (t && !/^\s*(```|~~~)/.test(t)) section.push(t.replace(/^[-*•]\s+/, ''))
      }
      const text = section.join(' ').replace(/[*_`]/g, '').replace(/\s+/g, ' ').trim()
      return text ? { text } : null
    }
  }
  for (let i = 0; i < lines.length; i++) {
    if (mask[i]) continue
    if (/数据截至|数据截止|快照时间/.test(lines[i])) {
      const text = lines[i]
        .replace(/^#{1,6}\s*/, '')
        .replace(/^[-*•]\s+/, '')
        .replace(/[*_`]/g, '')
        .trim()
      if (text) return { text }
    }
  }
  return null
}

const SOURCE_HEADING_WORDS = ['参考来源', '数据来源', '参考资料', '引用来源', '参考文献']

function isSourceHeading(text: string): boolean {
  const t = text.trim()
  return SOURCE_HEADING_WORDS.some(w => t.includes(w)) || /^(来源|References?|Sources?)$/i.test(t)
}

function hostnameOf(url: string): string {
  const candidates = [url, /^https?:\/\//i.test(url) ? '' : 'https://' + url]
  for (const c of candidates) {
    if (!c) continue
    try {
      return new URL(c).hostname.replace(/^www\./, '')
    } catch { /* 尝试下一种形态 */ }
  }
  const slash = url.indexOf('/')
  return slash > 0 ? url.slice(0, slash) : url
}

function isListPrefix(prefix: string): boolean {
  return /^(?:[-*•]\s*|\[\d{1,2}\]\s*|\d{1,3}[.、.)]\s*)+$/.test(prefix)
}

/** 解析来源清单行：[标题](URL)，支持 - / 1. / [1] 等前缀 */
function parseSourceItems(raw: string, mask: boolean[]): SourceItem[] {
  const items: SourceItem[] = []
  const lines = raw.split('\n')
  for (let k = 0; k < lines.length; k++) {
    if (mask[k]) continue
    const trimmed = lines[k].trim()
    if (!trimmed || /^!\[/.test(trimmed)) continue
    const m = trimmed.match(/\[([^\]]+)\]\(([^)\s]+)\)/)
    if (!m || m.index == null) continue
    const prefix = trimmed.slice(0, m.index).trim()
    if (prefix && !isListPrefix(prefix)) continue
    const title = m[1].replace(/[*_`]/g, '').trim()
    const url = m[2].trim()
    if (!title || !url) continue
    items.push({ title, url, domain: hostnameOf(url) })
  }
  return items
}

/** 提取文末参考来源/数据来源区块；无法结构化时原样保留 */
function parseSources(md: string): SourcesResult {
  const lines = md.split('\n')
  const mask = fenceMask(lines)
  let start = -1
  for (let i = 0; i < lines.length; i++) {
    if (mask[i]) continue
    const m = /^(#{1,6})\s*(.+)$/.exec(lines[i].trim())
    if (m && isSourceHeading(m[2])) { start = i; break }
  }
  if (start < 0) return { rest: md, sources: null, sectionText: null }

  let end = lines.length
  for (let j = start + 1; j < lines.length; j++) {
    if (!mask[j] && /^#{1,6}\s+/.test(lines[j])) { end = j; break }
  }
  const rawSection = lines.slice(start + 1, end).join('\n')
  const sectionMask = mask.slice(start + 1, end)
  const items = parseSourceItems(rawSection, sectionMask)
  const heading = lines[start].trim().replace(/^#{1,6}\s*/, '').trim()

  if (items.length === 0) {
    // 有来源区块但无法结构化 → 原样渲染，且不再把其中的 [1] 转成引用上标
    return { rest: md, sources: null, sectionText: rawSection }
  }
  const rest = [...lines.slice(0, start), ...lines.slice(end)].join('\n').trim()
  return { rest, sources: { heading, items }, sectionText: null }
}

/** 提取免责声明区块 */
function parseDisclaimer(md: string): DisclaimerResult {
  const lines = md.split('\n')
  const mask = fenceMask(lines)
  let start = -1
  for (let i = 0; i < lines.length; i++) {
    if (mask[i]) continue
    const m = /^(#{1,6})\s*(.+)$/.exec(lines[i].trim())
    if (m && /免责声明/.test(m[2])) { start = i; break }
  }
  if (start < 0) return { rest: md, disclaimer: null }

  let end = lines.length
  for (let j = start + 1; j < lines.length; j++) {
    if (!mask[j] && /^#{1,6}\s+/.test(lines[j])) { end = j; break }
  }
  const text = lines.slice(start + 1, end).join(' ')
    .replace(/\[([^\]]+)\]\([^)\s]+\)/g, '$1')
    .replace(/[*_`]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  if (!text) return { rest: md, disclaimer: null }
  const rest = [...lines.slice(0, start), ...lines.slice(end)].join('\n').trim()
  return { rest, disclaimer: text }
}

/* ───────────────── 结论卡 / 图表 / 证据指纹（F3a） ───────────────── */

interface TopStat { k: string; v: string }

/** 正文首张 Markdown 表格的前 4 行 → 结论卡。
 *  与分享页 `web_ui._share_page_structured` 同口径：列取表头含"指标"/"数值"者，
 *  缺省第 0/1 列；键截 24 字、值截 32 字。无表返回空数组（由调用方决定降级展示）。 */
export function parseTopStats(md: string): TopStat[] {
  let header: string[] = []
  const rows: string[][] = []
  for (const line of md.split('\n')) {
    const t = line.trim()
    if (!t.startsWith('|')) {
      if (header.length) break
      continue
    }
    const cells = t.replace(/^\|+|\|+$/g, '').split('|').map(c => c.trim())
    if (cells.every(c => !c || /^[-:\s—]*$/.test(c))) continue
    if (!header.length) { header = cells; continue }
    rows.push(cells)
    if (rows.length >= 4) break
  }
  if (!header.length || !rows.length) return []
  const kFind = header.findIndex(h => h.includes('指标') || h.includes('数值'))
  const vFind = header.findIndex(h => h.includes('数值'))
  const kIdx = kFind >= 0 ? kFind : 0
  const vIdx = vFind >= 0 ? vFind : Math.min(1, header.length - 1)
  return rows
    .filter(r => r.length > vIdx && r[vIdx].trim())
    .map(r => ({ k: (r.length > kIdx ? r[kIdx] : '').slice(0, 24), v: r[vIdx].slice(0, 32) }))
}

/** "补充图表（未达发布标准）"章节下的图片地址集合 —— 这些图不是发布级，需标"草稿级"。 */
export function draftChartSrcs(md: string): string[] {
  const out: string[] = []
  let draft = false
  for (const line of md.split('\n')) {
    const heading = line.match(/^#{1,6}\s+(.*)$/)
    if (heading) { draft = /补充图表|未达发布标准/.test(heading[1]); continue }
    if (!draft) continue
    for (const m of line.matchAll(/!\[[^\]]*\]\(([^)\s]+)/g)) {
      if (!out.includes(m[1])) out.push(m[1])
    }
  }
  return out
}

async function downloadViaBlob(url: string, name: string) {
  try {
    const res = await fetch(url)
    if (!res.ok) return
    const blob = await res.blob()
    const href = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = href
    a.download = name
    a.click()
    URL.revokeObjectURL(href)
  } catch { /* 下载失败保持原状，不改页面状态 */ }
}

async function openViaBlob(url: string) {
  try {
    const res = await fetch(url)
    if (!res.ok) return
    window.open(URL.createObjectURL(await res.blob()), '_blank')
  } catch { /* 打开失败保持原状 */ }
}

/** 报告内嵌图：图注（title/alt）+ 点击放大（Esc/遮罩关闭）+ 单图下载。 */
function ReportImage({ src, alt, title, draft, taskId }: {
  src?: string; alt?: string; title?: string; draft?: boolean; taskId?: string | null
}) {
  const [zoom, setZoom] = useState(false)
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null)
  useEffect(() => {
    if (!zoom) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setZoom(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [zoom])
  const raw = String(src || '')
  if (!raw) return null
  const caption = String(title || alt || '').trim()
  const name = raw.split('/').pop()?.split('?')[0] || 'chart.png'
  const url = raw.startsWith('http') || raw.startsWith('/')
    ? raw
    : (taskId ? `/files/${encodeURIComponent(taskId)}/${raw.split('/').map(encodeURIComponent).join('/')}` : raw)
  // 与后端 chart_qa.MAX_CANVAS_ASPECT=12 同一阈值：超高画布（历史异常产物，
  // 如实测 1038×121366）按容器缩放后只剩几像素宽，嵌在页里等于隐形——
  // 换成如实占位说明，源文件仍可下载。
  const aspectIssue = dims !== null && dims.w > 0 && dims.h / dims.w > 12
  return (
    <figure className="my-4 rounded-xl border border-slate-800 bg-slate-900/60 p-2">
      {aspectIssue && dims ? (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
          图片尺寸异常（{dims.w}×{dims.h}，高宽比 {(dims.h / dims.w).toFixed(1)}），
          按图表质检规则不在页面嵌入展示；源文件仍可下载。
        </div>
      ) : (
        <button type="button" onClick={() => setZoom(true)}
          title="点击放大（Esc/遮罩关闭）" className="block w-full cursor-zoom-in">
          <img src={url} alt={caption || '报告图表'} loading="lazy"
            onLoad={(e) => setDims({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
            className="mx-auto max-h-[420px] w-auto rounded-lg" />
        </button>
      )}
      <figcaption className="mt-2 flex flex-wrap items-center gap-2 px-1 pb-1">
        {draft && (
          <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-xs text-amber-400">草稿级</span>
        )}
        <span className="min-w-0 flex-1 truncate text-xs text-slate-400" title={caption}>
          {caption || name}
        </span>
        <button type="button" onClick={() => downloadViaBlob(url, name)}
          className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700">
          下载图片
        </button>
      </figcaption>
      {zoom && createPortal(
        <div role="dialog" aria-modal="true" onClick={() => setZoom(false)}
          className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-3 bg-slate-950/90 p-4">
          <img src={url} alt={caption || '报告图表'} onClick={e => e.stopPropagation()}
            className="max-h-[75vh] max-w-full rounded-lg bg-white/5 object-contain" />
          <div className="flex items-center gap-3" onClick={e => e.stopPropagation()}>
            <span className="max-w-[60vw] truncate text-xs text-slate-300">{caption || name}</span>
            <button type="button" onClick={() => downloadViaBlob(url, name)}
              className="rounded bg-slate-800 px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-700">
              下载图片
            </button>
            <button type="button" onClick={() => setZoom(false)}
              className="rounded bg-slate-800 px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-700">
              关闭（Esc）
            </button>
          </div>
        </div>,
        document.body,
      )}
    </figure>
  )
}

const CITATION_SEQ_RE = /(^|[^\[!\]\w])(\[\d{1,2}\](?:\s*\[\d{1,2}\])*)(?![(:\d])/gm

/** 将 [1]、[1][2] 编号引用转换为 cite: 链接（由 a 组件渲染上标徽章） */
function addCitationLinks(md: string, protectedSection?: string | null): string {
  const stash: string[] = []
  let protectedMd = md
  if (protectedSection) {
    protectedMd = protectedMd.replace(protectedSection, raw => `\u0000${stash.push(raw) - 1}\u0000`)
  }
  protectedMd = protectedMd
    .replace(/```[\s\S]*?```/g, raw => `\u0000${stash.push(raw) - 1}\u0000`)
    .replace(/`[^`\n]+`/g, raw => `\u0000${stash.push(raw) - 1}\u0000`)
  const withCites = protectedMd.replace(CITATION_SEQ_RE, (_match, before, seq) => {
    const nums = seq.match(/\d{1,2}/g) || []
    return `${before}[${nums.join(',')}](cite:${nums.join(',')})`
  })
  return withCites.replace(/\u0000(\d+)\u0000/g, (_match, i) => stash[Number(i)] ?? '')
}

/* ===================== 报告渲染子组件 ===================== */

// 模块级渲染期状态：ReactMarkdown 同步渲染、页面单实例，可避免每次渲染重建组件身份导致整棵树重挂载
let citationSources: SourceItem[] = []
let headingSeen: Record<string, number> = {}

function nextHeadingId(text: string): string {
  const slug = slugify(text)
  const n = (headingSeen[slug] = (headingSeen[slug] || 0) + 1)
  return n > 1 ? `${slug}-${n}` : slug
}

interface MarkdownComponentProps {
  className?: string
  children?: ReactNode
  href?: string
  node?: unknown
}

function scrollToId(id: string) {
  // smooth 滚动在某些 WebView 中静默无效：先尝试 smooth，
  // 700ms 后滚动位置未变化则回退为瞬时滚动，保证一定到位
  const el = document.getElementById(id)
  if (!el) return
  const scroller = document.querySelector('main') as HTMLElement | null
  const before = scroller ? scroller.scrollTop : 0
  el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  window.setTimeout(() => {
    const after = scroller ? scroller.scrollTop : 0
    if (Math.abs(after - before) < 4) {
      el.scrollIntoView({ block: 'start' })
    }
  }, 700)
}

function CitationBadges({ nums, sources }: { nums: number[]; sources: SourceItem[] }) {
  const scrollToSources = () => scrollToId('sources')
  return (
    <span className="mx-0.5 inline-flex items-center gap-0.5 align-super">
      {nums.map(n => {
        const src = sources[n - 1]
        const tip = src ? `${src.title} — ${src.domain}` : `未找到对应来源 ${n}`
        return (
          <button key={n} type="button" title={tip}
            onClick={src ? scrollToSources : undefined}
            className={`rounded px-1 py-px text-xs font-semibold leading-none transition-colors ${
              src
                ? 'border border-cyan-500/25 bg-cyan-500/15 text-cyan-400 hover:bg-cyan-500/30 hover:text-cyan-300'
                : 'border border-slate-700 bg-slate-800/60 text-slate-500'
            }`}>
            {n}
          </button>
        )
      })}
    </span>
  )
}

function HeadingComponent({ level, children }: { level: 1 | 2 | 3 | 4 | 5 | 6; children?: ReactNode }) {
  const id = nextHeadingId(nodeToText(children))
  const Tag = `h${level}` as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'
  return <Tag id={id} className="scroll-mt-20">{children}</Tag>
}

function H1(props: { children?: ReactNode }) { return <HeadingComponent level={1} {...props} /> }
function H2(props: { children?: ReactNode }) { return <HeadingComponent level={2} {...props} /> }
function H3(props: { children?: ReactNode }) { return <HeadingComponent level={3} {...props} /> }
function H4(props: { children?: ReactNode }) { return <HeadingComponent level={4} {...props} /> }
function H5(props: { children?: ReactNode }) { return <HeadingComponent level={5} {...props} /> }
function H6(props: { children?: ReactNode }) { return <HeadingComponent level={6} {...props} /> }

function AComponent({ href, children }: MarkdownComponentProps) {
  if (href && href.startsWith('cite:')) {
    const nums = href.slice(5).split(',').map(Number).filter(n => Number.isInteger(n) && n > 0)
    return <CitationBadges nums={nums} sources={citationSources} />
  }
  return (
    <a href={href} target="_blank" rel="noopener noreferrer"
      className="text-cyan-400 underline decoration-cyan-500/40 underline-offset-2 transition-colors hover:text-cyan-300">
      {children}
    </a>
  )
}

function CodeComponent({ className, children }: MarkdownComponentProps) {
  const match = /language-(\w+)/.exec(className || '')
  const inline = !match
  return !inline && match ? (
    <SyntaxHighlighter style={oneDark} language={match[1]} PreTag="div"
      customStyle={{ background: '#0f172a', borderRadius: '8px', padding: '16px', fontSize: '12px', margin: '8px 0' }}>
      {String(children).replace(/\n$/, '')}
    </SyntaxHighlighter>
  ) : (
    <code className={className}>{children}</code>
  )
}

const markdownComponents = {
  h1: H1,
  h2: H2,
  h3: H3,
  h4: H4,
  h5: H5,
  h6: H6,
  a: AComponent,
  code: CodeComponent,
}

export const ReportMarkdown = memo(function ReportMarkdown({ md, sources, taskId }: {
  md: string; sources: SourceItem[]; taskId?: string | null
}) {
  citationSources = sources
  headingSeen = {}
  // 图片组件需要知道哪些图落在"补充图表（未达发布标准）"之下，故按报告内容建一次映射
  const draftSrcs = useMemo(() => new Set(draftChartSrcs(md)), [md])
  const components = useMemo(() => ({
    ...markdownComponents,
    img: (props: { src?: string; alt?: string; title?: string }) => (
      <ReportImage {...props} taskId={taskId} draft={draftSrcs.has(String(props.src || ''))} />
    ),
  }), [draftSrcs, taskId])

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      urlTransform={(url) => (url.startsWith('cite:') ? url : defaultUrlTransform(url))}
      components={components}
    >
      {md}
    </ReactMarkdown>
  )
})

export default memo(function ReportViewer() {
  const report = useTaskStore(s => s.report)
  const logs = useTaskStore(s => s.logs)
  const currentTaskId = useTaskStore(s => s.currentTaskId)
  const demoMode = useTaskStore(s => s.demoMode)
  const taskIdForFiles = currentTaskId || report?.taskId || null
  // F3-B：人工修订后重取任务详情（正文/结构对象/导出版本都换到新版本）
  const reloadTask = async (tid: string) => {
    try {
      const res = await fetch(`/task/${tid}`)
      if (!res.ok) return
      const d = await res.json()
      const cur = useTaskStore.getState().report
      if (!cur) return
      useTaskStore.getState().setReport({
        ...cur,
        final_report: d.report || cur.final_report,
        research: d.research ?? cur.research,
        export: d.export ?? cur.export,
        acceptance: d.acceptance && Array.isArray(d.acceptance.gaps)
          ? { overall: d.acceptance.overall || d.status, gaps: d.acceptance.gaps }
          : cur.acceptance,
      })
    } catch { /* 重取失败保留旧视图，面板会显示提交结果 */ }
  }
  const [showLogs, setShowLogs] = useState(false)
  // E1 溯源页：POST /api/verify 的三档分类结果
  const [verifyData, setVerifyData] = useState<any>(null)
  const [verifyLoading, setVerifyLoading] = useState(false)
  const [verifyError, setVerifyError] = useState('')
  // T4：报告打开即自动拉取验收全量报告（四档常驻徽章行）
  const [accData, setAccData] = useState<any>(null)
  // S2：可重算底稿（明细 / 派生 / 缺口），供"关键数字定位到 fact、计算值看公式"
  const [paperData, setPaperData] = useState<any>(null)
  useEffect(() => {
    setAccData(null)
    if (!taskIdForFiles) return
    let cancelled = false
    fetch('/api/task/' + taskIdForFiles + '/acceptance')
      .then(r => (r.ok ? r.json() : null))
      .then(d => { if (!cancelled && d && !d.error) setAccData(d) })
      .catch(() => {})
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskIdForFiles])
  useEffect(() => {
    setPaperData(null)
    if (!taskIdForFiles) return
    let cancelled = false
    fetch('/api/task/' + taskIdForFiles + '/working_paper')
      .then(r => (r.ok ? r.json() : null))
      .then(d => { if (!cancelled && d && !d.error) setPaperData(d) })
      .catch(() => {})
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskIdForFiles])

  // F3a：验收事件流（反思重做/每轮验收可回放）+ 证据指纹折叠态
  const [timeline, setTimeline] = useState<any[] | null>(null)
  const [fingerprintOpen, setFingerprintOpen] = useState(false)
  useEffect(() => {
    setTimeline(null)
    if (!taskIdForFiles) return
    let cancelled = false
    fetch('/api/task/' + taskIdForFiles + '/acceptance/timeline')
      .then(r => (r.ok ? r.json() : null))
      .then(d => { if (!cancelled && Array.isArray(d?.events)) setTimeline(d.events) })
      .catch(() => { if (!cancelled) setTimeline([]) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskIdForFiles])
  const [expandedFiles, setExpandedFiles] = useState(false)
  const [runOutput, setRunOutput] = useState<Record<string, string>>({})
  const [running, setRunning] = useState<string | null>(null)
  // 报告分享：shareUrl 非空表示该任务已生成分享链接
  const [shareUrl, setShareUrl] = useState<string | null>(null)
  // 09-23：按当前采纳版本重新导出（无模型、确定性）——旧包保留，新包绑定当前版本
  const [repacking, setRepacking] = useState(false)
  const [repackMsg, setRepackMsg] = useState('')
  const [shareLoading, setShareLoading] = useState(false)
  const [shareDialogOpen, setShareDialogOpen] = useState(false)
  const [shareCopied, setShareCopied] = useState(false)
  const [shareError, setShareError] = useState('')
  const [sharePassword, setSharePassword] = useState('')
  const [shareTtlHours, setShareTtlHours] = useState('168') // 默认 7 天，上限 30 天
  const [shareProtected, setShareProtected] = useState(false)
  const shareTaskId = currentTaskId || report?.taskId || null

  const applyExpiry = (expiresAt?: string) => {
    if (!expiresAt) return
    const days = Math.round((new Date(expiresAt).getTime() - Date.now()) / 86400000)
    const hours = Math.min(720, Math.max(24, (days || 7) * 24))
    setShareTtlHours(String(hours))
  }

  // 刷新/切换任务后恢复“已分享”状态（GET /api/share/<task_id>）
  useEffect(() => {
    if (!shareTaskId) {
      setShareUrl(null)
      return
    }
    let cancelled = false
    fetch('/api/share/' + encodeURIComponent(shareTaskId))
      .then(r => r.json())
      .then(d => {
        if (!cancelled && d?.shared && (d.url || d.path)) {
          setShareUrl(d.url || window.location.origin + d.path)
          setShareProtected(Boolean(d.protected))
          applyExpiry(d.expires_at)
        } else if (!cancelled) {
          setShareUrl(null)
          setShareProtected(false)
        }
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [shareTaskId])

  const generateShare = async () => {
    if (!shareTaskId || shareLoading) return
    setShareLoading(true)
    setShareError('')
    try {
      const res = await fetch('/api/share', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          task_id: shareTaskId,
          password: sharePassword,
          ttl_hours: Number(shareTtlHours) || 168,
        }),
      })
      const d = await res.json()
      if (!res.ok || !d.token) throw new Error(d.error || '分享失败，请稍后重试')
      const url = d.url || window.location.origin + d.path
      setShareUrl(url)
      setShareProtected(Boolean(d.protected))
      applyExpiry(d.expires_at)
    } catch (e: any) {
      setShareError(e?.message || '分享失败，请稍后重试')
    } finally {
      setShareLoading(false)
    }
  }

  const createShare = () => {
    if (!shareTaskId || shareLoading) return
    setShareError('')
    setShareDialogOpen(true)
  }

  const revokeShare = async () => {
    if (!shareTaskId) return
    try {
      await fetch('/api/share/' + encodeURIComponent(shareTaskId), { method: 'DELETE' })
    } catch { /* 即使请求失败也清理本地状态 */ }
    setShareUrl(null)
    setShareDialogOpen(false)
    setSharePassword('')
    setShareProtected(false)
  }

  const copyShare = async () => {
    if (!shareUrl) return
    const done = () => {
      setShareCopied(true)
      setTimeout(() => setShareCopied(false), 2000)
    }
    try {
      await navigator.clipboard.writeText(shareUrl)
      done()
    } catch {
      // 剪贴板 API 不可用时回退到选中复制
      const ta = document.createElement('textarea')
      ta.value = shareUrl
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      done()
    }
  }

  const runFile = async (name: string) => {
    setRunning(name)
    try {
      const res = await fetch('/api/deliverable/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: name, task_id: taskIdForFiles || undefined }),
      })
      const d = await res.json()
      setRunOutput(prev => ({ ...prev, [name]: d.output || d.error || '(no output)' }))
    } catch (e: any) {
      setRunOutput(prev => ({ ...prev, [name]: '运行失败: ' + (e?.message || e) }))
    }
    setRunning(null)
  }

  // 文件可能受鉴权保护（未分享任务）：走带 Authorization 头的 fetch + Blob 打开/下载，
  // 避免 window.open 新标签页不带 token 而 401。
  const openFile = async (name: string) => {
    await openViaBlob(fileUrl(name))
  }

  const downloadFile = async (name: string) => {
    await downloadViaBlob(fileUrl(name), name.split('/').pop() || name)
  }

  // 任务完成自动滚动到报告顶部：report 引用变化（null→对象 / 切换任务 /
  // 查看历史报告）即平滑滚动；真实完成、demo、viewFullReport 三路径均经
  // store.report 变化触发。锚定报告顶部，不依赖异步 files/验收数据落地。
  const prevReportKey = useRef<string | null>(null)
  useEffect(() => {
    if (!report) { prevReportKey.current = null; return }
    const key = report.taskId || 'report'
    if (prevReportKey.current === key) return
    prevReportKey.current = key
    const raf = requestAnimationFrame(() => {
      scrollToId('report-viewer')
    })
    return () => cancelAnimationFrame(raf)
  }, [report])

  // 解析管线 useMemo：report 引用不变时不重跑
  // （原为渲染期直跑，每次日志推送/3s 轮询都全量重解析）
  const parsed = useMemo(() => {
    if (!report) return null
    const rawMd = stripOuterFence(report.final_report || '')
    const freshness = parseFreshness(rawMd)
    const sourcesResult = parseSources(rawMd)
    const disclaimerResult = parseDisclaimer(sourcesResult.rest)
    const bodyMd = addCitationLinks(disclaimerResult.rest, sourcesResult.sectionText)
    const toc = parseToc(disclaimerResult.rest)
    return {
      freshness,
      sourcesResult,
      disclaimerResult,
      bodyMd,
      toc,
      sourceItems: sourcesResult.sources?.items ?? [],
      // 结论卡取自正文首表（与分享页同口径；无表则空数组，由渲染端降级到验收数字）
      topStats: parseTopStats(rawMd),
    }
  }, [report])

  if (!report) return null

  const s = report.stats || { totalSteps: report.steps?.length ?? 0, successSteps: 0, failedSteps: 0, duration: 0 }
  const rate = s.totalSteps > 0 ? Math.round((s.successSteps / s.totalSteps) * 100) : 100
  // 验收器统计（与报告页可信度卡同一份数据；未加载时为 null，渲染端据此显示"未知"而非 0）
  const accTrace = accData?.checks?.number_traceability || null
  const { freshness, sourcesResult, disclaimerResult, bodyMd, toc, sourceItems, topStats } = parsed!

  const scrollToHeading = (id: string) => scrollToId(id)

  // 导出鉴权/故障分级（R0.3）：401 要登录、403 无权限——都不自动下载；
  // 只有"服务端不可用"这类故障才允许退本地缓存稿，且必须显式标注未验证。
  const exportBlocked = async (res: Response): Promise<boolean> => {
    if (res.status === 401) {
      window.alert('需登录后才能导出：请先登录再重试。')
      return true
    }
    if (res.status === 403) {
      window.alert('无权限导出该报告：请确认账号权限或报告归属。')
      return true
    }
    return false
  }

  // Markdown 导出优先走服务端：导出字节的 hash 与服务端导出清单一并落盘，
  // 并与 PDF 绑定同一个选中版本；服务端不可用（离线/旧版）时退回内存稿，
  // 此时文件名为"本地未验证副本"，并明确提示它没有清单绑定。
  const downloadMarkdown = async () => {
    if (taskIdForFiles) {
      try {
        const res = await fetch('/api/task/' + encodeURIComponent(taskIdForFiles) + '/report.md')
        if (await exportBlocked(res)) return
        if (res.ok) {
          const blob = await res.blob()
          const url = URL.createObjectURL(blob)
          const a = document.createElement('a')
          a.href = url
          a.download = taskIdForFiles + '.md'
          a.click()
          URL.revokeObjectURL(url)
          return
        }
      } catch { /* 服务端不可达 → 退本地缓存稿（下面标注） */ }
    }
    const blob = new Blob([report.final_report], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'report-本地未验证副本.md'
    a.click()
    URL.revokeObjectURL(url)
    window.alert('服务端导出不可用：已导出**本地未验证副本**（无版本绑定与验收清单，不得当作已通过）。')
  }

  const fileUrl = (name: string) => {
    // 逐段编码：整段 encodeURIComponent 会把 "charts/chart_1.png" 的斜杠编成 %2F，
    // 而 /files 路由不做 URL 解码 → 404（浏览器实测图片全挂）。
    const rel = name.split('/').map(encodeURIComponent).join('/')
    return taskIdForFiles ? '/files/' + encodeURIComponent(taskIdForFiles) + '/' + rel
                          : '/files/' + rel
  }

  const downloadPDF = async () => {
    if (taskIdForFiles) {
      try {
        const res = await fetch('/api/task/' + encodeURIComponent(taskIdForFiles) + '/pdf')
        if (await exportBlocked(res)) return
        if (!res.ok) throw new Error('PDF unavailable')
        const blob = await res.blob()
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = taskIdForFiles + '.pdf'
        a.click()
        URL.revokeObjectURL(url)
        return
      } catch { /* 服务端 PDF 不可用 → 浏览器打印（打印件会标版本/草稿或"未知"） */ }
    }
    const w = window.open('', '_blank')
    if (!w) return
    // 打印页正文优先取服务端导出的同一份选中版本（顺带拿到版本号/草稿标记，
    // 打印件因此可追溯到它是哪一版）；服务端不可用时退回内存稿，并在页脚
    // 明确写"版本未知（本地未验证副本）"，不冒充已验证交付。
    let printBody = report.final_report
    let versionId = ''
    let isDraft = false
    let bodyFromServer = false
    if (taskIdForFiles) {
      try {
        const res = await fetch('/api/task/' + encodeURIComponent(taskIdForFiles) + '/report.md')
        if (res.ok) {
          printBody = await res.text()
          versionId = res.headers.get('X-Report-Version-Id') || ''
          isDraft = res.headers.get('X-Report-Draft') === '1'
          bodyFromServer = true
        }
      } catch { /* 用内存稿（下面标注版本未知） */ }
    }
    w.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Report</title>
<style>body{font-family:system-ui;max-width:800px;margin:40px auto;padding:20px;color:#1a1a2e;line-height:1.8}
h1,h2{color:#16213e} pre{background:#f5f5f5;padding:16px;border-radius:8px;overflow-x:auto}
code{background:#f0f0f0;padding:2px 6px;border-radius:4px} table{border-collapse:collapse;width:100%}
th,td{border:1px solid #ddd;padding:8px;text-align:left} th{background:#16213e;color:#fff}
</style></head><body><div id="content"></div></body></html>`)
    w.document.close()
    // Convert markdown to HTML inline；先整体实体转义再套标签——
    // 报告正文是 LLM 聚合外部内容的产物，原样 innerHTML 会执行其中
    // 任意 HTML（存储型 XSS），转义后注入标签的来源只剩本函数自身
    const esc = (s: string) =>
      s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
       .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    const md = esc(printBody)
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      .replace(/^- (.+)$/gm, '<li>$1</li>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\n/g, '<br/>')
    w.document.getElementById('content')!.innerHTML = md
    {
      // 页脚必须说清"这是哪一版、能不能当已通过用"：拿不到服务端版本时
      // 显式写"版本未知（本地未验证副本）"，不留下"看起来正常"的打印件
      const foot = w.document.createElement('p')
      foot.style.cssText = 'margin-top:32px;padding-top:12px;border-top:1px solid #ddd;'
        + 'color:#666;font-size:12px'
      const head = isDraft ? '未验收草稿 · ' : ''
      foot.textContent = bodyFromServer
        ? head + '版本 ' + versionId
        : head + '版本未知（本地未验证副本，无验收清单绑定）'
      w.document.getElementById('content')!.appendChild(foot)
    }
    w.print()
  }

  return (
    <div id="report-viewer" className="animate-fade-in space-y-5 scroll-mt-6">
      {/* F3a 结论卡：报告正文首表前 4 行（与分享页同口径）；无表时降级到验收器数字统计 */}
      {topStats.length > 0 ? (
        <div className="rounded-xl border border-cyan-500/20 bg-slate-900 p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Award className="h-4 w-4 text-cyan-400" />
            <span className="text-sm font-semibold text-cyan-400">结论速览</span>
            <span className="text-xs text-slate-500">取自报告正文首表前 4 行</span>
          </div>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {topStats.map((it, i) => (
              <div key={i} className="rounded-lg border border-slate-800 bg-slate-800/30 px-3 py-2">
                <div className="truncate text-xs text-slate-400" title={it.k}>{it.k}</div>
                <div className="mt-0.5 truncate font-semibold text-slate-100" title={it.v}>{it.v}</div>
              </div>
            ))}
          </div>
        </div>
      ) : accTrace ? (
        <div className="rounded-xl border border-slate-800 bg-slate-900 p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <ScrollText className="h-4 w-4 text-slate-400" />
            <span className="text-sm font-semibold text-slate-300">结论速览</span>
            <span className="text-xs text-slate-500">
              报告正文没有可提取的表格，以下为验收器统计（非报告结论）
            </span>
          </div>
          <div className="grid grid-cols-3 gap-3">
            {[
              { label: '关键数字', value: String(accTrace.total_count ?? 0) },
              { label: '可溯源', value: String(accTrace.traceable_count ?? 0) },
              {
                label: '溯源率',
                value: accTrace.covered_ratio != null
                  ? `${Math.round(accTrace.covered_ratio * 100)}%` : '未知',
              },
            ].map(item => (
              <div key={item.label} className="rounded-lg border border-slate-800 bg-slate-800/30 px-3 py-2">
                <div className="text-xs text-slate-400">{item.label}</div>
                <div className="mt-0.5 font-semibold text-slate-100">{item.value}</div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {/* Stats bar */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          { icon: Clock, label: '耗时', value: `${s.duration}s`, color: 'text-slate-400' },
          { icon: CheckCircle2, label: '成功率', value: `${rate}%`, color: 'text-emerald-400' },
          { icon: Award, label: 'Steps', value: `${s.successSteps}/${s.totalSteps}`, color: 'text-cyan-400' },
          { icon: Zap, label: 'Files', value: `${report.files?.length ?? 0}`, color: 'text-violet-400' },
        ].map(item => (
          <div key={item.label} className="bg-slate-900 border border-slate-800 rounded-xl p-4 text-center">
            <item.icon className={`w-5 h-5 mx-auto mb-2 ${item.color}`} />
            <div className={`text-xl font-bold ${item.color}`}>{item.value}</div>
            <div className="text-slate-500 text-xs">{item.label}</div>
          </div>
        ))}
      </div>

      {/* F3-B：研究简报面板——关键发现 → 缺口 → 证据 → 修改/重验 → 导出版本绑定。
          放在正文之前：金融读者先看结论与缺口，再决定要不要改、怎么导。 */}
      <ResearchBriefPanel
        taskId={report.taskId}
        research={report.research}
        exportState={report.export}
        onRevised={() => { if (report.taskId) void reloadTask(report.taskId) }}
      />

      {/* Report card */}
      <div className="bg-slate-900 border border-emerald-500/20 rounded-xl">
        <div className="flex items-center justify-between px-5 py-3 bg-emerald-500/5 border-b border-emerald-500/20 rounded-t-xl">
          <div className="flex items-center gap-2.5">
            <CheckCircle2 className="w-5 h-5 text-emerald-400" />
            <span className="text-emerald-400 font-semibold text-sm">任务完成</span>
            <span className="text-slate-600 text-xs ml-1">— {report.summary}</span>
          </div>
        </div>

        {/* F3a 数字可信度卡：四档分类 + 溯源率/金额溯源率 + 不可溯源明细 + 口径（同一份验收器数据） */}
        {accTrace && (() => {
          const nt = accTrace
          const total = nt.total_count ?? 0
          const ratio = nt.covered_ratio != null ? Math.round(nt.covered_ratio * 100) : null
          const amountRate = nt.amount_rate != null ? Math.round(nt.amount_rate * 100) : null
          const untraced: any[] = Array.isArray(nt.untraceable) ? nt.untraceable : []
          const unverifiable = nt.unverifiable_count ?? untraced.length
          const pass = nt.pass !== false
          const DOMAIN_LABEL: Record<string, string> = {
            financial: '财务', news: '新闻', crypto: '加密资产', macro: '宏观', research: '调研',
          }
          return (
            <div className="mx-4 mt-4 space-y-2.5 rounded-xl border border-cyan-500/20 bg-cyan-500/5 px-3.5 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <ScrollText className="w-3.5 h-3.5 text-cyan-400 shrink-0" />
                <span className="text-xs font-semibold text-cyan-400">数字可信度</span>
                <span className={`px-2 py-0.5 rounded text-xs ${
                  pass ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400'}`}>
                  {pass ? '达阈值' : '低于阈值'}
                </span>
                <span className="text-xs text-slate-400">共 {total} 个：</span>
                <span className="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 text-xs">引用 {nt.cited_count ?? 0}</span>
                <span className="px-2 py-0.5 rounded bg-cyan-500/10 text-cyan-400 text-xs">计算 {nt.computed_count ?? 0}</span>
                <span className="px-2 py-0.5 rounded bg-violet-500/10 text-violet-400 text-xs">模型知识 {nt.disclosed_count ?? 0}</span>
                <span className="px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 text-xs">不可溯源 {unverifiable}</span>
              </div>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs">
                <span className="text-slate-400">
                  数字溯源率
                  <span className={`ml-1.5 font-semibold ${ratio != null && ratio >= 70 ? 'text-emerald-400' : 'text-amber-400'}`}>
                    {ratio != null ? `${ratio}%` : '未知'}
                  </span>
                  {nt.traceable_count != null && total > 0 && (
                    <span className="ml-1 text-slate-500">（{nt.traceable_count}/{total}）</span>
                  )}
                </span>
                <span className="text-slate-400">
                  金额溯源率
                  <span className={`ml-1.5 font-semibold ${amountRate != null && amountRate >= 70 ? 'text-emerald-400' : 'text-amber-400'}`}>
                    {amountRate != null ? `${amountRate}%` : '未知'}
                  </span>
                  {nt.amount_total != null && (
                    <span className="ml-1 text-slate-500">（{nt.amount_traceable ?? 0}/{nt.amount_total}）</span>
                  )}
                </span>
              </div>
              {untraced.length > 0 && (
                <div>
                  <div className="text-xs text-slate-400">
                    不可溯源数字（最多显示 10 条，共 {unverifiable} 条）
                  </div>
                  <ul className="mt-1 space-y-0.5">
                    {untraced.slice(0, 10).map((u: any, i: number) => (
                      <li key={i} className="truncate text-xs text-amber-300/80" title={String(u?.raw ?? '')}>
                        - {String(u?.raw ?? '').slice(0, 60)}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <div className="text-xs leading-relaxed text-slate-500">
                口径：分母 = 报告正文提取到的关键数字个数；本域阈值
                {nt.threshold != null ? ` ${Math.round(nt.threshold * 100)}%` : ' —'}
                {nt.domain ? `，域=${DOMAIN_LABEL[nt.domain] || nt.domain}` : ''}。
                金额溯源率只统计带货币单位的数字，与整体溯源率分开看——整体达标不代表金额可信。
              </div>
            </div>
          )
        })()}

        {/* S2：可重算底稿（目标达成 / 关键指标 / 来源与缺口 / 计算值公式） */}
        {paperData && (
          <div className="mx-4 mt-4">
            <WorkingPaperPanel paper={paperData} />
          </div>
        )}

        {/* 验收缺口横幅：SUCCESS_WITH_ISSUES 任务的报告顶部展示缺口明细 */}
        {report.summary === 'SUCCESS_WITH_ISSUES' && report.acceptance?.gaps && report.acceptance.gaps.length > 0 && (
          <div className="mx-4 mt-4 flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" />
            <div className="min-w-0">
              <div className="text-xs font-semibold text-amber-300/90">验收未完全通过（SUCCESS_WITH_ISSUES）</div>
              <ul className="mt-1 space-y-1">
                {report.acceptance.gaps.map((g, i) => (
                  <li key={i} className="text-sm leading-relaxed text-amber-200/80">- {g}</li>
                ))}
              </ul>
            </div>
          </div>
        )}

        {/* 数据时效信息卡 */}
        {freshness && (
          <div className="mx-4 mt-4 flex items-start gap-3 rounded-xl border border-amber-400/20 bg-amber-400/5 px-4 py-3">
            <CalendarClock className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" />
            <div className="min-w-0">
              <div className="text-xs font-semibold text-amber-300/90">数据时效</div>
              <div className="mt-0.5 text-sm leading-relaxed text-slate-300">{freshness.text}</div>
            </div>
          </div>
        )}

        {/* 窄屏：顶部粘性目录 */}
        {toc.length >= 2 && (
          <div className="lg:hidden sticky top-4 z-20 mt-4 border-y border-slate-800/80 bg-slate-900/95 px-4 py-2.5 backdrop-blur">
            <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-slate-500">
              <ListTree className="h-3 w-3" /> 目录
            </div>
            <div className="flex gap-1.5 overflow-x-auto pb-0.5">
              {toc.map(e => (
                <button key={e.id} onClick={() => scrollToHeading(e.id)}
                  className="shrink-0 rounded-md border border-slate-800 bg-slate-800/40 px-2 py-1 text-xs text-slate-400 transition-colors hover:border-cyan-500/30 hover:text-cyan-300">
                  {e.text}
                </button>
              ))}
            </div>
          </div>
        )}

        <div className={toc.length >= 2 ? 'grid lg:grid-cols-[14rem_1fr]' : ''}>
          {/* 宽屏：左侧粘性目录 */}
          {toc.length >= 2 && (
            <aside className="sticky top-6 hidden max-h-[calc(100vh-6rem)] self-start overflow-y-auto border-r border-slate-800/70 px-4 py-4 lg:block">
              <div className="mb-3 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-slate-500">
                <ListTree className="h-3.5 w-3.5" /> 目录
              </div>
              <nav className="space-y-0.5">
                {toc.map(e => (
                  <button key={e.id} onClick={() => scrollToHeading(e.id)}
                    className={`block w-full rounded-md px-2 py-1.5 text-left transition-colors hover:bg-slate-800/70 hover:text-cyan-300 ${
                      e.level === 1
                        ? 'text-[13px] font-medium text-slate-200'
                        : e.level === 2
                          ? 'pl-4 text-[13px] text-slate-400'
                          : 'pl-7 text-xs text-slate-500'
                    }`}>
                    {e.text}
                  </button>
                ))}
              </nav>
            </aside>
          )}

          <div className="min-w-0 p-6 text-sm leading-relaxed text-slate-300 max-w-none
            [&_h1]:text-xl [&_h1]:font-bold [&_h1]:text-slate-100 [&_h1]:mb-3
            [&_h2]:text-lg [&_h2]:font-semibold [&_h2]:text-cyan-400 [&_h2]:mt-6 [&_h2]:mb-3
            [&_h3]:text-base [&_h3]:font-medium [&_h3]:text-slate-200 [&_h3]:mb-2
            [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:space-y-1 [&_li]:text-slate-400
            [&_strong]:text-slate-200 [&_code]:text-cyan-400 [&_code]:bg-slate-800 [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:rounded [&_code]:text-xs
            [&_table]:w-full [&_table]:text-xs [&_th]:text-left [&_th]:text-slate-400 [&_th]:font-medium [&_th]:px-2 [&_th]:py-1 [&_th]:border-b [&_th]:border-slate-800
            [&_td]:px-2 [&_td]:py-1 [&_td]:border-b [&_td]:border-slate-800/50">
            <ReportMarkdown md={bodyMd} sources={sourceItems} taskId={taskIdForFiles} />

            {/* 参考来源结构化卡片 */}
            {sourcesResult.sources && (
              <section id="sources" className="mt-8 scroll-mt-20">
                <h2 className="flex items-center gap-2 text-lg font-semibold text-cyan-400">
                  <Quote className="h-4 w-4" /> {sourcesResult.sources.heading}
                </h2>
                <ol className="mt-3 space-y-2">
                  {sourcesResult.sources.items.map((s, i) => (
                    <li key={`${s.url}-${i}`}>
                      <a href={s.url} target="_blank" rel="noopener noreferrer"
                        className="group flex items-center gap-3 rounded-lg border border-slate-800 bg-slate-800/30 px-3.5 py-2.5 transition-colors hover:border-cyan-500/30 hover:bg-slate-800/60">
                        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-cyan-500/15 text-xs font-semibold text-cyan-400">
                          {i + 1}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm text-slate-200 transition-colors group-hover:text-cyan-300">{s.title}</span>
                          <span className="mt-0.5 block truncate text-xs text-slate-500">{s.domain}</span>
                        </span>
                        <ExternalLink className="h-3.5 w-3.5 shrink-0 text-slate-600 transition-colors group-hover:text-cyan-400" />
                      </a>
                    </li>
                  ))}
                </ol>
              </section>
            )}

            {/* 免责声明弱化卡片 */}
            {disclaimerResult.disclaimer && (
              <section className="mt-6 rounded-lg border border-slate-800/60 bg-slate-900/40 px-4 py-3">
                <div className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-slate-600">
                  <Quote className="h-3 w-3" /> 免责声明
                </div>
                <p className="mt-1 text-xs leading-relaxed text-slate-600">{disclaimerResult.disclaimer}</p>
              </section>
            )}
          </div>
        </div>
      </div>

      {/* F3a 验收证据：判定规则指纹（可反查旧结果由哪版规则产出）+ 验收时间线（反思重做可见） */}
      {accData && (
        <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
          <button onClick={() => setFingerprintOpen(!fingerprintOpen)}
            className="w-full flex items-center justify-between px-5 py-3 hover:bg-slate-800/30 transition-colors">
            <div className="flex items-center gap-2 text-sm text-slate-300">
              <Fingerprint className="w-4 h-4 text-cyan-400" />
              验收证据
              {timeline && timeline.length > 0 && (
                <span className="text-xs text-slate-500">验收 {timeline.length} 次</span>
              )}
            </div>
            {fingerprintOpen ? <ChevronDown className="w-4 h-4 text-slate-500" /> : <ChevronRight className="w-4 h-4 text-slate-500" />}
          </button>
          {fingerprintOpen && (
            <div className="px-5 pb-4 space-y-3">
              <div className="grid gap-2 sm:grid-cols-2">
                {[
                  { k: '判定规则版本', v: accData.rules_version || '—' },
                  { k: '规则指纹', v: accData.rules_fingerprint || '—' },
                  { k: '报告 SHA256', v: accData.report_sha256 || '—' },
                  { k: '评估时间', v: accData.evaluated_at || '—' },
                ].map(row => (
                  <div key={row.k} className="rounded-lg border border-slate-800 bg-slate-800/30 px-3 py-2">
                    <div className="text-xs text-slate-400">{row.k}</div>
                    <div className="mt-0.5 truncate font-mono text-xs text-slate-200" title={String(row.v)}>
                      {String(row.v)}
                    </div>
                  </div>
                ))}
              </div>
              <div className="text-xs leading-relaxed text-slate-500">
                规则指纹随验收规则内容变化——同一份报告在不同规则版本下的判定不同，
                留指纹才能在规则更新后反查旧结论由哪一版产出。
              </div>
              <div>
                <div className="mb-1.5 text-xs font-semibold text-slate-400">验收时间线</div>
                {timeline == null && <div className="text-xs text-slate-500">加载中…</div>}
                {timeline != null && timeline.length === 0 && (
                  <div className="text-xs text-slate-500">该任务没有验收事件（旧任务或尚未进入验收阶段）</div>
                )}
                {timeline != null && timeline.length > 0 && (
                  <ol className="space-y-1.5">
                    {timeline.map((ev: any, i: number) => {
                      const ok = String(ev?.overall || '').includes('SUCCESS') && !String(ev?.overall || '').includes('ISSUE')
                      return (
                        <li key={i} className="flex flex-wrap items-center gap-2 rounded border border-slate-800 bg-slate-800/20 px-3 py-2 text-xs">
                          <span className="text-slate-500">#{ev?.seq ?? i + 1}</span>
                          <span className="text-slate-300">{ev?.trigger || '验收'}</span>
                          {ev?.iteration ? <span className="text-slate-500">第 {ev.iteration} 轮</span> : null}
                          <span className={`px-1.5 py-0.5 rounded ${ok ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400'}`}>
                            {ev?.overall || '—'}
                          </span>
                          <span className="text-slate-400">缺口 {ev?.gaps_count ?? 0}</span>
                          <span className="text-slate-500">{String(ev?.timestamp || '').replace('T', ' ').replace('Z', '')}</span>
                          {ev?.report_sha256 ? (
                            <span className="font-mono text-slate-600" title={`报告指纹 ${ev.report_sha256}`}>
                              {String(ev.report_sha256).slice(0, 8)}
                            </span>
                          ) : null}
                        </li>
                      )
                    })}
                  </ol>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Files section */}
      {report.files && report.files.length > 0 && (
        <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
          <button onClick={() => setExpandedFiles(!expandedFiles)}
            className="w-full flex items-center justify-between px-5 py-3 hover:bg-slate-800/30 transition-colors">
            <div className="flex items-center gap-2 text-sm text-slate-300">
              <Package className="w-4 h-4 text-violet-400" />
              生成文件 ({report.files.length})
            </div>
            {expandedFiles ? <ChevronDown className="w-4 h-4 text-slate-500" /> : <ChevronRight className="w-4 h-4 text-slate-500" />}
          </button>
          {expandedFiles && (
            <div className="px-5 pb-4 space-y-1">
              {report.files.map((f, i) => (
                <div key={i} className="px-3 py-2 space-y-1.5">
                  <div className="flex items-center gap-2 text-xs font-mono text-slate-300 bg-slate-800/40 rounded px-3 py-2">
                    <FileDown className="w-3 h-3 text-slate-500 shrink-0" />
                    <span className="truncate">{f.name}</span>
                    {f.size != null && <span className="text-slate-600 shrink-0">{(f.size / 1024).toFixed(1)} KB</span>}
                    {f.kind && <span className="px-1.5 py-0.5 rounded bg-slate-700/60 text-slate-400 text-xs shrink-0">{f.kind}</span>}
                    <div className="ml-auto flex items-center gap-1.5 shrink-0">
                      {f.kind === 'html' && (
                        <button onClick={() => openFile(f.name)}
                          className="flex items-center gap-1 px-2 py-1 rounded bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-400 text-xs">
                          <ExternalLink className="w-3 h-3" /> 打开
                        </button>
                      )}
                      {f.kind === 'py' && (
                        <button onClick={() => runFile(f.name)} disabled={running === f.name || demoMode}
                          title={demoMode ? '演示模式下已停用（会在服务端执行代码）' : '运行该交付物（沙箱内执行）'}
                          className="flex items-center gap-1 px-2 py-1 rounded bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 text-xs disabled:opacity-50">
                          <Play className="w-3 h-3" /> {running === f.name ? '运行中...' : '运行'}
                        </button>
                      )}
                      {f.kind !== 'html' && (
                        <button onClick={() => downloadFile(f.name)}
                          className="flex items-center gap-1 px-2 py-1 rounded bg-slate-700/50 hover:bg-slate-700 text-slate-300 text-xs">
                          <Download className="w-3 h-3" /> 下载
                        </button>
                      )}
                    </div>
                  </div>
                  {runOutput[f.name] && (
                    <pre className="px-3 py-2 text-xs text-emerald-300/90 bg-slate-950 rounded whitespace-pre-wrap max-h-48 overflow-y-auto">
                      {runOutput[f.name]}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* E1 溯源面板：三档分类 + 来源声明 + 免责声明 */}
      {(verifyData || verifyError || verifyLoading) && (
        <div className="bg-slate-900/80 border border-cyan-500/20 rounded-xl p-4 space-y-3">
          <div className="text-xs font-semibold text-cyan-400">数据溯源（确定性验收器）</div>
          {verifyLoading && <div className="text-xs text-slate-500">体检中…</div>}
          {verifyError && <div className="text-xs text-amber-400">体检失败：{verifyError}</div>}
          {verifyData && (
            <>
              <div className="text-sm text-slate-200">{verifyData.summary}</div>
              {verifyData.numbers && (
                <div className="flex flex-wrap gap-2 text-xs">
                  <span className="px-2 py-1 rounded bg-emerald-500/10 text-emerald-400">引用 {verifyData.numbers.cited}</span>
                  <span className="px-2 py-1 rounded bg-cyan-500/10 text-cyan-400">计算 {verifyData.numbers.computed}</span>
                  <span className="px-2 py-1 rounded bg-violet-500/10 text-violet-400">模型知识 {verifyData.numbers.disclosed_model_knowledge}</span>
                  <span className="px-2 py-1 rounded bg-amber-500/10 text-amber-400">不可溯源 {verifyData.numbers.untraced}</span>
                </div>
              )}
              {verifyData.source_labeling && !verifyData.source_labeling.pass && (
                <div className="text-xs text-amber-400">
                  疑似虚假来源标注：{(verifyData.source_labeling.mislabeled || []).slice(0, 5).join('、')}
                </div>
              )}
              {verifyData.disclaimer && (
                <div className="text-xs text-slate-500">
                  免责声明：{verifyData.disclaimer.present ? '✅ 已包含' : '❌ 缺失'}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* F3a 导出与复核：下载 / 复核 / 分享集中一处，并说明交付包 zip 为何暂不提供 */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-slate-300">导出与复核</span>
          <span className="text-xs text-slate-500">报告文件、复核与分享都在这里</span>
        </div>
        <div className="flex gap-3 flex-wrap">
        <span className="basis-full text-xs text-slate-500">报告文件</span>
        <button onClick={downloadMarkdown}
          className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors">
          <FileDown className="w-4 h-4" /> 下载Markdown
        </button>
        <button onClick={downloadPDF}
          className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors">
          <FileDown className="w-4 h-4" /> 下载PDF
        </button>
        {report.files && report.files.length > 0 && (
          <button onClick={() => setExpandedFiles(!expandedFiles)}
            className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors">
            <Package className="w-4 h-4" /> 生成文件 ({report.files.length}) {expandedFiles ? '收起' : '展开'}
          </button>
        )}
        {report.export?.package && taskIdForFiles ? (
          <a href={`/files/${encodeURIComponent(taskIdForFiles)}/${encodeURIComponent(report.export.package)}`}
            download={report.export.package}
            className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors">
            <Package className="w-4 h-4" /> 下载交付包 zip
          </a>
        ) : null}
        {taskIdForFiles ? (
          <button
            onClick={async () => {
              if (repacking) return
              setRepacking(true); setRepackMsg('')
              try {
                const res = await fetch(`/api/task/${encodeURIComponent(taskIdForFiles)}/package`, {
                  method: 'POST', headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({}),
                })
                const d = await res.json().catch(() => null)
                if (!res.ok || !d || d.error) {
                  setRepackMsg(d?.error || `重新导出失败（HTTP ${res.status}）`)
                } else {
                  setRepackMsg(
                    `已生成 ${d.package}（${d.files?.length ?? 0} 个成员，`
                    + `包内字节自检 ${d.verify_ok ? '全部一致' : '有不一致，见日志'}）`)
                  await reloadTask(taskIdForFiles)
                }
              } catch (e) {
                setRepackMsg(`重新导出失败：${String(e).slice(0, 120)}`)
              } finally { setRepacking(false) }
            }}
            disabled={repacking}
            className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-slate-300 rounded-lg text-sm transition-colors">
            <Package className="w-4 h-4" /> {repacking ? '正在重新导出…' : '按当前版本重新导出'}
          </button>
        ) : null}
        <span className="basis-full text-xs leading-relaxed text-slate-500">
          {report.export?.package
            ? `交付包 ${report.export.package}（生成于 ${report.export.package_generated_at || '未知时间'}）按登录会话放行下载；`
              + '包可能早于最近一次修订，正文里的图表与"生成文件"列表始终按当前版本导出。'
              + '修订后用"按当前版本重新导出"生成新包（旧包保留不动）。'
            : '本次任务暂无交付包（打包步骤未产出 deliverables_*.zip）；当前按文件逐个下载——'
              + '上方"生成文件"列表与正文里的图表都带下载按钮。'}
        </span>
        {repackMsg ? (
          <span className="basis-full text-xs text-slate-400">{repackMsg}</span>
        ) : null}
        <span className="basis-full text-xs text-slate-500">复核与分享</span>
        <button onClick={async () => {
          if (!taskIdForFiles) return
          setVerifyLoading(true); setVerifyError('')
          try {
            const res = await fetch('/api/verify', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ report_text: report?.final_report || '', task_id: taskIdForFiles }),
            })
            const d = await res.json()
            if (d.error) { setVerifyError(d.error) } else { setVerifyData(d) }
          } catch (e: any) { setVerifyError(String(e)) }
          setVerifyLoading(false)
        }}
          className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors disabled:opacity-50"
          disabled={demoMode || verifyLoading}
          title={demoMode ? '演示模式下已停用（数据溯源会调用真实模型）' : '对报告数字做一次可溯源复核'}>
          <ScrollText className="w-4 h-4" /> 数据溯源
        </button>
        <button onClick={() => setShowLogs(!showLogs)}
          className={`flex items-center gap-2 px-4 py-2.5 rounded-lg text-sm transition-colors ${
            showLogs ? 'bg-cyan-500/20 text-cyan-400' : 'bg-slate-800 hover:bg-slate-700 text-slate-300'
          }`}>
          <ScrollText className="w-4 h-4" /> {showLogs ? 'Hide' : 'View'} 完整日志 ({logs.length})
        </button>
        {shareTaskId && !demoMode && (
          shareUrl ? (
            <>
              <button onClick={() => setShareDialogOpen(true)}
                className="flex items-center gap-2 px-4 py-2.5 bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 rounded-lg text-sm border border-emerald-500/30 transition-colors">
                <Link2 className="w-4 h-4" /> 分享链接已开启
              </button>
              <button onClick={revokeShare}
                className="flex items-center gap-2 px-4 py-2.5 bg-red-500/10 hover:bg-red-500/20 text-red-400 rounded-lg text-sm border border-red-500/20 transition-colors">
                <Trash2 className="w-4 h-4" /> 撤销分享
              </button>
            </>
          ) : (
            <button onClick={createShare} disabled={shareLoading}
              className="flex items-center gap-2 px-4 py-2.5 bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-400 rounded-lg text-sm border border-cyan-500/30 transition-colors disabled:opacity-50">
              <Share2 className="w-4 h-4" /> {shareLoading ? '生成中...' : '分享链接'}
            </button>
          )
        )}
      </div>
      </div>

      {/* 分享链接复制对话框：挂到 body 上——报告容器带 animate-fade-in
          （will-change: transform）会为 fixed 后代新建包含块，留在树内会被
          拉到报告全高（实测 6388px）、跑到视口之外，用户看不到对话框。 */}
      {shareDialogOpen && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setShareDialogOpen(false)}>
          <div className="w-full max-w-md bg-slate-900 border border-slate-700 rounded-xl p-5 shadow-xl"
            onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-1">
              <div className="flex items-center gap-2 text-sm text-emerald-400 font-semibold">
                <Link2 className="w-4 h-4" /> 报告分享链接
              </div>
              <button onClick={() => setShareDialogOpen(false)}
                className="text-slate-500 hover:text-slate-300">
                <X className="w-4 h-4" />
              </button>
            </div>
            {shareUrl ? (
              <>
                <p className="text-xs text-slate-500 mb-3">
                  复制链接发给别人，对方无需登录即可在浏览器查看该报告
                  （{Math.round(Number(shareTtlHours) / 24) || 7} 天内有效
                  {shareProtected ? '，已开启访问密码' : ''}）。
                </p>
                <div className="flex gap-2">
                  <input readOnly value={shareUrl} onFocus={e => e.currentTarget.select()}
                    className="flex-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-xs text-cyan-300 font-mono focus:outline-none" />
                  <button onClick={copyShare}
                    className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-cyan-500 text-slate-950 text-xs font-semibold hover:bg-cyan-400 shrink-0">
                    {shareCopied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                    {shareCopied ? '已复制' : '复制'}
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="text-xs text-slate-500 mb-3">
                  可设置访问密码与有效期后生成分享链接；密码留空表示公开链接。
                </p>
                <label className="block text-xs text-slate-400 mb-1">
                  访问密码（可选）
                  <input type="password" value={sharePassword}
                    onChange={e => setSharePassword(e.target.value)}
                    placeholder="留空则不设密码"
                    className="w-full mt-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none" />
                </label>
                <label className="block text-xs text-slate-400 mt-3 mb-1">
                  有效期
                  <select value={shareTtlHours}
                    onChange={e => setShareTtlHours(e.target.value)}
                    className="w-full mt-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none">
                    <option value="168">7 天</option>
                    <option value="336">14 天</option>
                    <option value="720">30 天（上限）</option>
                  </select>
                </label>
                <button onClick={generateShare} disabled={shareLoading}
                  className="w-full mt-4 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-cyan-500 text-slate-950 text-xs font-semibold hover:bg-cyan-400 disabled:opacity-50">
                  {shareLoading ? '生成中...' : '生成分享链接'}
                </button>
                <p className="text-xs text-red-400 mt-2">{shareError || ''}</p>
              </>
            )}
            {shareUrl && (
              <div className="mt-4 flex gap-2">
                <button onClick={() => setShareDialogOpen(false)}
                  className="flex-1 px-3 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs">
                  完成
                </button>
                <button onClick={revokeShare}
                  className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 text-xs border border-red-500/20">
                  <Trash2 className="w-3.5 h-3.5" /> 撤销分享
                </button>
              </div>
            )}
          </div>
        </div>,
        document.body,
      )}

      {/* Inline logs */}
      {showLogs && (
        <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden animate-fade-in">
          <div className="px-5 py-3 border-b border-slate-800 text-sm text-slate-400 font-medium">执行日志</div>
          <div className="max-h-64 overflow-y-auto font-mono text-xs">
            {logs.map((l, i) => (
              <div key={l.id || i} className={`flex gap-3 px-4 py-1.5 border-b border-slate-800/30 ${
                l.type === 'error' ? 'bg-red-500/5' : l.type === 'review' ? 'bg-purple-500/5' : ''
              }`}>
                <span className="text-slate-600 shrink-0 w-16">{l.timestamp.slice(0, 8)}</span>
                <span className={`shrink-0 w-12 text-xs ${
                  l.type === 'error' ? 'text-red-400' : l.type === 'plan' ? 'text-blue-400' :
                  l.type === 'review' ? 'text-purple-400' : l.type === 'dispatch' ? 'text-cyan-400' :
                  l.type === 'memory' ? 'text-amber-400' : 'text-slate-500'
                }`}>{l.type}</span>
                <span className="text-slate-400">{l.message}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
})
