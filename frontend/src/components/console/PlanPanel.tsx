import { memo, useEffect, useMemo, useState } from 'react'
import { Sparkles, RefreshCw, Plus, Play } from 'lucide-react'
import TaskTreeView from '../TaskTreeView'
import type { TaskNode } from '../../stores/types'

const CAPABILITIES = ['web_search', 'web_fetch', 'content_summary', 'code_execution',
  'data_loader', 'data_analyzer', 'model_trainer', 'report_generator', 'file_io', 'package']

/** 执行计划面板 + 待确认时的可编辑确认区（TaskConsole 拆分 T11c）。 */
export default memo(function PlanPanel({
  planTree, awaitingConfirm, revision, isRunning, taskId,
  markPlanConfirmed, onSelectStep,
}: {
  planTree: TaskNode | null
  awaitingConfirm: boolean
  revision: boolean
  isRunning: boolean
  taskId: string | null
  markPlanConfirmed: () => void
  onSelectStep: (node: TaskNode) => void
}) {
  const [editableSteps, setEditableSteps] = useState<any[]>([])
  const [newCap, setNewCap] = useState('content_summary')
  const [newInstr, setNewInstr] = useState('')

  // 计划待确认时，把后端计划同步到本地可编辑数组
  useEffect(() => {
    if (awaitingConfirm && planTree) {
      const children = planTree.children && planTree.children.length ? planTree.children : [planTree]
      setEditableSteps(children.map(c => ({
        step_id: c.step_id || c.id || ('s' + Math.random().toString(36).slice(2, 6)),
        capability: c.capability || 'content_summary',
        instruction: c.instruction || c.name || '',
        timeout: 120,
      })))
    }
  }, [awaitingConfirm, planTree])

  const moveStep = (id: string, dir: -1 | 1) => {
    setEditableSteps(prev => {
      const i = prev.findIndex(s => s.step_id === id)
      const j = i + dir
      if (i < 0 || j < 0 || j >= prev.length) return prev
      const next = [...prev]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })
  }

  const removeStep = (id: string) => setEditableSteps(prev => prev.filter(s => s.step_id !== id))

  const addStep = () => {
    const text = newInstr.trim()
    if (!text) return
    setEditableSteps(prev => [...prev, {
      step_id: 'e' + Math.random().toString(36).slice(2, 6),
      capability: newCap,
      instruction: text,
      timeout: 120,
    }])
    setNewInstr('')
  }

  const confirmPlan = async (action: 'confirm' | 'cancel') => {
    if (!taskId) return
    try {
      const res = await fetch('/api/plan/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(action === 'confirm'
          ? { task_id: taskId, action, steps: editableSteps }
          : { task_id: taskId, action }),
      })
      // 后端拒绝（如 Redis 不可用 503）时保持面板：任务在后端仍是
      // AWAITING_CONFIRM，前端关闭会造成"确认丢失"假象
      if (res.ok) markPlanConfirmed()
    } catch { /* ignore */ }
  }

  const editTree = useMemo<TaskNode | null>(() => {
    if (!awaitingConfirm || !planTree) return null
    return {
      id: 'root', step_id: 'root', capability: '', name: planTree.name || 'Plan',
      status: 'running', children: editableSteps.map(s => ({
        id: s.step_id, step_id: s.step_id, capability: s.capability,
        name: s.instruction || 'Step', status: 'pending', children: [],
      })),
    }
  }, [awaitingConfirm, editableSteps, planTree])

  return (
    <div className="col-span-1 lg:col-span-2 self-start bg-slate-900 border border-slate-800 rounded-xl p-5">
      <h2 className="flex items-center gap-2 text-slate-200 font-semibold text-sm mb-4">
        <span className="w-1.5 h-1.5 rounded-full bg-cyan-400" />
        {awaitingConfirm ? '计划待确认（可编辑）' : 'Execution Plan'}
      </h2>
      {awaitingConfirm && (
        <div className="mb-3 text-xs text-amber-400/90 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
          可上下移动、删除步骤或添加新步骤；确认后按你的计划执行。
        </div>
      )}
      {planTree ? (
        <TaskTreeView root={editTree ?? planTree} onSelect={onSelectStep}
          editable={awaitingConfirm} onMove={moveStep} onDelete={removeStep} />
      ) : (
        <div className="flex flex-col items-center justify-center h-64 text-slate-600">
          <Sparkles className="w-8 h-8 mb-3 opacity-20" />
          <p className="text-sm">{isRunning ? 'Generating plan...' : 'Execution plan will appear here'}</p>
          <p className="text-xs mt-1 opacity-60">{isRunning ? 'Orchestrator working' : 'Submit a task to begin'}</p>
        </div>
      )}
      {awaitingConfirm && (
        <div className="mt-4 space-y-3">
          {revision && (
            <div className="flex items-center gap-2 text-xs text-amber-400 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
              <RefreshCw className="w-3.5 h-3.5 shrink-0" />
              搜索未获得可用结果，已建议改为直接生成。确认后按修订计划执行。
            </div>
          )}
          <div className="flex gap-2">
            <select value={newCap} onChange={e => setNewCap(e.target.value)}
              className="bg-slate-800 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-slate-300 shrink-0">
              {CAPABILITIES.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
            <input value={newInstr} onChange={e => setNewInstr(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') addStep() }}
              placeholder="新步骤指令（回车添加）..."
              className="flex-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 placeholder-slate-600 focus:outline-none" />
            <button onClick={addStep}
              className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-xs shrink-0">
              <Plus className="w-3.5 h-3.5 inline" /> 添加
            </button>
          </div>
          <div className="flex gap-2">
            <button onClick={() => confirmPlan('confirm')}
              className="flex-1 flex items-center justify-center gap-2 py-2.5 bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-400 rounded-lg text-sm border border-emerald-500/30 transition-colors">
              <Play className="w-4 h-4" /> 确认并执行（{editableSteps.length} 步）
            </button>
            <button onClick={() => confirmPlan('cancel')}
              className="px-4 py-2.5 bg-red-500/10 hover:bg-red-500/20 text-red-400 rounded-lg text-sm border border-red-500/20 transition-colors">
              放弃
            </button>
          </div>
        </div>
      )}
    </div>
  )
})
