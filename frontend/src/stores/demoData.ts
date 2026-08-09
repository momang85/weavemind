import { TaskNode, LogEntry, TaskReport, AgentInfo } from './types'

export const DEMO_PLAN: TaskNode = {
  id: 'root',
  capability: '',
  name: 'Build AI industry analysis dashboard',
  status: 'running',
  children: [
    { id: 's1', capability: 'web_search', name: '调研AI看板最佳实践与竞品', status: 'pending', children: [], agent_id: 'search_agent' },
    { id: 's2', capability: 'content_summary', name: '总结关键功能与数据需求', status: 'pending', children: [], agent_id: 'content_summarizer' },
    { id: 's3', capability: 'web_search', name: '搜索可用AI数据API', status: 'pending', children: [], agent_id: 'search_agent', replanHistory: [[{ id: 's3-r1', name: 'Retry API search with different keywords', capability: 'web_search', status: 'failed', children: [] }, { id: 's3-r2', name: 'Use backup data source', capability: 'content_summary', status: 'success', children: [] }]] },
    { id: 's4', capability: 'code_execution', name: '验证API可靠性与数据质量', status: 'pending', children: [], agent_id: 'code_executor' },
    { id: 's5', capability: 'file_io', name: '创建项目结构(React+Vite+Tailwind)', status: 'pending', children: [], agent_id: 'file_io_worker' },
    { id: 's6', capability: 'code_execution', name: '构建后端API与自动更新机制', status: 'pending', children: [], agent_id: 'code_executor' },
    { id: 's7', capability: 'code_execution', name: '构建前端看板(Chart.js)', status: 'pending', children: [], agent_id: 'code_executor' },
    { id: 's8', capability: 'code_execution', name: '添加响应式布局与交互动画', status: 'pending', children: [], agent_id: 'code_executor' },
    { id: 's9', capability: 'code_execution', name: '性能优化与代码压缩', status: 'pending', children: [], agent_id: 'code_executor' },
    { id: 's10', capability: 'file_io', name: '生成README部署文档', status: 'pending', children: [], agent_id: 'file_io_worker' },
  ]
}

export const DEMO_LOGS: LogEntry[] = [
  { id: 'l1',  timestamp: '00:00:01', type: 'plan',     agent: 'orchestrator',  message: '计划已生成: 10步执行链' },
  { id: 'l2',  timestamp: '00:00:02', type: 'memory',    agent: 'orchestrator',  message: '注入3条相关历史经验 (672字符)' },
  { id: 'l3',  timestamp: '00:00:05', type: 'review',    agent: 'critic',        message: '计划评审通过 (8/7/9/8/6) — 创新性偏低，已自动优化' },
  { id: 'l4',  timestamp: '00:00:08', type: 'dispatch',  agent: 'search_agent',  message: '派发 web_search 任务: 调研AI看板最佳实践' },
  { id: 'l5',  timestamp: '00:00:15', type: 'info',      agent: 'search_agent',  message: '搜索完成: 返回3条相关结果' },
  { id: 'l6',  timestamp: '00:00:16', type: 'dispatch',  agent: 'content_summarizer', message: '派发 content_summary 任务: 总结关键功能' },
  { id: 'l7',  timestamp: '00:00:35', type: 'info',      agent: 'content_summarizer', message: '总结完成: 已提取6项核心功能需求' },
  { id: 'l8',  timestamp: '00:00:36', type: 'dispatch',  agent: 'search_agent',  message: '派发 web_search 任务: 搜索可用AI数据API' },
  { id: 'l9',  timestamp: '00:00:42', type: 'error',     agent: 'search_agent',  message: '搜索失败: API端点超时 (30s无响应)' },
  { id: 'l10', timestamp: '00:00:43', type: 'plan',      agent: 'orchestrator',  message: '触发Replan: 将步骤拆解为2个子任务' },
  { id: 'l11', timestamp: '00:00:45', type: 'dispatch',  agent: 'search_agent',  message: 'Replan子任务: 更换关键词重新搜索' },
  { id: 'l12', timestamp: '00:00:55', type: 'info',      agent: 'search_agent',  message: '二次搜索成功: 找到5个可用API' },
  { id: 'l13', timestamp: '00:01:05', type: 'dispatch',  agent: 'code_executor',  message: '派发 code_execution 任务: 验证API可靠性' },
  { id: 'l14', timestamp: '00:01:25', type: 'info',      agent: 'code_executor',  message: '验证完成: 3/5 API稳定可用' },
  { id: 'l15', timestamp: '00:01:26', type: 'dispatch',  agent: 'file_io_worker', message: '派发 file_io 任务: 创建项目文件结构' },
  { id: 'l16', timestamp: '00:01:36', type: 'info',      agent: 'file_io_worker', message: '项目结构已创建: /tmp/agent_workspace' },
  { id: 'l17', timestamp: '00:01:37', type: 'dispatch',  agent: 'code_executor',  message: '派发 code_execution 任务: 构建后端API' },
  { id: 'l18', timestamp: '00:02:10', type: 'info',      agent: 'code_executor',  message: '后端构建完成: Express + SQLite' },
  { id: 'l19', timestamp: '00:02:15', type: 'dispatch',  agent: 'code_executor',  message: '派发 code_execution 任务: 构建前端看板' },
  { id: 'l20', timestamp: '00:02:55', type: 'info',      agent: 'code_executor',  message: '前端完成: Chart.js图表已集成' },
  { id: 'l21', timestamp: '00:03:25', type: 'dispatch',  agent: 'code_executor',  message: '派发 code_execution 任务: 性能优化' },
  { id: 'l22', timestamp: '00:03:45', type: 'info',      agent: 'code_executor',  message: '优化完成: 包体积减少32%' },
  { id: 'l23', timestamp: '00:03:46', type: 'dispatch',  agent: 'file_io_worker', message: '派发 file_io 任务: 生成README文档' },
  { id: 'l24', timestamp: '00:03:55', type: 'info',      agent: 'orchestrator',  message: '全部完成! 10/10步骤通过 (总耗时225s)' },
]

