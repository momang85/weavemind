import { useEffect, useState } from 'react'
import { Loader2, Sparkles, RefreshCw, Plus, MessagesSquare, FileText, ChevronDown, Upload, Zap, ExternalLink } from 'lucide-react'

/** 快答结果卡片：正文 + 来源链接 + 诚实降级标签。 */
function QuickAnswerCard({ qa }: { qa: any }) {
  return (
    <div className="bg-cyan-500/5 border border-cyan-500/20 rounded-xl p-3 space-y-2">
      <div className="flex items-center gap-2 text-[10px]">
        <Zap className="w-3 h-3 text-cyan-400" />
        <span className="text-cyan-400 font-semibold">快答</span>
        {qa.mode === 'searched' ? (
          <span className="text-emerald-400">已检索 {qa.sources?.length || 0} 条来源 · {qa.searched_at}</span>
        ) : qa.mode === 'error' ? (
          <span className="text-red-400">请求失败</span>
        ) : (
          <span className="text-amber-400">模型知识 · 未检索（未验证）</span>
        )}
        <span className="text-slate-600 ml-auto">{qa.duration}s</span>
      </div>
      <pre className="whitespace-pre-wrap break-all text-slate-300 text-xs font-sans max-h-64 overflow-y-auto">{qa.content}</pre>
      {qa.sources?.length > 0 && (
        <div className="space-y-1 pt-1 border-t border-slate-800">
          <div className="text-[10px] text-slate-500">参考来源：</div>
          {qa.sources.map((s: any, i: number) => (
            <a key={i} href={s.url} target="_blank" rel="noreferrer"
              className="flex items-center gap-1 text-[10px] text-cyan-400 hover:text-cyan-300 truncate">
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
export default function SubmitPanel({
  goal, setGoal, project, setProject, confirmMode, setConfirmMode,
  templateName, setTemplateName,
  userContext, setUserContext, importMsg, setImportMsg,
  isRunning, demoMode, activeConversationId, lastGoal, reportSummary, status,
  onSubmit, onNewConversation,
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
  importMsg: { name: string; status: string }[]
  setImportMsg: React.Dispatch<React.SetStateAction<{ name: string; status: string }[]>>
  isRunning: boolean
  demoMode: boolean
  activeConversationId: string | null
  lastGoal: string
  reportSummary?: string
  status: string
  onSubmit: (goalOverride?: string) => void
  onNewConversation: () => void
}) {
  const [showContext, setShowContext] = useState(false)
  const [templates, setTemplates] = useState<any[]>([])

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
      setImportMsg(prev => [...prev, { name: f.name, status: '提取中...' }])
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
          setImportMsg(prev => [...prev, { name: f.name, status: `成功 ${d.chars} 字${d.truncated ? '（已截断）' : ''}` }])
        } else {
          setImportMsg(prev => [...prev, { name: f.name, status: d.error || '无内容' }])
        }
      } catch {
        setImportMsg(prev => [...prev, { name: f.name, status: '导入失败' }])
      }
    }
  }

  // T3 快答：先检索后作答（不走编排器，秒级返回，带来源）
  const [qaGoal, setQaGoal] = useState('')
  const [qa, setQa] = useState<any>(null)
  const [qaLoading, setQaLoading] = useState(false)
  const askQuick = async () => {
    const g = qaGoal.trim()
    if (!g || qaLoading) return
    setQaLoading(true); setQa(null)
    try {
      const res = await fetch('/api/quick-answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal: g }),
      })
      const d = await res.json()
      if (d.error) setQa({ content: d.error, sources: [], mode: 'error', duration: 0 })
      else setQa(d)
    } catch {
      setQa({ content: '请求失败，请稍后重试', sources: [], mode: 'error', duration: 0 })
    } finally {
      setQaLoading(false)
    }
  }

  return (
    <>
      {/* 快答栏（T3：秒级问答入口，检索优先、来源可见） */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-1.5 flex items-center gap-2">
        <Zap className="w-4 h-4 text-cyan-400 ml-2 shrink-0" />
        <input value={qaGoal}
          onChange={e => setQaGoal(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') askQuick() }}
          placeholder="快答：秒级问答（自动检索资讯并附来源），如「贵州茅台最新财报要点」"
          disabled={qaLoading}
          className="flex-1 min-w-0 bg-transparent border-none text-slate-200 placeholder-slate-600 p-2 text-sm focus:outline-none disabled:opacity-50" />
        <button onClick={askQuick} disabled={qaLoading || !qaGoal.trim()}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-cyan-500/10 hover:bg-cyan-500/20 text-cyan-400 rounded-lg text-xs transition-colors shrink-0 disabled:opacity-40 disabled:cursor-not-allowed">
          {qaLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5" />}
          快答
        </button>
      </div>
      {qa && <QuickAnswerCard qa={qa} />}

      {/* 输入区 */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-1.5 flex items-end gap-2">
        <textarea value={goal}
          onChange={e => setGoal(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSubmit() } }}
          placeholder={demoMode ? 'Demo mode' : 'Enter a task for your AI team...'}
          disabled={isRunning || demoMode}
          rows={1}
          className="flex-1 min-w-0 bg-transparent border-none text-slate-200 placeholder-slate-600 resize-none p-3 text-sm focus:outline-none disabled:opacity-50" />
        <div className="flex items-center gap-2 pb-1 shrink-0">
          {activeConversationId && (
            <span className="hidden sm:inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-violet-500/10 border border-violet-500/20 text-violet-400 text-xs whitespace-nowrap">
              <MessagesSquare className="w-3.5 h-3.5" /> 对话中
            </span>
          )}
          <select value={templateName}
            onChange={e => {
              const name = e.target.value
              setTemplateName(name)
              const tpl = templates.find(t => t.name === name)
              if (tpl && tpl.goal) setGoal(tpl.goal)
            }}
            className="hidden sm:block bg-slate-800 border border-slate-700 rounded-lg px-2 py-2 text-xs text-slate-300 shrink-0">
            <option value="">自定义任务</option>
            {templates.map(t => <option key={t.name} value={t.name}>{t.name}</option>)}
          </select>
          <input value={project}
            onChange={e => setProject(e.target.value)}
            placeholder="项目 (default)"
            title="任务所属项目（F1 多项目工作区）"
            className="hidden sm:block w-28 bg-slate-800 border border-slate-700 rounded-lg px-2 py-2 text-xs text-slate-300 placeholder-slate-600 shrink-0 focus:outline-none focus:border-cyan-500" />
          <label className="flex items-center gap-1.5 px-2 py-2 text-xs text-slate-400 cursor-pointer shrink-0">
            <input type="checkbox" checked={confirmMode}
              onChange={e => setConfirmMode(e.target.checked)}
              className="accent-cyan-500" />
            先确认计划
          </label>
          <button onClick={() => onSubmit()} disabled={isRunning || (!goal.trim() && !demoMode)}
            className="flex items-center gap-2 bg-cyan-500 hover:bg-cyan-400 disabled:opacity-40 disabled:cursor-not-allowed text-slate-950 font-semibold px-5 py-2.5 rounded-lg transition-all text-sm shrink-0">
            {isRunning ? (<><Loader2 className="w-4 h-4 animate-spin" /> Running...</>) : (<><Sparkles className="w-4 h-4" /> Execute</>)}
          </button>
          {status === 'completed' && reportSummary === 'FAILED' && lastGoal && (
            <button onClick={() => onSubmit(lastGoal)}
              className="flex items-center gap-2 bg-red-500/10 hover:bg-red-500/20 text-red-400 font-semibold px-5 py-2.5 rounded-lg transition-all text-sm border border-red-500/20 shrink-0">
              <RefreshCw className="w-4 h-4" /> Retry
            </button>
          )}
          <button onClick={onNewConversation} title="开始新对话"
            className="flex items-center gap-2 px-3 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors shrink-0">
            <Plus className="w-4 h-4" /> 新对话
          </button>
        </div>
      </div>

      {/* 导入上下文（可选） */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-1.5">
        <button onClick={() => setShowContext(!showContext)}
          className="w-full flex items-center gap-2 px-3 py-2 text-xs text-slate-400 hover:text-slate-300 text-left">
          <FileText className="w-3.5 h-3.5 text-cyan-400" />
          导入上下文（需求背景 / 参考资料 / 约束条件）
          <ChevronDown className={`w-3.5 h-3.5 ml-auto transition-transform ${showContext ? 'rotate-180' : ''}`} />
        </button>
        {showContext && (
          <div className="space-y-2">
            <textarea value={userContext}
              onChange={e => setUserContext(e.target.value)}
              rows={4}
              placeholder="粘贴需求背景、参考资料、URL、约束条件等（可选）。将随任务一起提供给 AI 团队，用于更准确地满足需求。"
              className="w-full bg-slate-800/50 border border-slate-700 rounded-lg p-3 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-cyan-500" />
            <div className="flex flex-wrap items-center gap-2">
              <input id="ctx-file-input" type="file" multiple
                accept=".txt,.md,.csv,.json,.log,.py,.yaml,.yml,.pdf,.docx,.xlsx,.xls"
                className="hidden"
                onChange={e => { importFiles(e.target.files); e.target.value = '' }} />
              <label htmlFor="ctx-file-input"
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs cursor-pointer">
                <Upload className="w-3.5 h-3.5" /> 导入文件（txt/md/csv/json/pdf/docx/xlsx...）
              </label>
              <span className="text-[11px] text-slate-600">文件内容将自动追加到上下文中</span>
            </div>
            {importMsg.length > 0 && (
              <div className="space-y-1">
                {importMsg.map((m, i) => (
                  <div key={i} className="text-[11px] flex gap-2">
                    <span className="text-slate-400 truncate max-w-[200px]">{m.name}</span>
                    <span className={m.status.startsWith('成功') ? 'text-emerald-400' : 'text-amber-400'}>{m.status}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}
