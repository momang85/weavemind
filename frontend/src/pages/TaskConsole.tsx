import { useState, useCallback, useEffect } from 'react'
import { useTaskStore } from '../stores/useTaskStore'
import { useTaskLive } from '../stores/useTaskLive'
import ReportViewer from '../components/ReportViewer'
import StepInspector from '../components/StepInspector'
import SubmitPanel from '../components/console/SubmitPanel'
import PlanPanel from '../components/console/PlanPanel'
import ConsoleSideTabs from '../components/console/ConsoleSideTabs'
import type { TaskNode, ConversationMessage, TaskReport } from '../stores/types'

type Tab = 'live' | 'context' | 'results'

/** 任务控制台（T11c 拆分后的薄编排页）：
 * 提交区/计划面板/右侧三栏各自独立组件；本页只保留任务生命周期编排。 */
export default function TaskConsole() {
  const planTree = useTaskStore(s => s.planTree)
  const status = useTaskStore(s => s.status)
  const report = useTaskStore(s => s.report)
  const demoMode = useTaskStore(s => s.demoMode)
  const activeConversationId = useTaskStore(s => s.activeConversationId)
  const awaitingConfirm = useTaskStore(s => s.awaitingConfirm)
  const revision = useTaskStore(s => s.revision)
  const currentTaskId = useTaskStore(s => s.currentTaskId)
  const startTask = useTaskStore(s => s.startTask)
  const addLog = useTaskStore(s => s.addLog)
  const setActiveConversation = useTaskStore(s => s.setActiveConversation)
  const setReport = useTaskStore(s => s.setReport)
  const reset = useTaskStore(s => s.reset)
  const updatePlan = useTaskStore(s => s.updatePlan)
  const markPlanConfirmed = useTaskStore(s => s.markPlanConfirmed)
  const setLogs = useTaskStore(s => s.setLogs)

  const [goal, setGoal] = useState('')
  const [project, setProject] = useState('default')
  const [lastGoal, setLastGoal] = useState('')
  // 运行中任务切页恢复：taskId 迁入 store（重挂载自动恢复跟踪，不再孤儿化）
  const taskId = currentTaskId
  const [selectedStep, setSelectedStep] = useState<TaskNode | null>(null)
  const [tab, setTab] = useState<Tab>('live')
  const [convMessages, setConvMessages] = useState<ConversationMessage[]>([])
  const [recentTasks, setRecentTasks] = useState<any[]>([])
  const [confirmMode, setConfirmMode] = useState(false)
  const [templateName, setTemplateName] = useState('')
  const [userContext, setUserContext] = useState('')
  const [importMsg, setImportMsg] = useState<{ name: string; status: string }[]>([])
  // P0-1：SUCCESS_WITH_ISSUES 的验收缺口明细（按任务缓存，点击展开）
  const [gapsFor, setGapsFor] = useState<Record<string, string[]>>({})

  useTaskLive(demoMode ? null : taskId)

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
      const msgs = data.messages ?? []
      setConvMessages(msgs)
      // BUG-8：历史/刷新载入后回放任务状态——
      // 运行中的任务 → 交给实时通道跟踪（执行计划/日志实时更新）；
      // 已结束的任务 → 一次性拉取持久化 steps/logs 回放执行计划
      const running = msgs.find((m: any) =>
        m.status === 'PENDING' || m.status === 'RUNNING')
      const last = msgs[msgs.length - 1]
      const targetId = running?.task_id || last?.task_id
      if (!targetId) return
      if (running?.task_id) {
        useTaskStore.setState({ currentTaskId: running.task_id })
      } else {
        fetch('/task/' + targetId).then(r => r.json()).then((d: any) => {
          if (!d || d.error) return
          if (Array.isArray(d.steps) && d.steps.length > 0) {
            updatePlan({
              id: 'root', capability: '',
              name: d.goal || 'Task',
              status: d.status === 'SUCCESS' || d.status === 'SUCCESS_WITH_ISSUES'
                ? 'success' : d.status === 'FAILED' ? 'failed' : 'running',
              children: d.steps.map((s: any) => ({
                id: s.step_id || '', step_id: s.step_id || '',
                iteration: s.iteration || 0, capability: s.capability || '',
                name: s.instruction || s.name || 'Step',
                status: (s.result?.status || 'pending').toLowerCase(),
                children: [], agent_id: s.result?.agent_id || '',
                result: s.result || null,
              })),
            })
          }
          // 回放去重：实时通道可能已按相同 id 写入这些日志，
          // 直接追加会造成完成后日志翻倍/重复 key
          const existing = new Set(useTaskStore.getState().logs.map(l => l.id))
          ;(d.logs || []).forEach((lg: any) => {
            if (!lg || lg.id === undefined) return
            const lid = 'srv-' + lg.id
            if (existing.has(lid)) return
            addLog({
              id: lid, timestamp: lg.timestamp || '',
              type: lg.type || 'info', agent: lg.agent || 'orchestrator',
              message: lg.message || '',
            })
          })
        }).catch(() => {})
      }
    } catch { /* ignore */ }
  }, [updatePlan, addLog])

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
      return
    }

    setGoal('')
    setLastGoal(g)
    useTaskStore.setState({ currentTaskId: null })

    try {
      const body: any = { goal: g }
      if (project.trim()) body.project = project.trim()
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
        useTaskStore.setState({ status: 'idle' })
        return
      }
      const tid = data.task_id
      startTask(tid)
      // D17：新任务重置过期 UI（步骤检查器/验收缺口缓存）
      setSelectedStep(null)
      setGapsFor({})
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
      useTaskStore.setState({ status: 'idle' })
    }
  }, [goal, project, status, demoMode, activeConversationId, confirmMode, templateName, userContext,
      startTask, addLog, setActiveConversation])

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
          duration: Math.max(0, Math.round((Date.now() - (useTaskStore.getState().startedAt || Date.now())) / 1000)),
        },
        steps: steps.map((s: any) => ({
          step_id: s.step_id || '', capability: s.capability || '',
          name: s.instruction || 'Step', status: (s.result?.status || 'pending').toLowerCase(),
        })),
        final_report: d.report || d.final_report || '',
      }
      // P0-1：随任务详情缓存验收缺口摘要（报告视图顶部横幅也用）
      const acc = d.acceptance
      if (acc && Array.isArray(acc.gaps)) {
        reportObj.acceptance = { overall: acc.overall || d.status, gaps: acc.gaps }
        setGapsFor(prev => ({ ...prev, [tid]: acc.gaps }))
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
      // 历史报告查看不改写运行态：运行中打开历史报告不应把全局
      // status 翻成 completed（会解锁重复提交、停掉流式轮询）
      const st = useTaskStore.getState().status
      if (st !== 'running') setReport(reportObj)
    } catch { /* ignore */ }
  }, [setReport, setLogs])

  // P0-1：点击展开/收起 SUCCESS_WITH_ISSUES 任务的验收缺口明细
  const toggleGaps = useCallback(async (tid: string) => {
    if (gapsFor[tid]) {
      setGapsFor(prev => {
        const next = { ...prev }
        delete next[tid]
        return next
      })
      return
    }
    try {
      const res = await fetch('/task/' + tid)
      const d = await res.json()
      const gaps = (d.acceptance && Array.isArray(d.acceptance.gaps))
        ? d.acceptance.gaps : []
      setGapsFor(prev => ({ ...prev, [tid]: gaps }))
    } catch {
      setGapsFor(prev => ({ ...prev, [tid]: ['（无法加载验收缺口）'] }))
    }
  }, [gapsFor])

  const newConversation = useCallback(() => {
    reset()
    setConvMessages([])
    useTaskStore.setState({ currentTaskId: null })
    window.history.replaceState({}, '', window.location.pathname)
  }, [reset])

  const isRunning = status === 'running'
  const resultItems = activeConversationId
    ? convMessages.filter(m => m.status !== 'PENDING')
    : recentTasks.filter(t => t.status !== 'PENDING')

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      <SubmitPanel
        key={taskId || 'idle'}
        goal={goal} setGoal={setGoal}
        project={project} setProject={setProject}
        confirmMode={confirmMode} setConfirmMode={setConfirmMode}
        templateName={templateName} setTemplateName={setTemplateName}
        userContext={userContext} setUserContext={setUserContext}
        importMsg={importMsg} setImportMsg={setImportMsg}
        isRunning={isRunning} demoMode={demoMode}
        activeConversationId={activeConversationId}
        lastGoal={lastGoal} reportSummary={report?.summary} status={status}
        onSubmit={submit} onNewConversation={newConversation} />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 min-h-[400px]">
        <PlanPanel
          planTree={planTree} awaitingConfirm={awaitingConfirm} revision={revision}
          isRunning={isRunning} taskId={taskId}
          markPlanConfirmed={markPlanConfirmed} onSelectStep={setSelectedStep} />

        <ConsoleSideTabs
          tab={tab} setTab={setTab} taskId={taskId} isRunning={isRunning}
          convMessages={convMessages}
          resultItems={resultItems} activeConversationId={activeConversationId}
          gapsFor={gapsFor} onToggleGaps={toggleGaps}
          onViewReport={viewFullReport} onSubmit={submit} />
      </div>

      <ReportViewer />

      {selectedStep && <StepInspector node={selectedStep} onClose={() => setSelectedStep(null)} />}
    </div>
  )
}
