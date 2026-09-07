import { useEffect, useRef } from 'react'
import { useTaskStore } from './useTaskStore'

/**
 * T11 双通道收敛：任务实时更新统一入口。
 * SSE（/api/task/{id}/events）为主——后端每收到一条 orchestrator 进度消息
 * 即下发合并后的完整快照；SSE 失败自动降级为 2s 轮询（/task/{id}）。
 * 两条通道共用同一套 applySnapshot 去重/合并逻辑，消除"各自去重"的双活状态。
 */
export function useTaskLive(taskId: string | null) {
  const {
    demoMode,
    updatePlan, addLog, setReport,
    fetchSystemStatus, setAwaitingConfirm, setRevision,
  } = useTaskStore()
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const esRef = useRef<EventSource | null>(null)
  const seenLogs = useRef<Set<string>>(new Set())
  const lastHash = useRef<string>('')
  const finished = useRef(false)

  useEffect(() => {
    if (!taskId || demoMode) return

    console.log('[TaskLive] Starting for', taskId)
    seenLogs.current = new Set()
    lastHash.current = ''
    finished.current = false
    let first = true
    let stopped = false

    const stopAll = () => {
      stopped = true
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
      if (esRef.current) { esRef.current.close(); esRef.current = null }
    }

    const applySnapshot = async (d: any) => {
      if (stopped || finished.current || !d || d.error) return

      // 防抖：用户刚点过"确认"后 8s 内忽略 AWAITING_CONFIRM，
      // 避免后端状态回写延迟导致确认模块反复弹出。
      // 经 getState() 实时读取：effect 闭包会固化解构时的旧值
      const lastConfirmAt = useTaskStore.getState().lastConfirmAt
      const justConfirmed = lastConfirmAt > 0 && Date.now() - lastConfirmAt < 8000
      setAwaitingConfirm(d.status === 'AWAITING_CONFIRM' && !justConfirmed)
      setRevision(d.status === 'AWAITING_CONFIRM' && !!d.revision)

      // 流式思考日志：合并服务端进度（按 id 去重）
      const serverLogs: any[] = d.logs || []
      serverLogs.forEach((lg: any) => {
        if (!lg || lg.id === undefined) return
        const lid = 'srv-' + lg.id
        if (seenLogs.current.has(lid)) return
        seenLogs.current.add(lid)
        addLog({
          id: lid,
          timestamp: lg.timestamp || new Date().toLocaleTimeString(),
          type: lg.type || 'info',
          agent: lg.agent || 'orchestrator',
          message: lg.message || '',
        })
      })

      // First snapshot: show "Task accepted" immediately
      if (first) { first = false; addLog({ id: 'accepted', timestamp: new Date().toLocaleTimeString(), type: 'plan', agent: 'orchestrator', message: 'Accepted: ' + (d.goal||'').slice(0,50) }) }

      const rawSteps = d.steps || []
      const hash = rawSteps.map((s: any) => (s.step_id||'') + (s.result?.status||'')).join('|')

      // Always update plan tree — show "Generating plan..." while waiting
      const buildChildren = () => rawSteps.map((s: any) => ({
        id: s.step_id || '',
        step_id: s.step_id || '',
        iteration: s.iteration || 0,
        capability: s.capability || '',
        name: s.instruction || s.name || 'Step',
        status: (s.result?.status || 'pending').toLowerCase(),
        children: [],
        agent_id: s.result?.agent_id || '',
        result: s.result || null,
      }))

      if (rawSteps.length > 0) {
        if (hash !== lastHash.current) {
          lastHash.current = hash
          updatePlan({
            id: 'root', capability: '',
            name: d.goal || 'Task',
            status: d.status === 'SUCCESS' ? 'success'
              : d.status === 'SUCCESS_WITH_ISSUES' ? 'success'
              : d.status === 'FAILED' ? 'failed' : 'running',
            children: buildChildren(),
          })
        }
      } else if (d.status === 'PENDING' || d.status === 'RUNNING') {
        // Show planning placeholder on first snapshot
        if (lastHash.current !== 'planning') {
          lastHash.current = 'planning'
          updatePlan({
            id: 'root', capability: '',
            name: d.goal || 'Task',
            status: 'running',
            children: [{
              id: 'planning', step_id: 'planning',
              capability: '',
              name: 'Planning (LLM thinking)...',
              status: 'running',
              children: [],
            }],
          })
        }
      }

      // Deduplicated step-status logs
      rawSteps.forEach((s: any) => {
        const st = ((s.result?.status || '')).toLowerCase()
        if (!st || st === 'pending') return
        const logId = (s.step_id || '') + '-' + st
        if (seenLogs.current.has(logId)) return
        seenLogs.current.add(logId)
        const msg = (s.instruction || '').slice(0, 60)
        addLog({
          id: logId,
          timestamp: new Date().toLocaleTimeString(),
          type: st === 'success' ? 'dispatch' : 'error',
          agent: s.capability || 'worker',
          message: (st === 'success' ? 'Completed: ' : 'Failed: ') + msg,
        })
      })

      // On complete（含 SUCCESS_WITH_ISSUES：带缺口交付也是终态，
      // 此前漏判导致前端一直显示 Running、需手动刷新才恢复）
      if (d.status === 'SUCCESS' || d.status === 'FAILED' || d.status === 'SUCCESS_WITH_ISSUES') {
        finished.current = true
        stopAll()
        setAwaitingConfirm(false)
        const reportObj: any = {
          summary: d.status,
          taskId,
          stats: { totalSteps: rawSteps.length,
            successSteps: rawSteps.filter((s: any) => (s.result?.status||'').toLowerCase() === 'success').length,
            failedSteps: rawSteps.filter((s: any) => (s.result?.status||'').toLowerCase() === 'failed').length,
            duration: 0 },
          steps: rawSteps.map((s: any) => ({ id: s.step_id||'', step_id: s.step_id||'', capability: s.capability||'', name: s.instruction||'Step', status: (s.result?.status||'pending').toLowerCase(), children: [] })),
          final_report: d.report || d.final_report || '',
          // 验收缺口：SUCCESS_WITH_ISSUES 任务的报告顶部展示
          acceptance: (d.acceptance && Array.isArray(d.acceptance.gaps))
            ? { overall: d.acceptance.overall || d.status, gaps: d.acceptance.gaps }
            : undefined,
        }
        try {
          const dl = await (await fetch('/api/task/' + taskId + '/deliverables')).json()
          reportObj.files = (dl.files ?? []).map((f: any) => ({ name: f.name, size: f.size, kind: f.kind }))
        } catch { /* ignore */ }
        // 窗口期用户可能已切到新任务：迟到的报告不覆盖新任务状态
        const cur = useTaskStore.getState().currentTaskId
        if (cur === taskId) setReport(reportObj)
        fetchSystemStatus()
      }
    }

    const startPolling = () => {
      if (timerRef.current || stopped || finished.current) return
      console.log('[TaskLive] SSE unavailable, falling back to polling')
      timerRef.current = setInterval(async () => {
        try {
          const res = await fetch('/task/' + taskId)
          const d = await res.json()
          await applySnapshot(d)
        } catch (err) { console.log('[TaskLive/poll]', err) }
      }, 2000)
    }

    const startSSE = () => {
      try {
        const es = new EventSource('/api/task/' + taskId + '/events')
        esRef.current = es
        es.addEventListener('snapshot', (ev) => {
          try {
            const msg = JSON.parse((ev as MessageEvent).data)
            applySnapshot(msg.data)
          } catch { /* ignore */ }
        })
        es.onerror = () => {
          es.close()
          if (esRef.current === es) esRef.current = null
          // 终态后服务端主动断连（EventSource 会触发 onerror）：
          // 此时不再回退轮询，避免完成后的空转
          if (!finished.current) startPolling()
        }
      } catch {
        startPolling()
      }
    }

    startSSE()
    // SSE 首帧前先拉一次快照，避免初始化空窗
    fetch('/task/' + taskId).then(r => r.json()).then(applySnapshot).catch(() => {})

    return () => { stopAll() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, demoMode])
}
