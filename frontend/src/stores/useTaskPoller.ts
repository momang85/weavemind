import { useEffect, useRef } from 'react'
import { useTaskStore } from './useTaskStore'

/**
 * Task poller — polls /task/{id} every 2s.
 * Deduplicates logs (seenLogs Set) and plan updates (hash compare).
 */
export function useTaskPoller(taskId: string | null) {
  const {
    demoMode,
    updatePlan, addLog, setReport,
    fetchSystemStatus,
  } = useTaskStore()
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const seenLogs = useRef<Set<string>>(new Set())
  const lastHash = useRef<string>('')

  useEffect(() => {
    if (!taskId || demoMode) return

    console.log('[TaskPoller] Starting for', taskId)
    seenLogs.current = new Set()
    lastHash.current = ''
    let first = true

    timerRef.current = setInterval(async () => {
      try {
        const res = await fetch('/task/' + taskId)
        const d = await res.json()
        if (d.error) return

        // First poll: show "Task accepted" immediately
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
              status: d.status === 'SUCCESS' ? 'success' : d.status === 'FAILED' ? 'failed' : 'running',
              children: buildChildren(),
            })
          }
        } else if (d.status === 'PENDING') {
          // Show planning placeholder on first poll
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

        // Deduplicated logs
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

        // On complete
        if (d.status === 'SUCCESS' || d.status === 'FAILED') {
          setReport({
            summary: d.status,
            stats: { totalSteps: rawSteps.length,
              successSteps: rawSteps.filter((s: any) => (s.result?.status||'').toLowerCase() === 'success').length,
              failedSteps: rawSteps.filter((s: any) => (s.result?.status||'').toLowerCase() === 'failed').length,
              duration: 0 },
            steps: rawSteps.map((s: any) => ({ id: s.step_id||'', step_id: s.step_id||'', capability: s.capability||'', name: s.instruction||'Step', status: (s.result?.status||'pending').toLowerCase(), children: [] })),
            final_report: d.report || d.final_report || '',
          })
          fetchSystemStatus()
          if (timerRef.current) clearInterval(timerRef.current)
        }
      } catch (err) { console.log('[Poller]', err) }
    }, 2000)

    return () => { if (timerRef.current) clearInterval(timerRef.current) }
  }, [taskId, demoMode])
}
