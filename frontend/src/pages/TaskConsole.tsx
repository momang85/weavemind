import { useState, useCallback, useEffect, useMemo } from 'react'
import { Loader2, Sparkles, RefreshCw, Plus, MessagesSquare, FolderOpen, Activity, Eye, Play, FileText, ChevronDown, Upload } from 'lucide-react'
import { useTaskStore } from '../stores/useTaskStore'
import { useTaskPoller } from '../stores/useTaskPoller'
import TaskTreeView from '../components/TaskTreeView'
import LiveActivity from '../components/LiveActivity'
import ReportViewer from '../components/ReportViewer'
import StepInspector from '../components/StepInspector'
import type { TaskNode, ConversationMessage, TaskReport } from '../stores/types'

type Tab = 'live' | 'context' | 'results'

const CAPABILITIES = ['web_search', 'web_fetch', 'content_summary', 'code_execution',
  'data_loader', 'data_analyzer', 'model_trainer', 'report_generator', 'file_io', 'package']

function statusBadge(status: string) {
  const base = 'px-2 py-0.5 rounded-full text-[10px] font-semibold'
  if (status === 'SUCCESS') return `${base} bg-emerald-500/20 text-emerald-400`
  if (status === 'FAILED') return `${base} bg-red-500/20 text-red-400`
  return `${base} bg-cyan-500/20 text-cyan-400`
}

