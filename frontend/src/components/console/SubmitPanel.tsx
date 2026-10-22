import { memo, useEffect, useState } from 'react'
import { Loader2, RefreshCw, Plus, MessagesSquare, FileText, ChevronDown, Upload, Zap, ExternalLink, X, Square, Building2, ArrowUpRight, Search, Table2, Archive, BookOpen } from 'lucide-react'
import { buildResearchGoal, type ResearchFields, type ResearchForm } from '../../lib/researchGoal'

/** 快答结果卡片：正文 + 来源链接 + 诚实降级标签。 */
function QuickAnswerCard({ qa, onClose }: { qa: any; onClose: () => void }) {
  return (
    <div id="quick-answer-card" className="bg-brand/5 border border-brand/20 rounded-xl p-3 space-y-2">
      <div className="flex items-center gap-2 text-xs">
        <Zap className="w-3 h-3 text-brand" />
        <span className="text-brand font-semibold">快答</span>
        {qa.mode === 'searched' ? (
          <span className="text-emerald-700">已检索 {qa.sources?.length || 0} 条来源 · {qa.searched_at}</span>
        ) : qa.mode === 'error' ? (
          <span className="text-red-700">请求失败</span>
        ) : (
          <span className="text-amber-700">模型知识 · 未检索（未验证）</span>
        )}
        <span className="text-ink-faint">{qa.duration}s</span>
        <button onClick={onClose} aria-label="关闭快答"
          className="ml-auto p-0.5 rounded text-ink-faint hover:text-ink transition-colors">
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
      <pre className="whitespace-pre-wrap break-all text-ink text-xs font-sans max-h-64 overflow-y-auto">{qa.content}</pre>
      {qa.sources?.length > 0 && (
        <div className="space-y-1 pt-1 border-t border-line">
          <div className="text-xs text-ink-faint">参考来源：</div>
          {qa.sources.map((s: any, i: number) => (
            <a key={i} href={s.url} target="_blank" rel="noreferrer"
              className="flex items-center gap-1 text-xs text-brand hover:text-brand truncate">
              <ExternalLink className="w-2.5 h-2.5 shrink-0" />
              <span className="truncate">{s.title || s.url}</span>
            </a>
          ))}
        </div>
      )}
    </div>
  )
}

