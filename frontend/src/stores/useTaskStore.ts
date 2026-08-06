import { create } from 'zustand'
import { TaskState, TaskNode, LogEntry, TaskReport, AgentInfo, SystemStatus } from './types'
import { DEMO_PLAN, DEMO_AGENTS } from './demoData'

export const useTaskStore = create<TaskState & {
  startTask: (id: string) => void
  setActiveConversation: (id: string | null) => void
  updatePlan: (tree: TaskNode) => void
  addLog: (entry: LogEntry) => void
  setReport: (report: TaskReport) => void
  updateAgents: (list: AgentInfo[]) => void
  setConnected: (v: boolean) => void
  toggleDemo: (force?: boolean) => void
  fetchSystemStatus: () => Promise<void>
  reset: () => void
}>((set, get) => ({
  currentTaskId: null,
  activeConversationId: null,
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
    planTree: null,
    logs: [],
    status: 'running',
    report: null,
  }),

  setActiveConversation: (id) => set({ activeConversationId: id }),

  updatePlan: (tree) => { console.log("[Store] updatePlan:", tree.id, tree.children?.length, "steps"); set({ planTree: tree }) },

  addLog: (entry) => { console.log("[Store] addLog:", entry.type, entry.message?.slice(0,40)); set(s => ({ logs: [...s.logs, entry] })) },

  setReport: (report) => set({ report, status: 'completed' }),

  updateAgents: (agents) => { console.log("[Store] updateAgents:", agents.length, "agents"); set({ agents }) },

  setConnected: (connected) => { console.log("[Store] connected:", connected); set({ connected }) },

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
    planTree: null,
    logs: [],
    status: 'idle',
    report: null,
    agents: [],
  }),
}))
