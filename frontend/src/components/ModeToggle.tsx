import { useState, useEffect } from 'react'
import { Cloud, Cpu, RefreshCw } from 'lucide-react'

/**
 * LLM 运行模式切换（织光 WeaveMind）：
 * - cloud  ：全商业 API（DeepSeek 主 + SiliconFlow 备份）——稳定、质量优先
 * - hybrid ：本地 QLoRA 小模型 + LoRA 参与部分 Worker（如 content_summary），
 *            云端 API 兜底——离线可用、零边际成本、规模化储备
 */
export default function ModeToggle({ compact = false }: { compact?: boolean }) {
  const [mode, setMode] = useState<string>('hybrid')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const loadMode = async () => {
    try {
      const r = await fetch('/api/llm-mode')
      const d = await r.json()
      if (d?.mode) setMode(d.mode)
    } catch { /* 后端不可达保留现状 */ }
  }

  useEffect(() => { loadMode() }, [])

  const toggle = async () => {
    if (loading) return
    setLoading(true)
    setError('')
    const next = mode === 'cloud' ? 'hybrid' : 'cloud'
    try {
      const r = await fetch('/api/llm-mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: next }),
      })
      const d = await r.json()
      if (!r.ok || d?.error) throw new Error(d?.error || '切换失败')
      setMode(d.mode)
    } catch (e: any) {
      setError(e?.message || '切换失败')
    } finally {
      setLoading(false)
    }
  }

  const isCloud = mode === 'cloud'

  if (compact) {
    return (
      <div className="flex items-center">
        <button
          onClick={toggle}
          disabled={loading}
          title={isCloud
            ? '全商业 API 模式：所有 Worker 走云端。点击切换为 本地 LoRA 混合模式'
            : '本地 LoRA 混合模式：小模型参与部分 Worker。点击切换为 全商业 API 模式'}
          className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold border transition-colors ${
            isCloud
              ? 'bg-sky-500/10 text-sky-400 border-sky-500/30 hover:bg-sky-500/20'
              : 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30 hover:bg-emerald-500/20'
          }`}>
          {loading
            ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
            : isCloud ? <Cloud className="w-3.5 h-3.5" /> : <Cpu className="w-3.5 h-3.5" />}
          <span>{isCloud ? '云端 API' : '本地 LoRA'}</span>
        </button>
        {error && <span className="ml-2 text-red-400 text-xs">{error}</span>}
      </div>
    )
  }

  return (
    <div className="flex items-center gap-3">
      <div className="text-right">
        <div className={`text-xs font-medium ${isCloud ? 'text-sky-400' : 'text-emerald-400'}`}>
          {isCloud ? '全商业 API' : '本地 LoRA 混合'}
        </div>
        <div className="text-[10px] text-slate-500 max-w-[180px] truncate" title={
          isCloud
            ? '所有 Worker 调用云端大模型（DeepSeek/SiliconFlow），稳定优先'
            : '小模型（Qwen2.5-7B + LoRA）参与 content_summary 等部分 Worker，云端兜底'
        }>
          {isCloud ? 'DeepSeek-V4-flash 主 + SiliconFlow 备份' : 'Qwen2.5-7B + LoRA · 云端兜底'}
        </div>
      </div>
      <button
        onClick={toggle}
        disabled={loading}
        className={`relative w-12 h-6 rounded-full transition-colors ${isCloud ? 'bg-sky-500/60' : 'bg-emerald-500/60'}`}
        title="点击切换 LLM 运行模式">
        <span className={`absolute top-0.5 w-5 h-5 rounded-full bg-white shadow transition-all ${isCloud ? 'left-6' : 'left-0.5'}`} />
      </button>
      {error && <span className="text-red-400 text-xs">{error}</span>}
    </div>
  )
}
