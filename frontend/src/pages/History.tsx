import { useState, useEffect, useCallback } from 'react'
import { useTaskStore } from '../stores/useTaskStore'
import type { ConversationSummary, ConversationMessage, TaskSummary } from '../stores/types'
import { FileText, ChevronDown, ChevronRight, MessagesSquare, Play, PlayCircle } from 'lucide-react'
import { ReportMarkdown } from '../components/ReportViewer'
import { EmptyState, StatusBadge } from '../components/ui'
import { normalizeDeliverySummary } from '../lib/format'
import { formatLocalTime } from '../lib/format'
import { requestResume, resumeBlockedReason, resumeButtonState, resumeVisible, beginResumeNote, applyResumeResult, type ResumeNote } from '../lib/resume'

/** 后端列表接口的单页条数上限（web_ui.py `_list_tasks(50)` / `_list_conversations(50)`）。 */
const LIST_LIMIT = 50

/** 命中上限时如实告知"只显示了最近 N 条"，并指向能看到总条数的地方。 */
function ListLimitNote({ shown, noun }: { shown: number; noun: string }) {
  if (shown < LIST_LIMIT) return (
    <p className="text-xs text-ink-faint px-1">共 {shown} 条{noun}。</p>
  )
  return (
    <p className="text-xs text-amber-700/80 px-1">
      只显示最近 {LIST_LIMIT} 条{noun}（按时间倒序）——更早的记录没有列在这里，
      并不代表已丢失；完整条数见「指标看板」的「总任务数」。
    </p>
  )
}

