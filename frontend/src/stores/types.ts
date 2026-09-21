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
  project?: string
  report?: string
  conversation_id?: string
  parent_task_id?: string
  acceptance?: { overall?: string; gaps?: string[] }
  llm_degraded?: { switches?: number; reasons?: string[]; both_failed?: boolean }
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
  acceptance?: { overall?: string; gaps?: string[] }
  llm_degraded?: { switches?: number; reasons?: string[]; both_failed?: boolean }
}

export interface MemoryDoc {
  id?: string
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

export interface ResearchGap {
  kind?: string
  text: string
  materials?: string[]
}

/** 研究简报的结构化对象（后端 `_research_payload`）——结果页按"发现→缺口→证据"呈现。 */
export interface ResearchBrief {
  scope?: {
    company?: string
    periods?: number[]
    caliber?: string
    as_of?: string
    unit?: string
    perspective?: string
    subject_type?: string
    subject_type_source?: string
  }
  findings?: { text: string; fact_ids?: string[] }[]
  gaps?: {
    required_data?: ResearchGap[]
    evidence?: string[]
    citations?: number[]
    as_of?: string
    unproven?: { label: string; materials?: string[] }[]
  }
  evidence?: {
    located?: number
    missing_labels?: string[]
    rules_version?: string
    excluded?: { title?: string; url?: string; validation_status?: string; published_at?: string; locator?: string }[]
    rejected_citations?: { title?: string; url?: string; reason?: string }[]
  }
  citations?: { n?: number; title?: string; url?: string; type?: string; admission?: string; used_by?: string[] }[]
  locators?: { label?: string; locator?: string; source_n?: string }[]
  evidence_locations?: { kind?: string; locator?: string; source_n?: string; text?: string }[]
  charts?: { file: string; type?: string; grade?: string; draft_reason?: string; question?: string; observation?: string }[]
  analysis?: string
  /** 主张记录（C2-2）：结论/主体/期间/类型/支持状态/来源；与正文、缺口、风险、导出同源。 */
  claims?: {
    text?: string
    type?: string
    status?: string
    reason?: string
    subject?: string
    periods?: number[]
    citations?: number[]
    fact_ids?: string[]
    source?: { url?: string; title?: string; old_n?: number }
  }[]
  /** 引用了未采用来源的句子（逐句，带原因） */
  unsupported_claims?: { sentence?: string; title?: string; url?: string; old_n?: number; reason?: string }[]
  /** 面板（结构对象）绑定的版本；与当前选中版本不一致时页面必须提示 */
  structure_version?: string
  structure_current?: boolean
  /** 机器通过与人工复核**分开**：机器一栏是本版绑定的验收结论；人工一栏只有真实
   *  研究员复核后才会是 recorded，缺记录一律 pending（不允许自动填批准）。 */
  review?: {
    machine?: { overall?: string; version_id?: string; bound?: boolean }
    human?: { status?: string; approver?: string; at?: string; version_id?: string }
  }
}

/** 导出与版本绑定（后端 `_get_task_page` 的 `export` 块）。 */
export interface ExportState {
  manifest_version_id?: string
  current_version_id?: string
  /** 正文版号（正文 SHA256，与复核栏"本版"同一口径）——提示"包与当前不同版"用它，
   *  避免与谱系标识（identity_id）并排出现时看起来像两个"当前版本"。 */
  package_body_version_id?: string
  current_body_version_id?: string
  package?: string
  package_generated_at?: string
}

export interface TaskReport {
  summary: string
  taskId?: string
  stats: { totalSteps: number; successSteps: number; failedSteps: number; duration: number }
  total_time?: number
  steps: { step_id: string; capability: string; name: string; status: string; result?: string }[]
  final_report: string
  files?: { name: string; size?: number; kind?: string }[]
  // 验收缺口摘要：SUCCESS_WITH_ISSUES 任务在报告视图顶部展示
  acceptance?: { overall?: string; gaps?: string[] }
  research?: ResearchBrief | null
  export?: ExportState | null
}

export interface EndpointDiversity {
  ok: boolean
  reason: string  // ok | same_host | same_vendor | backup_not_configured
  primary_vendor?: string
  backup_vendor?: string
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
  llm_warning?: string
  llm_health?: {
    healthy?: boolean
    balance?: Record<string, { ok?: boolean; reason?: string }>
    diversity?: EndpointDiversity
  }
}

export type TaskStatus = 'idle' | 'running' | 'completed'

export interface TaskState {
  currentTaskId: string | null
  startedAt: number
  activeConversationId: string | null
  awaitingConfirm: boolean
  revision: boolean
  lastConfirmAt: number
  planTree: TaskNode | null
  logs: LogEntry[]
  status: TaskStatus
  report: TaskReport | null
  agents: AgentInfo[]
  connected: boolean
  /** 实时通道：sse 正常 / polling 已降级（降级时页面顶部提示） */
  liveTransport: 'sse' | 'polling' | 'idle'
  demoMode: boolean
  systemStatus: SystemStatus | null
}
