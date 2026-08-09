export interface AgentInfo {
  agent_id: string
  status: string
  capabilities: string
  last_heartbeat: string
}

export interface TaskSummary {
  task_id: string
  goal: string
  status: string
  created_at: string
  completed_at?: string
  report?: string
  conversation_id?: string
  parent_task_id?: string
}

export interface ConversationSummary {
  conversation_id: string
  title: string
  message_count: number
  last_updated: string
  last_status?: string
}

export interface ConversationMessage {
  task_id: string
  goal: string
  status: string
  created_at: string
  completed_at?: string
  report_preview?: string
  report?: string
}

export interface MemoryDoc {
  content: string
  metadata: Record<string, any>
}

export interface EvolutionRound {
  timestamp?: string
  summary?: string
  winner?: any
  stable?: boolean
  deployed?: boolean
  scoreboard?: Record<string, number>
  win_counts?: Record<string, number>
  rankings?: { task?: string; winner?: string; ranking?: string[]; scores?: Record<string, number>; reason?: string }[]
}

export interface TaskNode {
  id: string
  step_id?: string
  iteration?: number
  name: string
  instruction?: string
  capability: string
  status: 'pending' | 'running' | 'success' | 'failed' | 'skipped'
  agent_id?: string
  children: TaskNode[]
  result?: any
  replanHistory?: TaskNode[][]
  started_at?: string
  completed_at?: string
}

export interface LogEntry {
  id?: string
  timestamp: string
  type: 'plan' | 'review' | 'dispatch' | 'memory' | 'error' | 'info' | 'retry' | 'replan' | 'success'
  level?: string
  agent?: string
  message: string
  step_id?: string
}

export interface TaskReport {
  summary: string
  stats: { totalSteps: number; successSteps: number; failedSteps: number; duration: number }
  total_time?: number
  steps: { step_id: string; capability: string; name: string; status: string; result?: string }[]
  final_report: string
  files?: { name: string; size?: number; kind?: string }[]
}

export interface SystemStatus {
  tasks: { total: number; success: number; today?: number }
  agents: AgentInfo[]
  queues: Record<string, number>
  memory: { conversations: number; strategies: number }
  recent: { task_id: string; goal: string; status: string; created_at: string; report?: string }[]
  survival_rate?: number
  uptime_sec?: number
  llm_usage?: { calls: number; prompt_tokens: number; completion_tokens: number }
}

export interface WSMessage {
  type: 'plan_update' | 'log' | 'agent_status' | 'task_complete' | 'error'
  payload: any
}

export type TaskStatus = 'idle' | 'running' | 'completed'

export interface TaskState {
  currentTaskId: string | null
  activeConversationId: string | null
  awaitingConfirm: boolean
  planTree: TaskNode | null
  logs: LogEntry[]
  status: TaskStatus
  report: TaskReport | null
  agents: AgentInfo[]
  connected: boolean
  demoMode: boolean
  systemStatus: SystemStatus | null
}