/** 任务提交区 + 上下文导入区（TaskConsole 拆分 T11c）。 */
export default memo(function SubmitPanel({
  goal, setGoal, project, setProject, confirmMode, setConfirmMode,
  templateName, setTemplateName,
  userContext, setUserContext, importMsg, setImportMsg,
  isRunning, demoMode, activeConversationId, lastGoal, reportSummary, status,
  taskId, etaText, elapsedText, onSubmit, onNewConversation,
  formCollapsedByDefault,
}: {
  goal: string
  setGoal: (v: string) => void
  project: string
  setProject: (v: string) => void
  confirmMode: boolean
  setConfirmMode: (v: boolean) => void
  templateName: string
  setTemplateName: (v: string) => void
  userContext: string
  setUserContext: React.Dispatch<React.SetStateAction<string>>
  importMsg: { name: string; note: string }[]
  setImportMsg: React.Dispatch<React.SetStateAction<{ name: string; note: string }[]>>
  isRunning: boolean
  demoMode: boolean
  activeConversationId: string | null
  lastGoal: string
  reportSummary?: string
  status: string
  taskId?: string | null
  etaText?: string
  elapsedText?: string | null
  onSubmit: (goalOverride?: string, fields?: ResearchFields | null) => void
  onNewConversation: () => void
  /** P2-4：?task=<id> 或已在跟踪某个任务时，研究表单默认折叠（第一屏先给任务本身） */
  formCollapsedByDefault?: boolean
}) {
  const [showContext, setShowContext] = useState(false)
  const [inputMode, setInputMode] = useState<'company' | 'custom'>('company')
  const [composerOpen, setComposerOpen] = useState(!formCollapsedByDefault)
  const [templates, setTemplates] = useState<any[]>([])
  const [stopping, setStopping] = useState(false)
  const [stopMsg, setStopMsg] = useState('')
  // 研究表单的结构化契约字段（与目标一起提交；重新输入目标时清空，避免张冠李戴）
  const [researchFields, setResearchFields] = useState<ResearchFields | null>(null)

  useEffect(() => { if (goal.trim()) { setInputMode('custom'); setComposerOpen(true) } }, [goal])

  // 停止运行中的任务：写取消标志，编排器在下一个派发边界收尾
  const stopTask = async () => {
    if (!taskId || stopping) return
    setStopping(true)
    setStopMsg('')
    try {
      const r = await fetch(`/api/task/${taskId}/cancel`, { method: 'POST' })
      const d = await r.json().catch(() => ({}))
      setStopMsg(r.ok ? '已请求停止，任务将在当前步骤结束后收尾'
                      : (d?.error || '停止请求失败'))
    } catch {
      setStopMsg('停止请求失败（网络错误）')
    } finally {
      setStopping(false)
    }
  }

  // 加载任务模板（模板复用：确定性步骤，跳过 LLM 规划）
  useEffect(() => {
    fetch('/api/templates').then(r => r.json()).then(d => setTemplates(d.templates ?? [])).catch(() => {})
  }, [])

  const readFileBase64 = (file: File) => new Promise<string>((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result).split(',')[1] || '')
    r.onerror = () => reject(new Error('read error'))
    r.readAsDataURL(file)
  })

  const importFiles = async (files: FileList | null) => {
    if (!files) return
    for (const f of Array.from(files)) {
      setImportMsg(prev => [...prev, { name: f.name, note: '提取中...' }])
      try {
        const b64 = await readFileBase64(f)
        const res = await fetch('/api/context/extract', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: f.name, data: b64 }),
        })
        const d = await res.json()
        if (d.text) {
          setUserContext(prev => (prev ? `${prev}\n\n[文件 ${f.name}]\n${d.text}` : `[文件 ${f.name}]\n${d.text}`))
          setImportMsg(prev => [...prev, { name: f.name, note: `成功 ${d.chars} 字${d.truncated ? '（已截断）' : ''}` }])
        } else {
          setImportMsg(prev => [...prev, { name: f.name, note: d.error || '无内容' }])
        }
      } catch {
        setImportMsg(prev => [...prev, { name: f.name, note: '导入失败' }])
      }
    }
  }

  // T3 快答：先检索后作答（不走编排器，秒级返回，带来源）
  const [qaGoal, setQaGoal] = useState('')
  const [qa, setQa] = useState<any>(null)
  const [qaLoading, setQaLoading] = useState(false)
  // 后端契约：content 应为字符串；若为 {content: str} 或其它类型，
  // 统一收敛为文本——直接把对象渲染成 React 子节点会触发 #31 崩溃
  const coerceText = (v: any): string => {
    if (typeof v === 'string') return v
    if (v && typeof v === 'object' && typeof (v as any).content === 'string') return (v as any).content
    return String(v ?? '')
  }
  const askQuick = async () => {
    const g = qaGoal.trim()
    // 演示模式必须一并拦住：快答走的是真实检索+模型接口，会产生真实调用与费用，
    // 而界面上此时写着"演示"——语义不一致会让用户以为没花钱。
    if (!g || qaLoading || demoMode) return
    setQaLoading(true); setQa(null)
    try {
      const res = await fetch('/api/quick-answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal: g }),
      })
      const d = await res.json()
      if (d.error) setQa({ content: d.error, sources: [], mode: 'error', duration: 0 })
      else setQa({ ...d, content: coerceText(d.content) })
      requestAnimationFrame(() => {
        document.getElementById('quick-answer-card')
          ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
      })
    } catch {
      setQa({ content: '请求失败，请稍后重试', sources: [], mode: 'error', duration: 0 })
    } finally {
      setQaLoading(false)
    }
  }

  if (formCollapsedByDefault && !composerOpen) return (
    <section className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-surface p-4">
      <BookOpen className="h-5 w-5 text-brand" />
      <div className="min-w-0 flex-1"><h2 className="text-sm font-medium">当前研究</h2><p className="truncate text-xs text-ink-faint">{taskId} {isRunning ? ` · ${elapsedText || '正在执行'}` : ''}</p></div>
      {isRunning && <button onClick={stopTask} disabled={stopping || demoMode} className="rounded-lg border border-red-200 px-3 py-2 text-xs text-state-failure">{stopping ? '正在请求停止…' : '停止研究'}</button>}
      <button onClick={() => setComposerOpen(true)} className="rounded-lg border border-line-strong px-3 py-2 text-xs text-brand">展开研究工具</button>
      <button onClick={onNewConversation} className="button-primary rounded-lg px-3 py-2 text-xs">新建研究</button>
      {stopMsg && <p role="status" className="basis-full text-xs text-state-issue">{stopMsg}</p>}
    </section>
  )

  return (
    <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_280px]">
      <section id="research-entry" className="min-w-0 rounded-xl border border-line bg-surface">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-4 md:px-6">
          <div><p className="eyebrow text-brand">RESEARCH DESK</p><h2 className="mt-1 text-lg font-medium">{taskId ? '当前研究与新任务' : '开启一份研究'}</h2></div>
          <button onClick={onNewConversation} title="开始新对话" className="flex items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-xs text-ink-muted hover:bg-surface-raised">
            <Plus className="h-3.5 w-3.5" />新研究
          </button>
        </div>
        <div className="p-5 md:p-6">
          <div role="tablist" aria-label="研究输入方式" className="mb-5 inline-flex max-w-full gap-1 rounded-lg bg-surface-raised p-1">
            <button type="button" role="tab" id="company-tab" aria-selected={inputMode === 'company'} aria-controls="company-panel"
              onClick={() => setInputMode('company')} className={`rounded-md px-3 py-2 text-sm ${inputMode === 'company' ? 'bg-surface text-brand-strong shadow-sm' : 'text-ink-muted'}`}>
              研究一家公司
            </button>
            <button type="button" role="tab" id="custom-tab" aria-selected={inputMode === 'custom'} aria-controls="custom-panel"
              onClick={() => setInputMode('custom')} className={`rounded-md px-3 py-2 text-sm ${inputMode === 'custom' ? 'bg-surface text-brand-strong shadow-sm' : 'text-ink-muted'}`}>
              自定义研究
            </button>
          </div>
          <div id="company-panel" role="tabpanel" aria-labelledby="company-tab" hidden={inputMode !== 'company'}>
            <ResearchQuickForm disabled={isRunning || demoMode} defaultOpen={!formCollapsedByDefault}
              onReady={(goalText, fields) => { setGoal(goalText); setResearchFields(fields); setInputMode('custom') }}
              onStart={(goalText, fields) => onSubmit(goalText, fields)} />
          </div>
          <div id="custom-panel" role="tabpanel" aria-labelledby="custom-tab" hidden={inputMode !== 'custom'} className="space-y-4">
            <label className="research-field">研究问题与要求
              <textarea value={goal} rows={5}
                onChange={event => { setGoal(event.target.value); if (researchFields) setResearchFields(null) }}
                onKeyDown={event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); onSubmit(undefined, researchFields) } }}
                placeholder={demoMode ? '演示模式：不可提交真实任务' : '例如：对比一家公司的两个年度，解释收入、利润与经营现金流的变化，并列出来源与待核查事项。'}
                disabled={isRunning || demoMode} />
            </label>
            {researchFields && <p className="text-xs text-brand">已附公司、年度与口径契约；手改目标将清除契约，可返回表单重新生成。</p>}
            <button onClick={() => onSubmit(undefined, researchFields)} disabled={isRunning || demoMode || !goal.trim()}
              className="button-primary inline-flex items-center gap-2 rounded-lg px-5 py-3 text-sm font-medium">
              {isRunning ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowUpRight className="h-4 w-4" />}
              {isRunning ? '研究进行中' : '开始研究'}
            </button>
            <span className="ml-3 text-xs text-ink-faint">Ctrl / ⌘ + Enter 提交</span>
          </div>
          <details className="mt-5 border-t border-line pt-4">
            <summary className="text-xs text-ink-muted">更多研究选项 · 项目 / 模板 / 计划确认</summary>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <label className="research-field">所属项目
                <input value={project} onChange={event => setProject(event.target.value)} placeholder="default" disabled={isRunning || demoMode} />
              </label>
              <label className="research-field">任务模板
                <select value={templateName} disabled={isRunning || demoMode} onChange={event => {
                  const name = event.target.value
                  setTemplateName(name)
                  const template = templates.find(item => item.name === name)
                  if (template?.goal && !goal.trim()) { setGoal(template.goal); setInputMode('custom') }
                }}>
                  <option value="">不使用模板</option>{templates.map(template => <option key={template.name} value={template.name}>{template.name}</option>)}
                </select>
              </label>
              <label className="flex items-center gap-2 text-xs text-ink-muted sm:col-span-2">
                <input type="checkbox" checked={confirmMode} disabled={isRunning || demoMode} onChange={event => setConfirmMode(event.target.checked)} />
                开始执行前，由我确认研究计划
              </label>
            </div>
          </details>
          <div className="mt-4 border-t border-line pt-4">
            <button type="button" onClick={() => setShowContext(!showContext)} aria-expanded={showContext}
              className="flex w-full items-center gap-2 text-left text-xs text-ink-muted">
              <FileText className="h-4 w-4 text-brand" />补充参考资料与背景（可选）
              <ChevronDown className={`ml-auto h-4 w-4 ${showContext ? 'rotate-180' : ''}`} />
            </button>
            {showContext && <div className="mt-4 space-y-3">
              <label className="research-field">背景、参考链接与约束条件
                <textarea value={userContext} onChange={event => setUserContext(event.target.value)} rows={4} disabled={isRunning || demoMode}
                  placeholder="粘贴参考资料或要求，将随本次研究一起提交。" />
              </label>
              <input id="ctx-file-input" type="file" multiple accept=".txt,.md,.csv,.json,.log,.py,.yaml,.yml,.pdf,.docx,.xlsx,.xls"
                className="hidden" disabled={isRunning || demoMode} onChange={event => { importFiles(event.target.files); event.target.value = '' }} />
              <label htmlFor="ctx-file-input" className={`inline-flex items-center gap-2 rounded-lg border border-line px-3 py-2 text-xs text-ink-muted ${isRunning || demoMode ? 'opacity-50' : 'cursor-pointer hover:bg-surface-raised'}`}>
                <Upload className="h-4 w-4" />导入文件 · PDF / Word / Excel 等
              </label>
              {importMsg.length > 0 && <ul className="space-y-1" aria-live="polite">{importMsg.map((message, index) => <li key={index} className="flex min-w-0 gap-2 text-xs">
                <span className="max-w-[50%] truncate text-ink-muted">{message.name}</span><span className={message.note.startsWith('成功') ? 'text-state-success' : 'text-state-issue'}>{message.note}</span>
              </li>)}</ul>}
            </div>}
          </div>
          {(isRunning || activeConversationId || demoMode || elapsedText || etaText) && <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-line pt-4 text-xs text-ink-faint">
            {activeConversationId && <span className="inline-flex items-center gap-1"><MessagesSquare className="h-3.5 w-3.5" />当前会话</span>}
            <span>{isRunning ? [elapsedText, etaText].filter(Boolean).join(' · ') : etaText}</span>
            {demoMode && <span className="text-state-issue">演示模式下提交与快答已停用</span>}
            {isRunning && taskId && <button onClick={stopTask} disabled={stopping || demoMode} className="inline-flex items-center gap-1 rounded border border-red-200 px-3 py-2 text-state-failure">
              <Square className="h-3 w-3" />{stopping ? '正在请求停止…' : '停止研究'}
            </button>}
            {status === 'completed' && reportSummary === 'FAILED' && lastGoal && <button disabled={demoMode} onClick={() => onSubmit(lastGoal)} className="inline-flex items-center gap-1 text-state-failure"><RefreshCw className="h-3.5 w-3.5" />重试研究</button>}
            {stopMsg && <span role="status" className="text-state-issue">{stopMsg}</span>}
          </div>}
        </div>
      </section>
      <aside aria-label="研究辅助" className="space-y-5">
        <section className="rounded-xl border border-line bg-surface p-5">
          <div className="flex items-center gap-2 text-sm font-medium"><Search className="h-4 w-4 text-brand" />资料快查</div>
          <p className="mb-4 mt-2 text-xs leading-relaxed text-ink-muted">一个具体问题，先检索再作答。来源随答案展示，不替代完整研究。</p>
          <label className="research-field"><span className="sr-only">快查问题</span>
            <textarea value={qaGoal} rows={3} onChange={event => setQaGoal(event.target.value)}
              onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); askQuick() } }}
              placeholder={demoMode ? '演示模式：快答已停用' : '例如：某公司最新财报披露了哪些经营变化？'} disabled={qaLoading || demoMode} />
          </label>
          <button onClick={askQuick} disabled={qaLoading || demoMode || !qaGoal.trim()}
            className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-lg border border-line-strong px-3 py-2.5 text-xs text-ink disabled:opacity-40">
            {qaLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />} {qaLoading ? '正在检索…' : '检索并快答'}
          </button>
          {qa && <div className="mt-4"><QuickAnswerCard qa={qa} onClose={() => setQa(null)} /></div>}
        </section>
        <section className="rounded-xl border border-line bg-surface p-5">
          <p className="eyebrow text-brand">YOUR RESEARCH FILE</p>
          <h3 className="mt-2 text-sm font-medium">不只是一份答案</h3>
          <ul className="mt-4 space-y-4">
            {[
              { icon: FileText, title: '研究正文', note: '分清数据观察、判断与缺口' },
              { icon: Table2, title: '可复算底稿', note: '计算口径与数字有据可查' },
              { icon: Archive, title: '来源与证据包', note: '定位公开披露，按版本交付' },
            ].map(({ icon: Icon, title, note }) => <li key={title} className="flex items-start gap-3">
              <Icon className="mt-0.5 h-4 w-4 shrink-0 text-brand" strokeWidth={1.5} /><div><p className="text-xs font-medium">{title}</p><p className="mt-1 text-xs text-ink-faint">{note}</p></div>
            </li>)}
          </ul>
        </section>
      </aside>
    </div>
  )
})

