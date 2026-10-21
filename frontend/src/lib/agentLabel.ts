// 智能体显示名（P2 实机复验 2026-10-22）：
//
// 智能体页此前把 `agent_id` 直接首字母大写当名字（`replace(/_/g,' ').replace(/\b\w/g, upper)`）：
//   `search_agent` → `Search Agent`（还看得懂）
//   `dataloaderworker` → `Dataloaderworker`、`reportgeneratorworker` → `Reportgeneratorworker`
//   → 用户看到的是粘连的机器名，且同一页里两种风格并存。
// 后端 `agents.agent_id` 是**运行标识**（改名会牵动队列/白名单），所以这里只在**显示层**
// 按能力给中文名，并把原始 id 一并显示出来（可追溯，不藏事实）。

/** 能力 → 中文短名（与 `agent_id` 无关，取的是卡片上那枚 capability 徽章）。 */
export const CAPABILITY_ZH: Record<string, string> = {
  web_search: '网络检索',
  web_fetch: '网页抓取',
  content_summary: '内容摘要',
  report_generator: '报告生成',
  data_loader: '数据加载',
  data_analyzer: '数据分析',
  file_io: '文件读写',
  code_execution: '代码执行',
  package: '打包交付',
  react_agent: 'ReAct 智能体',
  model_trainer: '模型训练',
  critic: '计划评审',
  memory: '记忆管理',
}

/** 机器名后缀补空格：`dataloaderworker` → `Dataloader Worker`（兜底路径也要能读）。 */
function humanizeId(agentId: string): string {
  return String(agentId || '')
    .replace(/_+/g, ' ')
    .replace(/([a-z])(worker|agent|manager|runner)\b/gi, '$1 $2')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, c => c.toUpperCase())
}

export interface AgentLike {
  agent_id?: string
  capabilities?: string
  capability?: string
}

/** 卡片主标题：优先第一个已知能力的中文名，否则把 agent_id 读成可读标题。 */
export function agentDisplayName(agent: AgentLike | null | undefined): string {
  const caps = String(agent?.capabilities || agent?.capability || '')
    .split(',').map(c => c.trim()).filter(Boolean)
  for (const c of caps) {
    const zh = CAPABILITY_ZH[c]
    if (zh) return zh
  }
  const raw = String(agent?.agent_id || '').trim()
  return raw ? humanizeId(raw) : '未命名智能体'
}

/** 卡片的**原始标识**（小字显示）：显示名是给人读的，标识是给排查用的，两者都要在。 */
export function agentRawId(agent: AgentLike | null | undefined): string {
  return String(agent?.agent_id || '').trim()
}
