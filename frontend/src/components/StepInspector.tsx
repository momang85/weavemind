import { useState } from 'react'
import type { TaskNode } from '../stores/types'
import { X, ChevronDown, ChevronUp } from 'lucide-react'
import { stepStatusMeta } from '../lib/statusMeta'

const roleColors: Record<string, string> = {
  planner: '#38bdf8', critic: '#c084fc', worker: '#6ee7b7',
  memory: '#fbbf24', replan: '#fb923c', error: '#fca5a5',
}

/** 结果字段的中文名：字段名直接给用户看等于没翻译（此前整块 Result 都是英文 JSON）。 */
const RESULT_LABELS: Record<string, string> = {
  status: '状态', summary: '摘要', output: '输出', text: '文本', content: '内容',
  answer: '回答', title: '标题', items: '条目', count: '数量', rows: '行数',
  data: '数据', error: '错误', reason: '原因', reason_detail: '原因明细',
  files: '文件', charts: '图表', metrics: '指标', values: '数值', sources: '来源',
  url: '链接', urls: '链接', duration_ms: '耗时（毫秒）', tokens: 'token 数',
  cost_usd: '成本（美元）', replanned: '已重规划', ok: '是否成功', stage: '阶段',
  notes: '备注', warnings: '告警', capabilities: '能力', agent_id: '执行体',
}