export default function TaskConsole() {
  const {
    planTree, status, report,
    startTask, addLog, demoMode, activeConversationId,
    setActiveConversation, setReport, reset,
    awaitingConfirm, revision, markPlanConfirmed, setLogs,
  } = useTaskStore()

  const [goal, setGoal] = useState('')
  const [lastGoal, setLastGoal] = useState('')
  const [taskId, setTaskId] = useState<string | null>(null)
  const [selectedStep, setSelectedStep] = useState<TaskNode | null>(null)
  const [tab, setTab] = useState<Tab>('live')
  const [convMessages, setConvMessages] = useState<ConversationMessage[]>([])
  const [recentTasks, setRecentTasks] = useState<any[]>([])
  const [confirmMode, setConfirmMode] = useState(false)
  const [editableSteps, setEditableSteps] = useState<any[]>([])
  const [newCap, setNewCap] = useState('content_summary')
  const [newInstr, setNewInstr] = useState('')
  const [templates, setTemplates] = useState<any[]>([])
  const [templateName, setTemplateName] = useState('')
  const [showContext, setShowContext] = useState(false)
  const [userContext, setUserContext] = useState('')
  const [importMsg, setImportMsg] = useState<{ name: string; status: string }[]>([])

  useTaskPoller(demoMode ? null : taskId)

  // 加载任务模板（模板复用：确定性步骤，跳过 LLM 规划）
  useEffect(() => {
    fetch('/api/templates').then(r => r.json()).then(d => setTemplates(d.templates ?? [])).catch(() => {})
  }, [])

  // 从 URL 恢复会话（历史页“继续对话”跳转）
  useEffect(() => {
    const conv = new URLSearchParams(window.location.search).get('conv')
    if (conv) {
      setActiveConversation(conv)
      setTab('context')
      loadConversation(conv)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const loadConversation = useCallback(async (convId: string) => {
    try {
      const res = await fetch('/api/conversations/' + convId)
      const data = await res.json()
      setConvMessages(data.messages ?? [])
    } catch { /* ignore */ }
  }, [])

  const loadRecentTasks = useCallback(async () => {
    try {
      const res = await fetch('/tasks')
      const data = await res.json()
      setRecentTasks((data.tasks ?? []).slice(0, 8))
    } catch { /* ignore */ }
  }, [])

  useEffect(() => { loadRecentTasks() }, [loadRecentTasks])

  // 当前任务完成后刷新会话消息
  useEffect(() => {
    if (status === 'completed' && activeConversationId) {
      loadConversation(activeConversationId)
      loadRecentTasks()
    }
  }, [status, activeConversationId, loadConversation, loadRecentTasks])

  const submit = useCallback(async (goalOverride?: string) => {
    const g = (goalOverride ?? goal).trim()
    if (!g || status === 'running') return

    if (demoMode) {
      startTask('demo-task-001')
      setTaskId('demo-task-001')
      return
    }

    setGoal('')
    setLastGoal(g)
    setTaskId(null)

    try {
      const body: any = { goal: g }
      if (activeConversationId) body.conversation_id = activeConversationId
      if (confirmMode) body.auto_run = false
      if (templateName) body.template = templateName
      if (userContext.trim()) body.context = userContext.trim()
      const res = await fetch('/task', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await res.json()
      if (!res.ok || !data.task_id) {
        addLog({ timestamp: new Date().toISOString(), type: 'error', message: data.error || 'Failed to submit task' })
        startTask('failed')
        return
      }
      const tid = data.task_id
      setTaskId(tid)
      startTask(tid)
      addLog({ timestamp: new Date().toISOString(), type: 'plan', agent: 'orchestrator', message: 'Submitted: ' + g.slice(0, 50) })

      // 进入/保持会话上下文
      if (data.conversation_id) {
        setActiveConversation(data.conversation_id)
        setConvMessages(prev => {
          const exists = prev.some(m => m.task_id === tid)
          return exists ? prev : [...prev, {
            task_id: tid, goal: g, status: 'PENDING',
            created_at: new Date().toISOString(),
          }]
        })
      }
    } catch {
      addLog({ timestamp: new Date().toISOString(), type: 'error', message: 'Failed to submit task' })
      startTask('failed')
    }
  }, [goal, status, demoMode, activeConversationId, confirmMode, templateName, userContext,
      startTask, addLog, setActiveConversation])

  // 计划待确认时，把后端计划同步到本地可编辑数组
  useEffect(() => {
    if (awaitingConfirm && planTree) {
      const children = planTree.children && planTree.children.length ? planTree.children : [planTree]
      setEditableSteps(children.map(c => ({
        step_id: c.step_id || c.id || ('s' + Math.random().toString(36).slice(2, 6)),
        capability: c.capability || 'content_summary',
        instruction: c.instruction || c.name || '',
        timeout: 120,
      })))
    }
  }, [awaitingConfirm, planTree])

  const moveStep = (id: string, dir: -1 | 1) => {
    setEditableSteps(prev => {
      const i = prev.findIndex(s => s.step_id === id)
      const j = i + dir
      if (i < 0 || j < 0 || j >= prev.length) return prev
      const next = [...prev]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })
  }

  const removeStep = (id: string) => setEditableSteps(prev => prev.filter(s => s.step_id !== id))

  const addStep = () => {
    const text = newInstr.trim()
    if (!text) return
    setEditableSteps(prev => [...prev, {
      step_id: 'e' + Math.random().toString(36).slice(2, 6),
      capability: newCap,
      instruction: text,
      timeout: 120,
    }])
    setNewInstr('')
  }

  const confirmPlan = async (action: 'confirm' | 'cancel') => {
    if (!taskId) return
    try {
      await fetch('/api/plan/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(action === 'confirm'
          ? { task_id: taskId, action, steps: editableSteps }
          : { task_id: taskId, action }),
      })
      markPlanConfirmed()
    } catch { /* ignore */ }
  }

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

  const editTree = useMemo<TaskNode | null>(() => {
    if (!awaitingConfirm || !planTree) return null
    return {
      id: 'root', step_id: 'root', capability: '', name: planTree.name || 'Plan',
      status: 'running', children: editableSteps.map(s => ({
        id: s.step_id, step_id: s.step_id, capability: s.capability,
        name: s.instruction || 'Step', status: 'pending', children: [],
      })),
    }
  }, [awaitingConfirm, editableSteps, planTree])

  const viewFullReport = useCallback(async (tid: string) => {
    try {
      const res = await fetch('/task/' + tid)
      const d = await res.json()
      const steps = d.steps ?? []
      const reportObj: TaskReport = {
        summary: d.status || 'SUCCESS',
        taskId: tid,
        stats: {
          totalSteps: steps.length,
          successSteps: steps.filter((s: any) => s.result?.status === 'SUCCESS').length,
          failedSteps: steps.filter((s: any) => s.result?.status === 'FAILED').length,
          duration: 0,
        },
        steps: steps.map((s: any) => ({
          step_id: s.step_id || '', capability: s.capability || '',
          name: s.instruction || 'Step', status: (s.result?.status || 'pending').toLowerCase(),
        })),
        final_report: d.report || d.final_report || '',
      }
      try {
        const dl = await (await fetch('/api/task/' + tid + '/deliverables')).json()
        reportObj.files = (dl.files ?? []).map((f: any) => ({
          name: f.name, size: f.size, kind: f.kind,
        }))
      } catch { /* ignore */ }
      // 历史任务也载入思考日志，让"实时动态"页可回看该任务的完整过程
      try {
        const lg = (d.logs ?? []).map((l: any, i: number) => ({
          id: 'srv-' + i,
          timestamp: l.timestamp || '',
          type: l.type || 'info',
          agent: l.agent || 'orchestrator',
          message: l.message || '',
        }))
        setLogs(lg)
      } catch { /* ignore */ }
      setReport(reportObj)
    } catch { /* ignore */ }
  }, [setReport, setLogs])

  const newConversation = useCallback(() => {
    reset()
    setConvMessages([])
    setTaskId(null)
    window.history.replaceState({}, '', window.location.pathname)
  }, [reset])

  const isRunning = status === 'running'
  // 步骤级流式输出（O-21）：运行中轮询 /api/task/<id>/stream 实时显示生成内容
  const [streamText, setStreamText] = useState('')
  useEffect(() => {
    if (!taskId || !isRunning) { setStreamText(''); return }
    let cancelled = false
    const poll = async () => {
      try {
        const d = await (await fetch('/api/task/' + taskId + '/stream')).json()
        if (!cancelled) setStreamText(d.text || '')
      } catch {}
    }
    poll()
    const t = setInterval(poll, 1500)
    return () => { cancelled = true; clearInterval(t) }
  }, [taskId, isRunning])

  const resultItems = activeConversationId
    ? convMessages.filter(m => m.status !== 'PENDING')
    : recentTasks.filter(t => t.status !== 'PENDING')

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      {/* 输入区 */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-1.5 flex items-end gap-2">
        <textarea value={goal}
          onChange={e => setGoal(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } }}
          placeholder={demoMode ? 'Demo mode' : 'Enter a task for your AI team...'}
          disabled={isRunning || demoMode}
          rows={2}
          className="flex-1 bg-transparent border-none text-slate-200 placeholder-slate-600 resize-none p-3 text-sm focus:outline-none disabled:opacity-50" />
        <div className="flex items-center gap-2 pb-1">
          {activeConversationId && (
            <span className="hidden sm:inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-violet-500/10 border border-violet-500/20 text-violet-400 text-xs">
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
          <label className="flex items-center gap-1.5 px-2 py-2 text-xs text-slate-400 cursor-pointer shrink-0">
            <input type="checkbox" checked={confirmMode}
              onChange={e => setConfirmMode(e.target.checked)}
              className="accent-cyan-500" />
            先确认计划
          </label>
          <button onClick={() => submit()} disabled={isRunning || (!goal.trim() && !demoMode)}
            className="flex items-center gap-2 bg-cyan-500 hover:bg-cyan-400 disabled:opacity-40 disabled:cursor-not-allowed text-slate-950 font-semibold px-5 py-2.5 rounded-lg transition-all text-sm shrink-0">
            {isRunning ? (<><Loader2 className="w-4 h-4 animate-spin" /> Running...</>) : (<><Sparkles className="w-4 h-4" /> Execute</>)}
          </button>
          {status === 'completed' && report?.summary === 'FAILED' && lastGoal && (
            <button onClick={() => submit(lastGoal)}
              className="flex items-center gap-2 bg-red-500/10 hover:bg-red-500/20 text-red-400 font-semibold px-5 py-2.5 rounded-lg transition-all text-sm border border-red-500/20 shrink-0">
              <RefreshCw className="w-4 h-4" /> Retry
            </button>
          )}
          <button onClick={newConversation} title="开始新对话"
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

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 min-h-[400px]">
        <div className="col-span-1 lg:col-span-2 bg-slate-900 border border-slate-800 rounded-xl p-5">
          <h2 className="flex items-center gap-2 text-slate-200 font-semibold text-sm mb-4">
            <span className="w-1.5 h-1.5 rounded-full bg-cyan-400" />
            {awaitingConfirm ? '计划待确认（可编辑）' : 'Execution Plan'}
          </h2>
          {awaitingConfirm && (
            <div className="mb-3 text-xs text-amber-400/90 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
              可上下移动、删除步骤或添加新步骤；确认后按你的计划执行。
            </div>
          )}
          {planTree ? (
            <TaskTreeView root={editTree ?? planTree} onSelect={setSelectedStep}
              editable={awaitingConfirm} onMove={moveStep} onDelete={removeStep} />
          ) : (
            <div className="flex flex-col items-center justify-center h-64 text-slate-600">
              <Sparkles className="w-8 h-8 mb-3 opacity-20" />
              <p className="text-sm">{isRunning ? 'Generating plan...' : 'Execution plan will appear here'}</p>
              <p className="text-xs mt-1 opacity-60">{isRunning ? 'Orchestrator working' : 'Submit a task to begin'}</p>
            </div>
          )}
          {awaitingConfirm && (
            <div className="mt-4 space-y-3">
              {revision && (
                <div className="flex items-center gap-2 text-xs text-amber-400 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
                  <RefreshCw className="w-3.5 h-3.5 shrink-0" />
                  搜索未获得可用结果，已建议改为直接生成。确认后按修订计划执行。
                </div>
              )}
              <div className="flex gap-2">
                <select value={newCap} onChange={e => setNewCap(e.target.value)}
                  className="bg-slate-800 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-slate-300 shrink-0">
                  {CAPABILITIES.map(c => <option key={c} value={c}>{c}</option>)}
                </select>
                <input value={newInstr} onChange={e => setNewInstr(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') addStep() }}
                  placeholder="新步骤指令（回车添加）..."
                  className="flex-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 placeholder-slate-600 focus:outline-none" />
                <button onClick={addStep}
                  className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-xs shrink-0">
                  <Plus className="w-3.5 h-3.5 inline" /> 添加
                </button>
              </div>
              <div className="flex gap-2">
                <button onClick={() => confirmPlan('confirm')}
                  className="flex-1 flex items-center justify-center gap-2 py-2.5 bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 rounded-lg text-sm border border-emerald-500/30 transition-colors">
                  <Play className="w-4 h-4" /> 确认并执行（{editableSteps.length} 步）
                </button>
                <button onClick={() => confirmPlan('cancel')}
                  className="px-4 py-2.5 bg-red-500/10 hover:bg-red-500/20 text-red-400 rounded-lg text-sm border border-red-500/20 transition-colors">
                  放弃
                </button>
              </div>
            </div>
          )}
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden flex flex-col">
          {/* 标签页：实时动态 / 对话上下文 / 项目结果 */}
          <div className="flex border-b border-slate-800 shrink-0">
            {([
              { key: 'live', label: '实时动态', icon: Activity },
              { key: 'context', label: '对话', icon: MessagesSquare },
              { key: 'results', label: '项目结果', icon: FolderOpen },
            ] as { key: Tab; label: string; icon: any }[]).map(t => (
              <button key={t.key} onClick={() => setTab(t.key)}
                className={`flex-1 flex items-center justify-center gap-1.5 py-2.5 text-xs transition-colors ${
                  tab === t.key ? 'text-cyan-400 bg-cyan-500/5 border-b-2 border-cyan-400' : 'text-slate-500 hover:text-slate-300'
                }`}>
                <t.icon className="w-3.5 h-3.5" /> {t.label}
              </button>
            ))}
          </div>

          <div className="flex-1 min-h-[360px] overflow-y-auto p-4">
            {tab === 'live' && (
              <>
                {streamText && (
                  <div className="mb-4 bg-slate-900/80 border border-cyan-500/20 rounded-xl p-3">
                    <div className="text-[10px] text-cyan-400 font-semibold mb-1">
                      生成内容（流式）· 实时
                    </div>
                    <pre className="whitespace-pre-wrap break-all text-slate-300 text-xs font-sans max-h-48 overflow-y-auto">
                      {streamText}
                    </pre>
                  </div>
                )}
                <LiveActivity />
              </>
            )}

            {tab === 'context' && (
              <div className="space-y-3">
                {convMessages.length === 0 && (
                  <div className="text-center text-slate-600 text-xs py-10">
                    尚无对话。提交任务后，后续输入将自动延续同一上下文；也可在“历史”页切换旧对话。
                  </div>
                )}
                {convMessages.map(m => (
                  <div key={m.task_id} className="space-y-2">
                    <div className="ml-auto max-w-[85%] bg-cyan-500/10 border border-cyan-500/20 text-slate-200 text-xs rounded-lg rounded-tr-none px-3 py-2">
                      {m.goal}
                    </div>
                    <div className="max-w-[95%] bg-slate-800/50 border border-slate-800 text-slate-400 text-xs rounded-lg rounded-tl-none px-3 py-2">
                      <div className="flex items-center gap-2 mb-1">
                        <span className={statusBadge(m.status)}>{m.status}</span>
                        <span className="text-slate-600">{new Date(m.created_at).toLocaleTimeString()}</span>
                      </div>
                      <pre className="whitespace-pre-wrap break-all max-h-32 overflow-y-auto font-sans">
                        {m.report_preview || '（运行中...）'}
                      </pre>
                      {(m.status === 'SUCCESS' || m.status === 'FAILED') && (
                        <div className="flex gap-2 mt-2">
                          <button onClick={() => viewFullReport(m.task_id)}
                            className="flex items-center gap-1 text-[10px] text-cyan-400 hover:text-cyan-300">
                            <Eye className="w-3 h-3" /> 查看完整报告
                          </button>
                          <button onClick={() => submit(m.goal)}
                            className="flex items-center gap-1 text-[10px] text-violet-400 hover:text-violet-300">
                            <RefreshCw className="w-3 h-3" /> 重跑
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {tab === 'results' && (
              <div className="space-y-2">
                <p className="text-[11px] text-slate-500 mb-3">
                  {activeConversationId ? '当前对话的完成结果' : '最近完成的任务'}
                </p>
                {resultItems.length === 0 && (
                  <div className="text-center text-slate-600 text-xs py-10">暂无完成结果</div>
                )}
                {resultItems.map(t => (
                  <div key={t.task_id} className="bg-slate-800/40 border border-slate-800 rounded-lg px-3 py-2">
                    <div className="flex items-center gap-2 mb-1">
                      <span className={statusBadge(t.status)}>{t.status}</span>
                      <span className="text-slate-300 text-xs truncate flex-1">{t.goal}</span>
                    </div>
                    <div className="flex gap-3">
                      <button onClick={() => viewFullReport(t.task_id)}
                        className="flex items-center gap-1 text-[10px] text-cyan-400 hover:text-cyan-300">
                        <Eye className="w-3 h-3" /> 查看
                      </button>
                      <button onClick={() => submit(t.goal)}
                        className="flex items-center gap-1 text-[10px] text-violet-400 hover:text-violet-300">
                        <RefreshCw className="w-3 h-3" /> 重新运行
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      <ReportViewer />

      {selectedStep && <StepInspector node={selectedStep} onClose={() => setSelectedStep(null)} />}
    </div>
  )
}
