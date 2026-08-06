import { useEffect, useRef, useCallback } from 'react'
import { useTaskStore } from './useTaskStore'
import { DEMO_PLAN, DEMO_LOGS, DEMO_REPORT, DEMO_AGENTS } from './demoData'
import { TaskNode } from './types'

export function useDemoRunner() {
  const {
    demoMode,
    updatePlan, addLog, setReport, updateAgents, startTask, reset,
  } = useTaskStore()
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const run = useCallback(() => {
    if (!demoMode) return
    reset()
    startTask('demo-task-001')
    updateAgents(DEMO_AGENTS)
    updatePlan(structuredClone(DEMO_PLAN))

    let step = 0
    const totalSteps = DEMO_LOGS.length
    const plan = structuredClone(DEMO_PLAN)

    // Animate plan tree steps
    const updateNodeStatus = (node: TaskNode, id: string, status: TaskNode['status']) => {
      if (node.id === id) { node.status = status; return true }
      for (const child of node.children) {
        if (updateNodeStatus(child, id, status)) return true
      }
      return false
    }

    timerRef.current = setInterval(() => {
      if (step >= totalSteps) {
        if (timerRef.current) clearInterval(timerRef.current)
        setReport(DEMO_REPORT)
        return
      }

      const entry = DEMO_LOGS[step]
      addLog({ ...entry, timestamp: new Date().toLocaleTimeString() })

      // Update plan tree node status based on log
      if (entry.step_id === 'root' && entry.type === 'success') {
        plan.status = 'success'
      } else if (entry.step_id && entry.type === 'success') {
        updateNodeStatus(plan, entry.step_id, 'success')
      } else if (entry.step_id && entry.type === 'error') {
        updateNodeStatus(plan, entry.step_id, 'failed')
      } else if (entry.step_id && entry.type === 'info' && entry.message.includes('Dispatching')) {
        updateNodeStatus(plan, entry.step_id, 'running')
      }
      updatePlan(structuredClone(plan))

      // Animate agent status at step 20 (mid-execution)
      if (step === 20) {
        updateAgents(DEMO_AGENTS.map(a =>
          a.agent_id === 'code_executor' ? { ...a, status: 'active:3/5' } : a
        ))
      }

      step++
    }, 600) // 600ms per step = ~30s total demo
  }, [demoMode])

  const stop = useCallback(() => {
    if (timerRef.current) clearInterval(timerRef.current)
    reset()
  }, [])

  useEffect(() => {
    if (demoMode) run()
    else stop()
    return () => { if (timerRef.current) clearInterval(timerRef.current) }
  }, [demoMode, run, stop])

  return { run, stop }
}
