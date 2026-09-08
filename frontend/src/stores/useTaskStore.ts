import { create } from 'zustand'
import { TaskState, TaskNode, LogEntry, TaskReport, AgentInfo, SystemStatus } from './types'
import { DEMO_PLAN, DEMO_AGENTS } from './demoData'

export const useTaskStore = create<TaskState & {
  startTask: (id: string) => void
  setActiveConversation: (id: string | null) => void
  setAwaitingConfirm: (v: boolean) => void
  setRevision: (v: boolean) => void
  markPlanConfirmed: () => void
  updatePlan: (tree: TaskNode) => void
  addLog: (entry: LogEntry) => void
  setLogs: (entries: LogEntry[]) => void
  setReport: (report: TaskReport) => void
  updateAgents: (list: AgentInfo[]) => void
  toggleDemo: (force?: boolean) => void
  fetchSystemStatus: () => Promise<void>
  reset: () => void
}>((set, get) => ({
  currentTaskId: null,
  startedAt: 0,
  activeConversationId: null,
  awaitingConfirm: false,
  revision: false,
  lastConfirmAt: 0,
  planTree: null,
  logs: [],
  status: 'idle',
  report: null,
  agents: [],
  connected: false,
  demoMode: new URLSearchParams(window.location.search).has('demo'),
  systemStatus: null,

  startTask: (id) => set({
    currentTaskId: id,
    startedAt: Date.now(),
    planTree: null,
    logs: [],
    status: 'running',
    report: null,
  }),

  setActiveConversation: (id) => set({ activeConversationId: id }),

  setAwaitingConfirm: (v) => set({ awaitingConfirm: v }),

  setRevision: (v) => set({ revision: v }),

  markPlanConfirmed: () => set({ awaitingConfirm: false, lastConfirmAt: Date.now() }),

  updatePlan: (tree) => set({ planTree: tree }),

  addLog: (entry) => set(s => {
    // 上限防泄漏：长任务日志无界增长会拖垮内存与 LiveActivity 渲染
    const logs = s.logs.length >= 800 ? s.logs.slice(-700) : s.logs
    return { logs: [...logs, entry] }
  }),

  setLogs: (entries) => set({ logs: entries }),

  setReport: (report) => set({ report, status: 'completed' }),

  updateAgents: (agents) => set({ agents }),


  toggleDemo: (force) => {
    const next = force ?? !get().demoMode
    set({ demoMode: next })
    if (next) {
      set({
        currentTaskId: 'demo-task-001',
        planTree: structuredClone(DEMO_PLAN),
        agents: DEMO_AGENTS,
        connected: true,
        status: 'idle',
        report: null,
        logs: [],
      })
    }
  },

  fetchSystemStatus: async () => {
    if (get().demoMode) return
    try {
      const res = await fetch('/api/status')
      const d = await res.json() as SystemStatus
      set({ systemStatus: d, agents: d.agents || [], connected: true })
    } catch {
      set({ connected: false })
    }
  },

  reset: () => set({
    currentTaskId: null,
    activeConversationId: null,
    awaitingConfirm: false,
    planTree: null,
    logs: [],
    status: 'idle',
    report: null,
    agents: [],
  }),
}))
