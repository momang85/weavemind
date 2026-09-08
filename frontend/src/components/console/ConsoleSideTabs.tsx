import { memo, useEffect, useRef, useState } from 'react'
import { MessagesSquare, FolderOpen, Activity, Eye, RefreshCw } from 'lucide-react'
import LiveActivity from '../LiveActivity'

type Tab = 'live' | 'context' | 'results'

function statusBadge(status: string) {
  const base = 'px-2 py-0.5 rounded-full text-[10px] font-semibold'
  if (status === 'SUCCESS') return `${base} bg-emerald-500/20 text-emerald-400`
  if (status === 'FAILED') return `${base} bg-red-500/20 text-red-400`
  if (status === 'SUCCESS_WITH_ISSUES') return `${base} bg-amber-500/20 text-amber-400`
  return `${base} bg-cyan-500/20 text-cyan-400`
}

/** 右侧三栏：实时动态 / 对话上下文 / 项目结果（TaskConsole 拆分 T11c）。
 * 步骤级流式输出轮询（/api/task/{id}/stream）随本组件生命周期收敛。 */
export default memo(function ConsoleSideTabs({
  tab, setTab, taskId, isRunning,
  convMessages, resultItems, activeConversationId,
  gapsFor, onToggleGaps, onViewReport, onSubmit,
}: {
  tab: Tab
  setTab: (t: Tab) => void
  taskId: string | null
  isRunning: boolean
  convMessages: any[]
  resultItems: any[]
  activeConversationId: string | null
  gapsFor: Record<string, string[]>
  onToggleGaps: (tid: string) => void
  onViewReport: (tid: string) => void
  onSubmit: (goalOverride?: string) => void
}) {
  // 步骤级流式输出（O-21）：运行中且停留在"实时动态"标签时才轮询
  // /api/task/<id>/stream；内容不变不 setState（此前每 1.5s 无条件写入新字符串）
  const [streamText, setStreamText] = useState('')
  const streamRef = useRef<HTMLPreElement>(null)
  useEffect(() => {
    if (!taskId || !isRunning || tab !== 'live') { setStreamText(''); return }
    let cancelled = false
    let last = ''
    const poll = async () => {
      try {
        const d = await (await fetch('/api/task/' + taskId + '/stream')).json()
        const text = d.text || ''
        if (!cancelled && text !== last) { last = text; setStreamText(text) }
      } catch {}
    }
    poll()
    const t = setInterval(poll, 1500)
    return () => { cancelled = true; clearInterval(t) }
  }, [taskId, isRunning, tab])
  // 流式内容自动滚底
  useEffect(() => {
    if (streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight
  }, [streamText])

  return (
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
                <pre ref={streamRef} className="whitespace-pre-wrap break-all text-slate-300 text-xs font-sans max-h-48 overflow-y-auto">
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
                  {(m.status === 'SUCCESS' || m.status === 'FAILED' || m.status === 'SUCCESS_WITH_ISSUES') && (
                    <div className="flex gap-2 mt-2 flex-wrap">
                      <button onClick={() => onViewReport(m.task_id)}
                        className="flex items-center gap-1 text-[10px] text-cyan-400 hover:text-cyan-300">
                        <Eye className="w-3 h-3" /> 查看完整报告
                      </button>
                      <button onClick={() => onSubmit(m.goal)}
                        className="flex items-center gap-1 text-[10px] text-violet-400 hover:text-violet-300">
                        <RefreshCw className="w-3 h-3" /> 重跑
                      </button>
                      {m.status === 'SUCCESS_WITH_ISSUES' && (
                        <button onClick={() => onToggleGaps(m.task_id)}
                          className="flex items-center gap-1 text-[10px] text-amber-400 hover:text-amber-300">
                          {gapsFor[m.task_id] ? '收起' : '验收缺口'}
                        </button>
                      )}
                    </div>
                  )}
                  {m.status === 'SUCCESS_WITH_ISSUES' && gapsFor[m.task_id] && (
                    <div className="mt-2 text-amber-300/90 bg-amber-500/10 border border-amber-500/20 rounded p-2 text-[10px] space-y-1">
                      <div className="font-semibold">已完成但有验收缺口：</div>
                      {gapsFor[m.task_id].length === 0 && <div>（无缺口明细）</div>}
                      {gapsFor[m.task_id].map((g, i) => <div key={i}>- {g}</div>)}
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
                <div className="flex gap-3 flex-wrap">
                  <button onClick={() => onViewReport(t.task_id)}
                    className="flex items-center gap-1 text-[10px] text-cyan-400 hover:text-cyan-300">
                    <Eye className="w-3 h-3" /> 查看
                  </button>
                  <button onClick={() => onSubmit(t.goal)}
                    className="flex items-center gap-1 text-[10px] text-violet-400 hover:text-violet-300">
                    <RefreshCw className="w-3 h-3" /> 重新运行
                  </button>
                  {t.status === 'SUCCESS_WITH_ISSUES' && (
                    <button onClick={() => onToggleGaps(t.task_id)}
                      className="flex items-center gap-1 text-[10px] text-amber-400 hover:text-amber-300">
                      {gapsFor[t.task_id] ? '收起' : '验收缺口'}
                    </button>
                  )}
                </div>
                {t.status === 'SUCCESS_WITH_ISSUES' && gapsFor[t.task_id] && (
                  <div className="mt-2 text-amber-300/90 bg-amber-500/10 border border-amber-500/20 rounded p-2 text-[10px] space-y-1">
                    <div className="font-semibold">已完成但有验收缺口：</div>
                    {gapsFor[t.task_id].length === 0 && <div>（无缺口明细）</div>}
                    {gapsFor[t.task_id].map((g, i) => <div key={i}>- {g}</div>)}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
})