/** 本机路径脱敏：交付与排查都不需要暴露发件机器的目录结构。保留末两段（仍可定位文件）。 */
export function redactPaths(text: string): string {
  return text.replace(
    /(?:[A-Za-z]:[\\/][^\s"'()\]]+|\/(?:Users|home|tmp|var|private|mnt)\/[^\s"'()\]]+)/g,
    (m) => {
      const parts = m.replace(/\\/g, '/').split('/').filter(Boolean)
      return '…/' + parts.slice(-2).join('/')
    },
  )
}

function statusColor(status: string) {
  // 颜色统一来自 lib/statusMeta（此前本文件自建一套 success/failed/running/skipped → 色值）
  return stepStatusMeta(status).text
}

function formatResult(result: any): string {
  if (result === null || result === undefined) return ''
  if (typeof result === 'string') return result
  try {
    return JSON.stringify(result, null, 2)
  } catch {
    return String(result)
  }
}

function summarizeValue(value: any): string | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return `${value.length} 项`
  if (typeof value === 'object') {
    const keys = Object.keys(value)
    return keys.length ? `${keys.length} 个字段` : '空对象'
  }
  return String(value)
}

/** 长文本折叠：超过阈值默认收起，点击展开/收起。 */
function CollapsibleText({ text, limit = 200, className = '' }: { text: string; limit?: number; className?: string }) {
  const [open, setOpen] = useState(false)
  const long = text.length > limit
  return (
    <div>
      <div className={`whitespace-pre-wrap break-all ${className}`}>
        {long && !open ? text.slice(0, limit) + '…' : text}
      </div>
      {long && (
        <button onClick={() => setOpen(!open)}
          className="mt-1 flex items-center gap-1 text-xs text-cyan-400 hover:text-cyan-300">
          {open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
          {open ? '收起' : `展开（${text.length} 字符）`}
        </button>
      )}
    </div>
  )
}

export default function StepInspector({ node, onClose }: { node: TaskNode | null; onClose: () => void }) {
  // 注意：hook 必须在 `if (!node)` 之前（早退后调 hook 会触发 React #310 白屏）
  const [rawOpen, setRawOpen] = useState(false)
  if (!node) return null
  const meta = (node as any).metadata || {}
  const logs = (node as any).logs || []
  const resultText = redactPaths(formatResult(node.result))
  const resultObj: Record<string, any> | null =
    node.result && typeof node.result === 'object' && !Array.isArray(node.result)
      ? node.result as Record<string, any>
      : null
  // 白名单：已知字段一律展示；未知字段仅在是标量时展示（对象/数组只进原始 JSON）
  const rows: Array<{ label: string; value: any }> = []
  if (resultObj) {
    for (const [k, v] of Object.entries(resultObj)) {
      const known = RESULT_LABELS[k]
      const scalar = v == null || ['string', 'number', 'boolean'].includes(typeof v)
      if (known || scalar) rows.push({ label: known || k, value: v })
    }
  }

  return (
    <div className="fixed right-0 top-0 z-50 h-full w-full overflow-y-auto border-l border-gray-700 bg-gray-900 p-4 shadow-2xl sm:w-96">
      <div className="flex justify-between items-center mb-4">
        <h3 className="text-lg font-bold text-white">步骤详情</h3>
        <button onClick={onClose} className="text-gray-400 hover:text-white" aria-label="关闭">
          <X className="w-5 h-5" />
        </button>
      </div>

      {meta.agent_hatched && (
        <div className="mb-3 px-2 py-1 bg-amber-500/20 text-amber-400 text-xs rounded-full inline-block">新物种</div>
      )}

      {/* 基本信息 */}
      <div className="mb-4 space-y-3 text-sm">
        {node.step_id && (
          <div>
            <div className="text-gray-400">步骤 ID</div>
            <div className="text-white font-mono text-xs break-all">{node.step_id}</div>
          </div>
        )}
        <div>
          <div className="text-gray-400">能力</div>
          <div className="text-white font-mono text-sm">{node.capability || 'goal'}</div>
        </div>
        {node.agent_id && (
          <div>
            <div className="text-gray-400">执行体</div>
            <div className="text-white font-mono text-sm">{node.agent_id}</div>
          </div>
        )}
        <div>
          <div className="text-gray-400">状态</div>
          <div className={`font-mono text-sm font-semibold ${statusColor(node.status)}`}>{stepStatusMeta(node.status).label}</div>
        </div>
        {(node.instruction || (node.name && node.id !== 'root')) && (
          <div>
            <div className="text-gray-400">指令</div>
            <CollapsibleText text={redactPaths(node.instruction || node.name || '')}
              limit={200} className="text-gray-200 text-xs leading-relaxed" />
          </div>
        )}
      </div>

      {/* 执行结果：白名单字段 + 中文名；本机路径已脱敏 */}
      {resultText && (
        <div className="mb-4">
          <div className="text-gray-400 text-sm mb-1">执行结果</div>
          {node.result && node.result.replanned && (
            <div className="mb-2 px-2 py-1 bg-amber-500/10 text-amber-400 text-xs rounded">
              已改用替代步骤重规划
            </div>
          )}
          {rows.length > 0 ? (
            <div className="space-y-2 rounded-lg bg-gray-800 p-3">
              {rows.map(({ label, value }) => {
                const summary = summarizeValue(value)
                if (summary === null) return null
                const isText = typeof value === 'string' && value.length > 80
                return (
                  <div key={label}>
                    <div className="text-gray-400 text-xs">{label}</div>
                    {isText ? (
                      <CollapsibleText text={redactPaths(summary)} limit={200}
                        className="text-emerald-300 text-xs leading-relaxed" />
                    ) : (
                      <div className="text-emerald-300 text-xs break-all">{redactPaths(summary)}</div>
                    )}
                  </div>
                )
              })}
            </div>
          ) : (
            <pre className="text-xs text-emerald-300 bg-gray-800 rounded-lg p-3 max-h-72 overflow-y-auto whitespace-pre-wrap break-all">
              {resultText.slice(0, 500)}
            </pre>
          )}
          <button onClick={() => setRawOpen(!rawOpen)}
            className="mt-2 flex items-center gap-1 text-xs text-cyan-400 hover:text-cyan-300">
            {rawOpen ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
            {rawOpen ? '收起原始 JSON' : '查看原始 JSON'}
          </button>
          {rawOpen && (
            <>
              <pre className="mt-2 text-xs text-slate-300 bg-gray-950 rounded-lg p-3 max-h-72 overflow-y-auto whitespace-pre-wrap break-all">
                {resultText.slice(0, 4000)}
              </pre>
              {resultText.length > 4000 && (
                <div className="text-xs text-slate-500 mt-1">结果过长，已截断前 4000 字符（完整结果见日志）</div>
              )}
            </>
          )}
        </div>
      )}

      {/* 决策轨迹 */}
      {(meta.planner_reasoning || meta.memory_injected || meta.critic_score || node.replanHistory) && (
        <div className="mb-4">
          <div className="text-gray-400 text-sm mb-2">决策轨迹</div>
          <div className="space-y-3">
            {meta.planner_reasoning && <Bubble role="planner" text={redactPaths(meta.planner_reasoning)} />}
            {meta.memory_injected && <Bubble role="memory" text={'记忆注入：' + redactPaths(meta.memory_injected)} />}
            {meta.critic_score && <Bubble role="critic" text={'评审评分：' + meta.critic_score} />}
            {node.replanHistory && node.replanHistory.length > 0 && (
              <div className="text-xs text-gray-500">
                重规划次数：{node.replanHistory.length}
              </div>
            )}
            {logs && logs.map((l: any, i: number) => <Bubble key={i} role={l.type || 'worker'} text={redactPaths(l.message || l)} />)}
          </div>
        </div>
      )}
    </div>
  )
}

function Bubble({ role, text }: { role: string; text: string }) {
  const color = roleColors[role] || '#94a3b8'
  return (
    <div className="flex items-start gap-2">
      <div className="w-2 h-2 mt-1.5 rounded-full flex-shrink-0" style={{ background: color }} />
      <div className="text-sm text-gray-300 bg-gray-800 rounded-lg px-3 py-2 flex-1 break-all">{String(text).slice(0, 300)}</div>
    </div>
  )
}
