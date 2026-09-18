import { create } from 'zustand'
import { TaskState, TaskNode, LogEntry, TaskReport, AgentInfo, SystemStatus } from './types'
import { DEMO_PLAN, DEMO_AGENTS } from './demoData'
import { setDemoActive } from '../lib/demoGuard'
import { saveLastTask } from '../lib/lastTask'

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
  setLiveTransport: (t: 'sse' | 'polling' | 'idle') => void
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
  // 实时通道状态：'sse' 正常 / 'polling' 已降级（顶部提示，避免"数据好像不动了"没人解释）
  liveTransport: 'idle' as 'sse' | 'polling' | 'idle',
  demoMode: new URLSearchParams(window.location.search).has('demo'),
  systemStatus: null,

  startTask: (id) => {
    // 记下"最后一次提交的任务"：刷新后据此恢复跟踪（此前刷新即丢跟踪）
    saveLastTask(id)
    set({
      currentTaskId: id,
      startedAt: Date.now(),
      planTree: null,
      logs: [],
      status: 'running',
      report: null,
    })
  },

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

  setLiveTransport: (t) => set({ liveTransport: t }),


  toggleDemo: (force) => {
    const next = force ?? !get().demoMode
    set({ demoMode: next })
    // 同步 fetch 层守卫：演示模式下的写/付费操作会被拦截（见 lib/demoGuard.ts）
    setDemoActive(next)
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

  // 注意：这里**不**清恢复点。`reset()` 不只是"用户要新对话"——`useDemoRunner` 在每次
  // 非演示模式挂载时也会调它（`stop()` → `reset()`），把 `clearLastTask()` 放这里会让
  // 刷新后的恢复点被挂载流程立刻抹掉（实测：重载后 9 秒内 `wm.lastTask` 变 null）。
  // 清恢复点是"用户主动放弃这个任务"的语义，放在 `newConversation` 里。
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
