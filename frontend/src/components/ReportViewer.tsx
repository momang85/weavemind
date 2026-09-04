import { useState, useEffect, memo } from 'react'
import type { ReactNode } from 'react'
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
// types used: TaskReport

import { useTaskStore } from '../stores/useTaskStore'
import {
  FileDown, Package, ScrollText, Clock, CheckCircle2, AlertTriangle,
  ChevronDown, ChevronRight, Award, Zap, Download, ExternalLink, Play,
  Share2, Link2, Copy, Check, X, Trash2, CalendarClock, ListTree, Quote,
} from 'lucide-react'

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

function CitationBadges({ nums, sources }: { nums: number[]; sources: SourceItem[] }) {
  const scrollToSources = () => {
    document.getElementById('sources')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  return (
    <span className="mx-0.5 inline-flex items-center gap-0.5 align-super">
      {nums.map(n => {
        const src = sources[n - 1]
        const tip = src ? `${src.title} — ${src.domain}` : `未找到对应来源 ${n}`
        return (
          <button key={n} type="button" title={tip}
            onClick={src ? scrollToSources : undefined}
            className={`rounded px-1 py-px text-[10px] font-semibold leading-none transition-colors ${
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

function ReportMarkdown({ md, sources }: { md: string; sources: SourceItem[] }) {
  citationSources = sources
  headingSeen = {}

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      urlTransform={(url) => (url.startsWith('cite:') ? url : defaultUrlTransform(url))}
      components={markdownComponents}
    >
      {md}
    </ReactMarkdown>
  )
}

export default memo(function ReportViewer() {
  const { report, logs, currentTaskId, demoMode } = useTaskStore()
  const taskIdForFiles = currentTaskId || report?.taskId || null
  const [showLogs, setShowLogs] = useState(false)
  const [expandedFiles, setExpandedFiles] = useState(false)
  const [runOutput, setRunOutput] = useState<Record<string, string>>({})
  const [running, setRunning] = useState<string | null>(null)
  // 报告分享：shareUrl 非空表示该任务已生成分享链接
  const [shareUrl, setShareUrl] = useState<string | null>(null)
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
    try {
      const res = await fetch(fileUrl(name))
      if (!res.ok) return
      const blob = await res.blob()
      window.open(URL.createObjectURL(blob), '_blank')
    } catch { /* 打开失败静默 */ }
  }

  const downloadFile = async (name: string) => {
    try {
      const res = await fetch(fileUrl(name))
      if (!res.ok) return
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = name.split('/').pop() || name
      a.click()
      URL.revokeObjectURL(url)
    } catch { /* 下载失败静默 */ }
  }

  if (!report) return null

  const s = report.stats || { totalSteps: report.steps.length, successSteps: 0, failedSteps: 0, duration: 0 }
  const rate = s.totalSteps > 0 ? Math.round((s.successSteps / s.totalSteps) * 100) : 100

  // 结构化解析流水线：围栏兜底 → 数据时效 → 来源清单 → 免责声明 → 引用上标 → 目录
  const rawMd = stripOuterFence(report.final_report || '')
  const freshness = parseFreshness(rawMd)
  const sourcesResult = parseSources(rawMd)
  const disclaimerResult = parseDisclaimer(sourcesResult.rest)
  const bodyMd = addCitationLinks(disclaimerResult.rest, sourcesResult.sectionText)
  const toc = parseToc(disclaimerResult.rest)
  const sourceItems = sourcesResult.sources?.items ?? []

  const scrollToHeading = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const downloadMarkdown = () => {
    const blob = new Blob([report.final_report], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = 'report.md'; a.click()
    URL.revokeObjectURL(url)
  }

  const fileUrl = (name: string) =>
    taskIdForFiles ? '/files/' + encodeURIComponent(taskIdForFiles) + '/' + encodeURIComponent(name)
                   : '/files/' + encodeURIComponent(name)

  const downloadPDF = async () => {
    if (taskIdForFiles) {
      try {
        const res = await fetch('/api/task/' + encodeURIComponent(taskIdForFiles) + '/pdf')
        if (!res.ok) throw new Error('PDF unavailable')
        const blob = await res.blob()
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = taskIdForFiles + '.pdf'
        a.click()
        URL.revokeObjectURL(url)
        return
      } catch { /* 退回浏览器打印方案 */ }
    }
    const w = window.open('', '_blank')
    if (!w) return
    w.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Report</title>
<style>body{font-family:system-ui;max-width:800px;margin:40px auto;padding:20px;color:#1a1a2e;line-height:1.8}
h1,h2{color:#16213e} pre{background:#f5f5f5;padding:16px;border-radius:8px;overflow-x:auto}
code{background:#f0f0f0;padding:2px 6px;border-radius:4px} table{border-collapse:collapse;width:100%}
th,td{border:1px solid #ddd;padding:8px;text-align:left} th{background:#16213e;color:#fff}
</style></head><body><div id="content"></div></body></html>`)
    w.document.close()
    // Convert markdown to HTML inline
    const md = report.final_report
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      .replace(/^- (.+)$/gm, '<li>$1</li>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\n/g, '<br/>')
    w.document.getElementById('content')!.innerHTML = md
    w.print()
  }

  return (
    <div className="animate-fade-in space-y-5">
      {/* Stats bar */}
      <div className="grid grid-cols-4 gap-3">
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

      {/* Report card */}
      <div className="bg-slate-900 border border-emerald-500/20 rounded-xl">
        <div className="flex items-center justify-between px-5 py-3 bg-emerald-500/5 border-b border-emerald-500/20 rounded-t-xl">
          <div className="flex items-center gap-2.5">
            <CheckCircle2 className="w-5 h-5 text-emerald-400" />
            <span className="text-emerald-400 font-semibold text-sm">任务完成</span>
            <span className="text-slate-600 text-xs ml-1">— {report.summary}</span>
          </div>
        </div>

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
            <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-slate-500">
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
              <div className="mb-3 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-slate-500">
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
            <ReportMarkdown md={bodyMd} sources={sourceItems} />

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
                        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-cyan-500/15 text-[11px] font-semibold text-cyan-400">
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
                <div className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-slate-600">
                  <Quote className="h-3 w-3" /> 免责声明
                </div>
                <p className="mt-1 text-xs leading-relaxed text-slate-600">{disclaimerResult.disclaimer}</p>
              </section>
            )}
          </div>
        </div>
      </div>

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
                    {f.kind && <span className="px-1.5 py-0.5 rounded bg-slate-700/60 text-slate-400 text-[10px] shrink-0">{f.kind}</span>}
                    <div className="ml-auto flex items-center gap-1.5 shrink-0">
                      {f.kind === 'html' && (
                        <button onClick={() => openFile(f.name)}
                          className="flex items-center gap-1 px-2 py-1 rounded bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-400 text-[10px]">
                          <ExternalLink className="w-3 h-3" /> 打开
                        </button>
                      )}
                      {f.kind === 'py' && (
                        <button onClick={() => runFile(f.name)} disabled={running === f.name}
                          className="flex items-center gap-1 px-2 py-1 rounded bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 text-[10px] disabled:opacity-50">
                          <Play className="w-3 h-3" /> {running === f.name ? '运行中...' : '运行'}
                        </button>
                      )}
                      {f.kind !== 'html' && (
                        <button onClick={() => downloadFile(f.name)}
                          className="flex items-center gap-1 px-2 py-1 rounded bg-slate-700/50 hover:bg-slate-700 text-slate-300 text-[10px]">
                          <Download className="w-3 h-3" /> 下载
                        </button>
                      )}
                    </div>
                  </div>
                  {runOutput[f.name] && (
                    <pre className="px-3 py-2 text-[11px] text-emerald-300/90 bg-slate-950 rounded whitespace-pre-wrap max-h-48 overflow-y-auto">
                      {runOutput[f.name]}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Bottom actions */}
      <div className="flex gap-3 flex-wrap">
        <button onClick={downloadMarkdown}
          className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors">
          <FileDown className="w-4 h-4" /> 下载Markdown
        </button>
        <button onClick={downloadPDF}
          className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors">
          <FileDown className="w-4 h-4" /> 下载PDF
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

      {/* 分享链接复制对话框 */}
      {shareDialogOpen && (
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
        </div>
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