export default function History() {
  const [mode, setMode] = useState<'conv' | 'tasks'>('conv')
  const [query, setQuery] = useState('')
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [convMessages, setConvMessages] = useState<Record<string, ConversationMessage[]>>({})
  const [fullReports, setFullReports] = useState<Record<string, string>>({})
  // P0-1：SUCCESS_WITH_ISSUES 任务的验收缺口明细（展开时按需加载）
  const [taskGaps, setTaskGaps] = useState<Record<string, string[]>>({})
  // U90-B：「继续执行」的点击回执（逐任务）；U90-R3：回执改为三态
  // （pending/ok/failed），失败且可重试时按钮保持可用，不再永久禁用
  const [resumeNote, setResumeNote] = useState<Record<string, ResumeNote>>({})
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

  // U90-B：失败任务就地「继续执行」——不重复整单，只跑未完步骤；拿到回执再刷新列表，
  // 状态与按钮态随之更新（不乐观改状态，避免"看起来恢复了其实没有"）。
  // U90-R3：只挡在途请求（双击去重）；失败/超时回执保留在按钮旁并**允许再点**，
  // 只有服务端判定不可重试（终态/已确认被接管）才禁用。
  const doResume = async (tid: string) => {
    const note = resumeNote[tid]
    if (note?.phase === 'pending' || note?.phase === 'ok') return
    setResumeNote(prev => beginResumeNote(prev, tid))
    const r = await requestResume(tid)
    setResumeNote(prev => applyResumeResult(prev, tid, r))
    if (r.ok) loadTasks()
  }

  const search = query.trim().toLowerCase()
  const shownConversations = conversations.filter(item => `${item.title || ''} ${item.conversation_id}`.toLowerCase().includes(search))
  const shownTasks = tasks.filter(item => `${item.goal || ''} ${item.task_id} ${item.project || ''}`.toLowerCase().includes(search))
  const noMatch = search && (mode === 'conv' ? conversations.length > 0 && shownConversations.length === 0 : tasks.length > 0 && shownTasks.length === 0)

  return (
    <div className="max-w-4xl mx-auto">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-5">
        <div><p className="eyebrow text-brand">RESEARCH ARCHIVE</p><h2 className="editorial-title mt-1 text-2xl text-ink">每一次研究，都有迹可循</h2></div>
        <div className="flex items-center gap-2">
          <div className="flex bg-surface border border-line rounded-lg p-0.5">
            {([
              { key: 'conv', label: '对话' },
              { key: 'tasks', label: '任务' },
            ] as { key: 'conv' | 'tasks'; label: string }[]).map(m => (
              <button key={m.key} onClick={() => setMode(m.key)}
                className={`px-3 py-1 rounded-md text-xs transition-colors ${
                  mode === m.key ? 'bg-brand/20 text-brand' : 'text-ink-faint hover:text-ink'
                }`}>
                {m.label}
              </button>
            ))}
          </div>
          <button onClick={() => { loadConversations(); loadTasks() }} aria-label="刷新历史记录"
            className="text-xs text-brand hover:text-brand">刷新</button>
        </div>
      </div>

      <label className="research-field mb-2"><span className="sr-only">搜索已加载的研究记录</span>
        <input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="按公司、研究问题、任务编号或项目搜索" />
      </label>
      <p className="mb-5 text-xs text-ink-faint">仅筛选当前已加载的最近 {LIST_LIMIT} 条记录。展开可查看报告、缺口与继续执行入口。</p>
      {noMatch && <EmptyState title="没有匹配的研究" description="尝试公司名称、任务编号或清空搜索；此处仅搜索当前已加载的记录。" action={<button onClick={() => setQuery('')} className="text-xs text-brand underline">清空搜索</button>} />}

      {mode === 'conv' && (
        <div className="space-y-3">
          {conversations.length > 0 && <ListLimitNote shown={conversations.length} noun="对话" />}
          {conversations.length === 0 && (
            <EmptyState title="暂无对话记录"
              description="在任务控制台提交一个任务后，这里会出现对话与报告入口；也可以点右上「任务」按单次任务查看。" />
          )}
          {shownConversations.map(c => (
            <div key={c.conversation_id} className="bg-surface border border-line rounded-xl overflow-hidden">
              <div className="w-full flex flex-wrap items-center gap-x-3 gap-y-2 px-4 sm:px-5 py-4 text-left hover:bg-surface-raised/30 transition-colors">
                <button onClick={() => expandConversation(c.conversation_id)}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1.5 flex-1 min-w-0 text-left">
                  {expanded.has(c.conversation_id)
                    ? <ChevronDown className="w-4 h-4 text-ink-faint shrink-0" />
                    : <ChevronRight className="w-4 h-4 text-ink-faint shrink-0" />}
                  <MessagesSquare className="w-4 h-4 text-violet-700 shrink-0" />
                  <span className="flex-1 min-w-[9rem] text-ink text-sm truncate">{c.title || '(空对话)'}</span>
                  <span className="text-ink-faint text-xs shrink-0">{c.message_count} 条消息</span>
                  <span className="shrink-0"><StatusBadge status={c.last_status || 'PENDING'} /></span>
                  <span className="text-ink-faint text-xs shrink-0">
                    {formatLocalTime(c.last_updated)}
                  </span>
                </button>
                <button onClick={() => continueConversation(c.conversation_id)}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-violet-500/10 hover:bg-violet-500/20 text-violet-700 text-xs shrink-0">
                  <Play className="w-3 h-3" /> 继续对话
                </button>
              </div>

              {expanded.has(c.conversation_id) && (convMessages[c.conversation_id] ?? []).length > 0 && (
                <div className="px-5 pb-4 border-t border-line space-y-2">
                  {convMessages[c.conversation_id].map(m => (
                    <div key={m.task_id} className="bg-surface-raised/40 rounded-lg p-3">
                      <div className="flex items-start gap-2">
                        <span className="text-xs text-brand bg-brand/10 px-1.5 py-0.5 rounded shrink-0 mt-0.5">用户</span>
                        <span className="text-ink text-xs">{m.goal}</span>
                      </div>
                      <div className="flex items-center gap-2 mt-2">
                        <StatusBadge status={m.status} />
                        <button onClick={() => viewFullReport(m.task_id)}
                          className="text-xs text-brand hover:text-brand">
                          查看报告
                        </button>
                      </div>
                      {fullReports[m.task_id] ? (
                        <div className="mt-2 text-ink-muted text-xs leading-relaxed max-h-64 overflow-y-auto">
                          <ReportMarkdown md={fullReports[m.task_id]} sources={[]} />
                        </div>
                      ) : (
                        m.report_preview && (
                          <div className="mt-2 text-ink-faint text-xs leading-relaxed line-clamp-3">
                            <ReportMarkdown md={normalizeDeliverySummary(m.report_preview)} sources={[]} />
                          </div>
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
          {tasks.length > 0 && <ListLimitNote shown={tasks.length} noun="任务" />}
          {tasks.length === 0 ? (
            <EmptyState title="暂无任务记录"
              description="提交任务后，这里按单次任务列出状态、报告与验收缺口。" />
          ) : (
            shownTasks.map(t => (
              <div key={t.task_id} className="bg-surface border border-line rounded-xl overflow-hidden">
                <button
                  onClick={() => toggle('task-' + t.task_id)}
                  className="w-full flex flex-wrap items-center gap-x-3 gap-y-2 px-4 sm:px-5 py-4 text-left hover:bg-surface-raised/30 transition-colors"
                >
                  {expanded.has('task-' + t.task_id)
                    ? <ChevronDown className="w-4 h-4 text-ink-faint shrink-0" />
                    : <ChevronRight className="w-4 h-4 text-ink-faint shrink-0" />}
                  <FileText className="w-4 h-4 text-brand shrink-0" />
                  <span className="flex-1 min-w-[9rem] text-ink text-sm truncate">{t.goal}</span>
                  {t.project && (
                    <span className="text-xs px-2 py-0.5 rounded bg-line/60 text-ink-muted shrink-0">
                      {t.project}
                    </span>
                  )}
                  <span className="shrink-0"><StatusBadge status={t.status} /></span>
                  <span className="text-ink-faint text-xs">{formatLocalTime(t.created_at)}</span>
                </button>
                {expanded.has('task-' + t.task_id) && (
                  <div className="px-5 pt-4 border-t border-line flex items-center justify-between gap-3">
                    <span className="text-ink-faint text-xs">在工作台里看这份任务的报告、复算与导出版本</span>
                    <div className="flex items-center gap-2 shrink-0">
                      {resumeVisible(t.resume) && (() => {
                        // U90-R3：按钮态由回执三态推导——在途/已接管禁用，
                        // 失败（503 等）保持可点并给出失败原文与"重试"文案
                        const btn = resumeButtonState(resumeNote[t.task_id], t.resume)
                        return (
                          <>
                            <button
                              onClick={() => doResume(t.task_id)}
                              disabled={btn.disabled}
                              title={btn.hint || String(t.resume?.reason || '')}
                              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-emerald-500/10 text-emerald-700 border border-emerald-500/30 hover:bg-emerald-500/20 disabled:opacity-60"
                            >
                              <PlayCircle className="w-3 h-3" /> {btn.label}
                            </button>
                            {resumeNote[t.task_id] && (resumeNote[t.task_id].phase === 'failed' || resumeNote[t.task_id].phase === 'timeout') && (
                              <span className="text-xs text-amber-700/90 max-w-[26rem] truncate" title={btn.hint}>
                                {btn.hint}
                              </span>
                            )}
                          </>
                        )
                      })()}
                      <button
                        onClick={() => { window.location.href = '/?task=' + encodeURIComponent(t.task_id) }}
                        className="text-xs px-3 py-1.5 rounded-lg bg-brand/10 text-brand border border-brand/30 hover:bg-brand/20"
                      >
                        打开研究工作台
                      </button>
                    </div>
                  </div>
                )}
                {/* U90-B：失败原因与"为什么（不）能继续"——同一份后端判定，如实解释 */}
                {expanded.has('task-' + t.task_id) && t.status === 'FAILED' && (t.resume?.failure || t.resume?.reason) && (
                  <div className="px-5 pb-2 border-t border-line text-xs space-y-1">
                    {t.resume?.failure && <div className="text-red-700/90">上次失败：{t.resume.failure}</div>}
                    {t.resume?.reason && !t.resume?.failure && (
                      <div className="text-ink-faint">{t.resume.reason}</div>
                    )}
                  </div>
                )}
                {expanded.has('task-' + t.task_id) && t.status === 'CANCELLED' && resumeBlockedReason(t.resume) && (
                  <div className="px-5 pb-2 border-t border-line text-xs text-ink-faint">
                    {resumeBlockedReason(t.resume)}
                  </div>
                )}
                {expanded.has('task-' + t.task_id) && t.report && (
                  <div className="px-5 pb-4 border-t border-line">
                    <div className="mt-3 text-ink-muted text-xs leading-relaxed max-h-96 overflow-y-auto">
                      <ReportMarkdown md={t.report} sources={[]} />
                    </div>
                  </div>
                )}
                {expanded.has('task-' + t.task_id) && t.status === 'SUCCESS_WITH_ISSUES' && (
                  <div className="px-5 pb-4 border-t border-line text-amber-700/90 text-xs space-y-1">
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
