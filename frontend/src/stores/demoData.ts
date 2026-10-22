import { TaskNode, LogEntry, TaskReport, AgentInfo } from './types'

const DEMO_STEPS = [
  { id: 's1', capability: 'web_search', name: '整理两期公开披露的来源位置', agent_id: 'search_agent' },
  { id: 's2', capability: 'data_loader', name: '提取收入、利润与经营现金流', agent_id: 'data_loader' },
  { id: 's3', capability: 'data_analyzer', name: '按相同口径复算同比变化', agent_id: 'data_analyzer' },
  { id: 's4', capability: 'content_summary', name: '区分数据观察与待核查解释', agent_id: 'content_summarizer' },
  { id: 's5', capability: 'report_generator', name: '整理正文与证据缺口清单', agent_id: 'report_generator' },
]

export const DEMO_PLAN: TaskNode = {
  id: 'root', capability: '', name: '示例公司两期经营研究 · 内置合成演示',
  status: 'running',
  children: DEMO_STEPS.map(step => ({ ...step, status: 'pending', children: [] })),
}

export const DEMO_LOGS: LogEntry[] = [
  { id: 'demo-start', timestamp: '', type: 'plan', agent: 'orchestrator', message: '内置合成演示：仅展示研究流程，不请求模型或真实资料源。' },
  ...DEMO_STEPS.flatMap((step, index): LogEntry[] => [
    { id: `demo-dispatch-${index}`, timestamp: '', type: 'dispatch', step_id: step.id, agent: step.agent_id, message: `演示步骤：${step.name}` },
    { id: `demo-result-${index}`, timestamp: '', type: 'success', step_id: step.id, agent: step.agent_id, message: `演示完成：${step.name}（合成数据，非真实检索）` },
  ]),
  { id: 'demo-end', timestamp: '', type: 'success', step_id: 'root', agent: 'orchestrator', message: '演示结束：未执行真实机器验收，未进行人工复核，不生成可下载的真实证据包。' },
]

export const DEMO_REPORT: TaskReport = {
  summary: 'SUCCESS_WITH_ISSUES',
  steps: DEMO_STEPS.map(step => ({ step_id: step.id, capability: step.capability, name: step.name, status: 'SUCCESS' })),
  final_report: `# 示例公司两期经营研究

> 内置合成演示，非本次实时生成。公司与下列数字均为虚构，不对应真实证券；不作为投资依据。

## 研究范围
期间：2023 与 2024 年。合并口径；单位：百万元。资料截至日：2025-04-30（演示设定）。

| 指标 | 2023 年 | 2024 年 | 同比 |
| --- | --- | --- | --- |
| 营业收入（百万元） | 100 | 120 | 20.0% |
| 归母净利润（百万元） | 8 | 9.6 | 20.0% |
| 经营活动现金流净额（百万元） | 6 | 5 | -16.7% |

## 数据观察
演示数据中，收入与利润同向增长，但经营现金流下降。同比计算为“本期 / 上期 − 1”。

## 需要补充的证据
数字变化本身不能证明变化原因。应继续查阅应收账款、存货与现金流附注，才能核查回款节奏或资金占用的解释。

## 三种状态，分别看待
- 机器验收：未执行真实规则验收。
- 研究状态：演示存在原因解释与来源定位缺口。
- 人工复核：本版待复核；没有代替用户确认。

本示例没有真实来源或冻结证据包。真实任务完成后，请在对应研究中复核来源、底稿与导出版本。`,
  files: [],
  stats: { totalSteps: DEMO_STEPS.length, successSteps: DEMO_STEPS.length, failedSteps: 0, duration: 0 },
}

export const DEMO_AGENTS: AgentInfo[] = DEMO_STEPS.map(step => ({
  agent_id: step.agent_id, status: 'idle', capabilities: step.capability, last_heartbeat: new Date().toISOString(),
}))