function ResearchQuickForm({ disabled, onReady, onStart, defaultOpen = true }: {
  disabled: boolean
  onReady: (goal: string, fields: ResearchFields | null) => void
  onStart: (goal: string, fields: ResearchFields | null) => void
  defaultOpen?: boolean
}) {
  const [form, setForm] = useState<ResearchForm>({
    company: '', companyId: '', market: '', yearFrom: '', yearTo: '',
    caliber: '合并', asOf: '', materials: '', perspective: 'equity',
  })
  const [gaps, setGaps] = useState<string[]>([])
  const [open, setOpen] = useState(defaultOpen)
  const preview = buildResearchGoal(form)
  const fill = (start: boolean) => {
    if (preview.gaps.length) { setGaps(preview.gaps); return }
    setGaps([])
    if (start) onStart(preview.goal, preview.fields)
    else onReady(preview.goal, preview.fields)
  }

  return (
    <div>
      <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open}
        className="mb-4 flex w-full items-center gap-2 text-left text-xs text-ink-muted">
        <Building2 className="h-4 w-4 text-brand" />两期经营研究
        <span className="ml-auto text-brand">{open ? '收起' : '展开新研究表单'}</span>
        <ChevronDown className={`h-3.5 w-3.5 ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && <div className="space-y-4">
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <label className="research-field col-span-2 lg:col-span-4">研究哪家公司
            <input value={form.company ?? ''} placeholder="输入公司名称或股票代码，如 贵州茅台 / 600519.SH" disabled={disabled}
              onChange={event => setForm({ ...form, company: event.target.value })} />
          </label>
          <label className="research-field">起始年度
            <input value={String(form.yearFrom ?? '')} inputMode="numeric" maxLength={4} placeholder="如 2023" disabled={disabled}
              onChange={event => setForm({ ...form, yearFrom: event.target.value })} />
          </label>
          <label className="research-field">对比年度
            <input value={String(form.yearTo ?? '')} inputMode="numeric" maxLength={4} placeholder="如 2024" disabled={disabled}
              onChange={event => setForm({ ...form, yearTo: event.target.value })} />
          </label>
          <label className="research-field">报表口径
            <select value={form.caliber ?? '合并'} disabled={disabled} onChange={event => setForm({ ...form, caliber: event.target.value })}>
              <option value="合并">合并报表</option><option value="母公司">母公司报表</option>
            </select>
          </label>
          <label className="research-field">资料截至日
            <input type="date" value={form.asOf ?? ''} disabled={disabled} onChange={event => setForm({ ...form, asOf: event.target.value })} />
          </label>
        </div>
        <details className="rounded-lg border border-line bg-surface-sunken px-3 py-2.5">
          <summary className="text-xs text-ink-muted">市场、阅读重点与参考链接（选填）</summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label className="research-field">证券市场
              <select value={form.market ?? ''} disabled={disabled} onChange={event => setForm({ ...form, market: event.target.value })}>
                <option value="">未指定（保留市场缺口）</option><option value="cn">A 股 · 首期研究范围</option><option value="hk">港股 · 非首期重点</option><option value="us">美股 · 非首期重点</option>
              </select>
            </label>
            <label className="research-field">阅读重点
              <select value={form.perspective ?? 'equity'} disabled={disabled} onChange={event => setForm({ ...form, perspective: event.target.value })}>
                <option value="equity">个人 / 小团队投研</option><option value="bank_corporate">银行对公研究</option>
              </select>
            </label>
            <label className="research-field sm:col-span-2">公司稳定标识（可选）
              <input value={form.companyId ?? ''} placeholder="如 600519.SH，用于明确研究主体" disabled={disabled} onChange={event => setForm({ ...form, companyId: event.target.value })} />
            </label>
            <label className="research-field sm:col-span-2">参考资料（可选）
              <textarea value={form.materials ?? ''} rows={2} placeholder="年报链接或需要参考的材料" disabled={disabled} onChange={event => setForm({ ...form, materials: event.target.value })} />
            </label>
          </div>
        </details>
        {gaps.length > 0 && <ul role="alert" className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{gaps.map(gap => <li key={gap}>· {gap}</li>)}</ul>}
        <div className="flex flex-wrap items-center gap-3 pt-1">
          <button type="button" onClick={() => fill(true)} disabled={disabled} className="button-primary inline-flex items-center gap-2 rounded-lg px-5 py-3 text-sm font-medium">
            <ArrowUpRight className="h-4 w-4" />开始研究
          </button>
          <button type="button" onClick={() => fill(false)} disabled={disabled} className="rounded-lg border border-line px-3 py-3 text-xs text-ink-muted disabled:opacity-40">预览并编辑研究目标</button>
        </div>
        <p className="text-xs leading-relaxed text-ink-faint">聚焦收入、归母净利润与经营现金流。首期适用于 A 股非金融公司；缺失证据会保留缺口，不生成买卖建议。</p>
      </div>}
    </div>
  )
}
