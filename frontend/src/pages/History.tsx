import { useState, useEffect, useCallback } from 'react'
import { useTaskStore } from '../stores/useTaskStore'
import type { ConversationSummary, ConversationMessage, TaskSummary } from '../stores/types'
import { FileText, ChevronDown, ChevronRight, MessagesSquare, Play } from 'lucide-react'

function statusBadge(s: string) {
  const base = 'px-2.5 py-0.5 rounded-full text-xs font-semibold'
  if (s === 'SUCCESS') return `${base} bg-emerald-500/20 text-emerald-400`
  if (s === 'FAILED') return `${base} bg-red-500/20 text-red-400`
  if (s === 'SUCCESS_WITH_ISSUES') return `${base} bg-amber-500/20 text-amber-400`
  return `${base} bg-cyan-500/20 text-cyan-400`
}

export default function History() {
  const [mode, setMode] = useState<'conv' | 'tasks'>('conv')
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [convMessages, setConvMessages] = useState<Record<string, ConversationMessage[]>>({})
  const [fullReports, setFullReports] = useState<Record<string, string>>({})
  // P0-1：SUCCESS_WITH_ISSUES 任务的验收缺口明细（展开时按需加载）
  const [taskGaps, setTaskGaps] = useState<Record<string, string[]>>({})
  const { fetchSystemStatus } = useTaskStore()

  const loadConversations = useCallback(async () => {
    try {
      const res = await fetch('/api/conversations')
      const data = await res.json()
      setConversations(data.conversations ?? [])
    } catch {}
  }, [])

  const loadTasks = useCallback(async () => {
    try {
      const res = await fetch('/tasks')
      const data = await res.json()
      setTasks(data.tasks ?? [])
    } catch {}
  }, [])

  useEffect(() => {
    loadConversations()
    loadTasks()
    fetchSystemStatus()
  }, [loadConversations, loadTasks, fetchSystemStatus])

  // P0-1：展开 SUCCESS_WITH_ISSUES 任务时加载验收缺口
  useEffect(() => {
    tasks.filter(t => t.status === 'SUCCESS_WITH_ISSUES' && expanded.has('task-' + t.task_id))
      .forEach(async t => {
        if (taskGaps[t.task_id]) return
        try {
          const d = await (await fetch('/task/' + t.task_id)).json()
          setTaskGaps(prev => ({
            ...prev,
            [t.task_id]: (d.acceptance && Array.isArray(d.acceptance.gaps))
              ? d.acceptance.gaps : [],
          }))
        } catch { /* ignore */ }
      })
  }, [tasks, expanded, taskGaps])

  const toggle = (id: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const expandConversation = async (id: string) => {
    toggle(id)
    if (!convMessages[id]) {
      try {
        const res = await fetch('/api/conversations/' + id)
        const data = await res.json()
        setConvMessages(prev => ({ ...prev, [id]: data.messages ?? [] }))
      } catch {}
    }
  }

  const viewFullReport = async (tid: string) => {
    if (fullReports[tid]) return
    try {
      const res = await fetch('/task/' + tid)
      const d = await res.json()
      setFullReports(prev => ({ ...prev, [tid]: d.report || d.final_report || '' }))
    } catch {}
  }

  const continueConversation = (id: string) => {
    window.location.href = '/?conv=' + encodeURIComponent(id)
  }

  return (
    <div className="max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-slate-200 font-semibold text-lg">历史记录</h2>
        <div className="flex items-center gap-2">
          <div className="flex bg-slate-900 border border-slate-800 rounded-lg p-0.5">
            {([
              { key: 'conv', label: '对话' },
              { key: 'tasks', label: '任务' },
            ] as { key: 'conv' | 'tasks'; label: string }[]).map(m => (
              <button key={m.key} onClick={() => setMode(m.key)}
                className={`px-3 py-1 rounded-md text-xs transition-colors ${
                  mode === m.key ? 'bg-cyan-500/20 text-cyan-400' : 'text-slate-500 hover:text-slate-300'
                }`}>
                {m.label}
              </button>
            ))}
          </div>
          <button onClick={() => { loadConversations(); loadTasks() }}
            className="text-xs text-cyan-400 hover:text-cyan-300">刷新</button>
        </div>
      </div>

      {mode === 'conv' && (
        <div className="space-y-3">
          {conversations.length === 0 && (
            <div className="text-slate-600 text-sm text-center py-12">暂无对话记录</div>
          )}
          {conversations.map(c => (
            <div key={c.conversation_id} className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
              <div className="w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-slate-800/30 transition-colors">
                <button onClick={() => expandConversation(c.conversation_id)}
                  className="flex items-center gap-3 flex-1 min-w-0">
                  {expanded.has(c.conversation_id)
                    ? <ChevronDown className="w-4 h-4 text-slate-500 shrink-0" />
                    : <ChevronRight className="w-4 h-4 text-slate-500 shrink-0" />}
                  <MessagesSquare className="w-4 h-4 text-violet-400 shrink-0" />
                  <span className="flex-1 text-slate-300 text-sm truncate">{c.title || '(空对话)'}</span>
                  <span className="text-slate-600 text-xs shrink-0">{c.message_count} 条消息</span>
                  <span className={statusBadge(c.last_status || 'PENDING')}>{c.last_status || 'PENDING'}</span>
                  <span className="text-slate-600 text-xs shrink-0">
                    {new Date(c.last_updated).toLocaleString()}
                  </span>
                </button>
                <button onClick={() => continueConversation(c.conversation_id)}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-violet-500/10 hover:bg-violet-500/20 text-violet-400 text-xs shrink-0">
                  <Play className="w-3 h-3" /> 继续对话
                </button>
              </div>

              {expanded.has(c.conversation_id) && (convMessages[c.conversation_id] ?? []).length > 0 && (
                <div className="px-5 pb-4 border-t border-slate-800 space-y-2">
                  {convMessages[c.conversation_id].map(m => (
                    <div key={m.task_id} className="bg-slate-800/40 rounded-lg p-3">
                      <div className="flex items-start gap-2">
                        <span className="text-[10px] text-cyan-400 bg-cyan-500/10 px-1.5 py-0.5 rounded shrink-0 mt-0.5">用户</span>
                        <span className="text-slate-300 text-xs">{m.goal}</span>
                      </div>
                      <div className="flex items-center gap-2 mt-2">
                        <span className={statusBadge(m.status)}>{m.status}</span>
                        <button onClick={() => viewFullReport(m.task_id)}
                          className="text-[10px] text-cyan-400 hover:text-cyan-300">
                          查看报告
                        </button>
                      </div>
                      {fullReports[m.task_id] ? (
                        <pre className="mt-2 text-slate-400 text-xs whitespace-pre-wrap font-sans leading-relaxed max-h-64 overflow-y-auto">
                          {fullReports[m.task_id]}
                        </pre>
                      ) : (
                        m.report_preview && (
                          <pre className="mt-2 text-slate-500 text-xs whitespace-pre-wrap font-sans line-clamp-3">
                            {m.report_preview}
                          </pre>
                        )
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {mode === 'tasks' && (
        <div className="space-y-3">
          {tasks.length === 0 ? (
            <div className="text-slate-600 text-sm text-center py-12">No tasks yet</div>
          ) : (
            tasks.map(t => (
              <div key={t.task_id} className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
                <button
                  onClick={() => toggle('task-' + t.task_id)}
                  className="w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-slate-800/30 transition-colors"
                >
                  {expanded.has('task-' + t.task_id)
                    ? <ChevronDown className="w-4 h-4 text-slate-500" />
                    : <ChevronRight className="w-4 h-4 text-slate-500" />}
                  <FileText className="w-4 h-4 text-cyan-400" />
                  <span className="flex-1 text-slate-300 text-sm truncate">{t.goal}</span>
                  {t.project && (
                    <span className="text-[10px] px-2 py-0.5 rounded bg-slate-700/60 text-slate-400 shrink-0">
                      {t.project}
                    </span>
                  )}
                  <span className={statusBadge(t.status)}>{t.status}</span>
                  <span className="text-slate-600 text-xs">{new Date(t.created_at).toLocaleDateString()}</span>
                </button>
                {expanded.has('task-' + t.task_id) && t.report && (
                  <div className="px-5 pb-4 border-t border-slate-800">
                    <pre className="mt-3 text-slate-400 text-xs whitespace-pre-wrap font-sans leading-relaxed max-h-96 overflow-y-auto">
                      {t.report}
                    </pre>
                  </div>
                )}
                {expanded.has('task-' + t.task_id) && t.status === 'SUCCESS_WITH_ISSUES' && (
                  <div className="px-5 pb-4 border-t border-slate-800 text-amber-300/90 text-xs space-y-1">
                    <div className="mt-2 font-semibold">已完成但有验收缺口：</div>
                    {(taskGaps[t.task_id] || []).length === 0 && <div>（无缺口明细）</div>}
                    {(taskGaps[t.task_id] || []).map((g, i) => <div key={i}>- {g}</div>)}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}
