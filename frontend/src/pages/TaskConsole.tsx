import { useState, useCallback, useEffect, useMemo, useRef } from 'react'
import { useTaskStore } from '../stores/useTaskStore'
import { useTaskLive } from '../stores/useTaskLive'
import ReportViewer from '../components/ReportViewer'
import StepInspector from '../components/StepInspector'
import SubmitPanel from '../components/console/SubmitPanel'
import PlanPanel from '../components/console/PlanPanel'
import ConsoleSideTabs from '../components/console/ConsoleSideTabs'
import FirstRunGuide from '../components/FirstRunGuide'
import { clearLastTask, readLastTask, saveLastTask, shouldResume } from '../lib/lastTask'
import { resolveSubmissionKey } from '../lib/submissionKey'
import { actionIntent, actionableTone, shouldShowActionable } from '../lib/actionable'
import type { TaskNode, ConversationMessage, TaskReport } from '../stores/types'
import type { ResearchFields } from '../lib/researchGoal'

type Tab = 'live' | 'context' | 'results' | 'materials'

/** C3/H3b：一次提交尝试的幂等键。
 *
 * 服务端按它去重：同一键的重复请求只创建一个任务、只执行一次。浏览器在
 * 127.0.0.1/localhost 下属安全上下文，优先用 `crypto.randomUUID()`；不可用时退回
 * 时间戳 + 随机数（仍然唯一到足够区分"同一次提交"）。 */