export const DEMO_REPORT: TaskReport = {
  summary: 'AI行业分析看板 - 已完整构建',
  steps: [
    { step_id: 's1', capability: 'web_search', name: '调研AI看板最佳实践与竞品', status: 'SUCCESS', result: '3 relevant results' },
    { step_id: 's2', capability: 'content_summary', name: 'Summarize key features', status: 'SUCCESS' },
    { step_id: 's3', capability: 'web_search', name: 'Search data APIs (replanned)', status: 'SUCCESS' },
    { step_id: 's4', capability: 'code_execution', name: '验证API可靠性与数据质量', status: 'SUCCESS' },
    { step_id: 's5', capability: 'file_io', name: 'Create project structure', status: 'SUCCESS' },
    { step_id: 's6', capability: 'code_execution', name: 'Build backend API', status: 'SUCCESS' },
    { step_id: 's7', capability: 'code_execution', name: 'Build frontend dashboards', status: 'SUCCESS' },
    { step_id: 's8', capability: 'code_execution', name: 'Responsive layout', status: 'SUCCESS' },
    { step_id: 's9', capability: 'code_execution', name: '性能优化与代码压缩', status: 'SUCCESS' },
    { step_id: 's10', capability: 'file_io', name: 'Generate README', status: 'SUCCESS' },
  ],
  files: [
    { name: 'frontend/index.html', size: 2048, kind: 'html' },
    { name: 'frontend/src/App.tsx', size: 5120, kind: 'tsx' },
    { name: 'backend/server.js', size: 4096, kind: 'js' },
    { name: 'README.md', size: 1024, kind: 'md' },
  ],
  final_report: `# AI Industry Analysis Dashboard

## Overview
A real-time web dashboard displaying AI industry trends, market size, and key players with auto-refreshing data.

## Tech Stack
- Frontend: React 18 + TypeScript + Tailwind CSS + Chart.js
- Backend: Node.js + Express
- Database: SQLite for caching

## Features
- Live AI news feed with source aggregation
- Market size charts (bar, pie, line)
- Top 10 AI players with company profiles
- Dark/Light theme toggle
- Mobile responsive

## Deliverables
- /frontend - React app
- /backend - Express API
- README.md - Deployment guide`,
  stats: { totalSteps: 10, successSteps: 9, failedSteps: 1, duration: 225 },
}

export const DEMO_AGENTS: AgentInfo[] = [
  { agent_id: 'search_agent', status: 'idle', capabilities: 'web_search,content_scrape', last_heartbeat: new Date().toISOString() },
  { agent_id: 'content_summarizer', status: 'idle:0/5', capabilities: 'content_summary', last_heartbeat: new Date().toISOString() },
  { agent_id: 'code_executor', status: 'active:2/5', capabilities: 'code_execution', last_heartbeat: new Date().toISOString() },
  { agent_id: 'file_io_worker', status: 'idle:0/5', capabilities: 'file_io', last_heartbeat: new Date().toISOString() },
  { agent_id: 'packaging_worker', status: 'idle:0/3', capabilities: 'package', last_heartbeat: new Date().toISOString() },
]