function newSubmissionKey(): string {
  try {
    const c = (globalThis as any).crypto
    if (c && typeof c.randomUUID === 'function') return 'sub-' + c.randomUUID()
  } catch { /* 退回下面的实现 */ }
  return 'sub-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12)
}

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
  const [importMsg, setImportMsg] = useState<{ name: string; note: string }[]>([])
  // P0-1：SUCCESS_WITH_ISSUES 的验收缺口明细（按任务缓存，点击展开）
  const [gapsFor, setGapsFor] = useState<Record<string, string[]>>({})
  // C3/H3b：同一次提交的幂等键（重复点击/失败重试复用；成功后清空）
  const pendingSubmission = useRef<{ sig: string; key: string } | null>(null)
  // C3：后端给的可行动状态（未接收/待消费/执行中/待材料/明确失败/已完成）
  const [actionable, setActionable] = useState<any | null>(null)
  // 交付节奏提示的数据源（ETA 用历史样本，已运行时长用 startedAt）
  const startedAt = useTaskStore(s => s.startedAt)
  const systemStatus = useTaskStore(s => s.systemStatus)

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

  // 运行中任务刷新后恢复跟踪（此前 taskId 只在内存，刷新即"孤儿化"：
  // 后端日志还在、页面空着）。只恢复**未终态**的任务——任务结束后刷新回到空白
  // 控制台是原有行为，不顺手改掉；已结束的恢复点顺手清掉，避免每次刷新都白查一次。
  useEffect(() => {
    if (taskId || demoMode) return
    const saved = readLastTask()
    if (!saved || saved.id.startsWith('demo-')) return
    let cancelled = false
    fetch('/task/' + saved.id)
      .then(r => (r.ok ? r.json() : null))
      .then((d: any) => {
        if (cancelled) return
        const st = String((d && d.status) || '')
        if (shouldResume(st)) {
          // 悬停态与提交路径一致：`useTaskLive` 只回写计划树/日志，不回写顶层 status，
          // 不补这一项的话恢复后顶栏没有"运行中/停止"，用户没法停掉它。
          useTaskStore.setState({
            currentTaskId: saved.id,
            startedAt: saved.startedAt || Date.now(),
            status: 'running',
          })
        } else if (st) {
          clearLastTask()   // **明确读到终态**才清恢复点
        }
        // 读不到（服务不可达/响应异常）就留着：下次刷新再试，别因为一次失败丢掉跟踪
      })
      .catch(() => { /* 同上：网络失败不清恢复点 */ })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, demoMode])

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
        saveLastTask(running.task_id)   // 与提交路径一致：刷新后仍能恢复跟踪
      } else {
        // C 批（09-28）实机反例：从会话打开**已结束**的任务时，这里只回放计划/日志，
        // **从不设置 currentTaskId** → 页面上的"可行动状态"（`/api/task/<id>/actionable`）
        // 与补材料面板都拿不到任务 id：接口明明返回 `{state:"failed",
        // action:"add_material"}`（实测 ui-c5loop-1），用户却看不到"该做什么"。
        // 用户是**明确点开会话**进来的，绑定这一条任务才对：这样"失败后该做什么"
        // 能显示、补材料/交付面板也指着同一个任务（终态任务不吃实时通道的重活）。
        useTaskStore.setState({ currentTaskId: targetId })
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

  // C3：可行动状态——"我现在该做什么"。状态变化或换任务时刷新一次轻量端点
  // （`/api/task/<id>/actionable`），不把大 payload 拉回来。
  //
  // C 批（09-28）实机反例：页面**根本不显示**这条提示——接口明明返回
  // `{state:"failed", action:"add_material", …}`（实测 ui-c5loop-1），页面却一片空白。
  // 根因是这里的取数竞态：依赖里带着 `revision`，而 `revision` 随实时同步频繁变化，
  // 每次变化都会把**上一次还没回来的请求**标记成"已失效"，于是结果永远落不了地
  // （`alive=false` 时既不设值也不置空）。现在改为：只在**换任务/状态变化**时取数，
  // 并用自增序号做"只认最后一次请求"，不再因为无关的重渲染丢掉结果。
  const actionableSeq = useRef(0)
  useEffect(() => {
    if (!currentTaskId) { setActionable(null); return }
    const seq = ++actionableSeq.current
    const tid = currentTaskId
    ;(async () => {
      try {
        const res = await fetch('/api/task/' + encodeURIComponent(tid) + '/actionable')
        if (seq !== actionableSeq.current) return
        if (!res.ok) { setActionable(null); return }
        const data = await res.json()
        setActionable(data && data.state ? data : null)
      } catch { if (seq === actionableSeq.current) setActionable(null) }
    })()
  }, [currentTaskId, status])

  // 当前任务完成后刷新会话消息
  useEffect(() => {
    if (status === 'completed' && activeConversationId) {
      loadConversation(activeConversationId)
      loadRecentTasks()
    }
  }, [status, activeConversationId, loadConversation, loadRecentTasks])

  const submit = useCallback(async (goalOverride?: string,
                                   fields?: ResearchFields | null) => {
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
      // A 批：研究契约随任务提交（提交时落库）——底稿只读它，抓取元数据不作数
      if (fields) body.research_request = fields
      // C3/H3b：幂等键——**同一次提交**的重复请求（双击、失败后重试）复用同一个键，
      // 服务端据此只创建一个任务、只执行一次；成功或改题后换新键，
      // 所以"用户主动再跑一次"仍是新任务（不把正常重跑当成重复）。
      const picked = resolveSubmissionKey(pendingSubmission.current, {
        goal: g, project, conversationId: activeConversationId || '',
        confirmMode, templateName, context: userContext, fields,
      }, newSubmissionKey)
      pendingSubmission.current = picked.pending
      body.idempotency_key = picked.key
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
      // 提交已被接收：清掉待重试的键，后续再点就是一次新的提交
      pendingSubmission.current = null
      if (data.deduplicated) {
        addLog({ timestamp: new Date().toISOString(), type: 'info', agent: 'orchestrator',
                 message: '重复提交已合并到任务 ' + tid + '（未新建、未重复执行）' })
      }
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

  // C3：可行动提示上的按钮 → 具体动作（切标签 / 跳设置或健康页 / 重新提交）
  const onActionableAction = useCallback(() => {
    const intent = actionIntent(actionable?.action)
    if (intent.kind === 'switch-tab') setTab(intent.tab)
    else if (intent.kind === 'navigate') window.location.assign(intent.to)
    else if (intent.kind === 'retry') submit(lastGoal || goal)
  }, [actionable, submit, lastGoal, goal])

  // 「改为修改目标」：把目标填回提交框并滚回提交区，不自动提交
  const prefillGoal = useCallback((g: string) => {
    setGoal(g)
    const main = document.querySelector('main')
    if (main) main.scrollTo({ top: 0, behavior: 'smooth' })
    else window.scrollTo({ top: 0, behavior: 'smooth' })
  }, [])

  const viewFullReport = useCallback(async (tid: string) => {    try {
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
        // F3-B：研究简报的结构化对象与导出版本绑定——结果页据此呈现
        // 关键发现/缺口/证据定位，以及"包生成于 vX、当前 vY"的如实标注
        research: d.research ?? null,
        export: d.export ?? null,
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
      // 历史报告查看不改写运行态：运行中打开历史报告不应把全局
      // status 翻成 completed（会解锁重复提交、停掉流式轮询）
      const st = useTaskStore.getState().status
      if (st !== 'running') {
        // 历史任务也载入思考日志，让"实时动态"页可回看该任务的完整过程；
        // 但运行中绝不替换全局日志（会把实时计划树与历史日志错位串台）
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
      }
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
    clearLastTask()   // "新对话"= 主动放弃当前任务：连刷新恢复点一起清
    setConvMessages([])
    useTaskStore.setState({ currentTaskId: null })
    window.history.replaceState({}, '', window.location.pathname)
  }, [reset])

  const isRunning = status === 'running'

  // N3：首次使用引导的公开状态（是否已配置模型、是否有内置演示）。
  // 只读`/api/auth/bootstrap`的布尔字段——不含密钥、地址或模型名。
  // N4 场景 8：`code_execution` 也是布尔+既有说明，用于提交任务前说明代码步骤会被拒绝。
  const [firstRun, setFirstRun] = useState<{
    configComplete: boolean
    demoAvailable: boolean
    codeExecution: { isolation_ready: boolean; isolation_required: boolean; execution_available: boolean; note: string } | null
  }>({ configComplete: true, demoAvailable: false, codeExecution: null })
  useEffect(() => {
    fetch('/api/auth/bootstrap')
      .then(r => r.json())
      .then(d => setFirstRun({
        configComplete: d?.config_complete !== false,
        demoAvailable: !!d?.demo_available,
        codeExecution: d?.code_execution || null,
      }))
      .catch(() => {})
  }, [])

  // 交付节奏：近 N 次已完成任务的平均耗时（无样本时明说未知，不给假预期）；
  // 运行中显示已运行时长，每 30s 走一次，避免看着不动以为卡死
  const [nowTick, setNowTick] = useState(() => Date.now())
  useEffect(() => {
    if (!isRunning) return
    const t = setInterval(() => setNowTick(Date.now()), 30000)
    return () => clearInterval(t)
  }, [isRunning])
  const etaText = useMemo(() => {
    const recent = (systemStatus?.recent || []) as any[]
    const mins = recent
      .filter(r => r?.created_at && r?.completed_at)
      .map(r => (new Date(r.completed_at).getTime() - new Date(r.created_at).getTime()) / 60000)
      .filter(m => Number.isFinite(m) && m > 0 && m < 24 * 60)
    if (!mins.length) return '预计时长未知（暂无已完成任务样本）'
    const avg = mins.reduce((a, b) => a + b, 0) / mins.length
    return `近 ${mins.length} 次任务平均 ${avg < 1 ? '不足 1' : Math.round(avg)} 分钟`
  }, [systemStatus])
  const elapsedText = isRunning && startedAt
    ? `已运行 ${Math.max(0, Math.round((nowTick - startedAt) / 60000))} 分钟`
    : null
  const resultItems = activeConversationId
    ? convMessages.filter(m => m.status !== 'PENDING')
    : recentTasks.filter(t => t.status !== 'PENDING')

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      {/* N3：首次使用引导——模型未配置时默认展开，配好后可随时折叠查看 */}
      <FirstRunGuide
        configComplete={firstRun.configComplete}
        demoAvailable={firstRun.demoAvailable}
        codeExecution={firstRun.codeExecution}
        onPrefillGoal={prefillGoal}
        onGoSettings={() => { window.location.href = '/settings' }} />

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
        taskId={taskId} etaText={etaText} elapsedText={elapsedText}
        onSubmit={submit} onNewConversation={newConversation} />

      {/* C3：一句话说清"现在是什么状态、你该做什么"。未知/等材料这类中性状态
          不给绿色（读成"一切正常"是最容易误导新人的地方）。 */}
      {shouldShowActionable(actionable) && (
        <div className={
          'rounded-lg border px-4 py-3 text-sm flex flex-wrap items-center gap-3 '
          + (actionableTone(actionable?.state) === 'bad'
            ? 'border-red-800 bg-red-950/40 text-red-200'
            : actionableTone(actionable?.state) === 'warn'
              ? 'border-amber-800 bg-amber-950/30 text-amber-100'
              : actionableTone(actionable?.state) === 'ok'
                ? 'border-emerald-800 bg-emerald-950/30 text-emerald-100'
                : 'border-slate-700 bg-slate-900/60 text-slate-200')
        }>
          <span className="font-medium">[{String(actionable?.label || '')}]</span>
          <span className="flex-1 min-w-[16rem]">{String(actionable?.message || '')}</span>
          {actionIntent(actionable?.action).kind !== 'none' && (
            <button
              type="button"
              onClick={onActionableAction}
              className="px-3 py-1 rounded border border-current/40 hover:bg-white/10 shrink-0">
              {String(actionable?.action_label || '处理')}
            </button>
          )}
        </div>
      )}

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
          onViewReport={viewFullReport} onSubmit={submit} onPrefillGoal={prefillGoal} />
      </div>

      <ReportViewer />

      {selectedStep && <StepInspector node={selectedStep} onClose={() => setSelectedStep(null)} />}
    </div>
  )
}
