import { useState, useEffect, useCallback } from 'react'
import { Save, RotateCcw, Key, Globe, Cpu, Clock, Shield, Server } from 'lucide-react'
import { useTaskStore } from '../stores/useTaskStore'

interface Config {
  llm: { api_key: string; base_url: string; model: string }
  redis: { host: string; port: number }
  system: { task_timeout: number; max_retry: number; replan_depth: number; guardian_heartbeat: number }
}

const defaults: Config = {
  llm: { api_key: '', base_url: 'https://api.siliconflow.cn/v1', model: 'deepseek-ai/DeepSeek-V3' },
  redis: { host: 'localhost', port: 6379 },
  system: { task_timeout: 90, max_retry: 2, replan_depth: 2, guardian_heartbeat: 20 },
}

export default function SettingsPage() {
  const [cfg, setCfg] = useState<Config>(defaults)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [showKey, setShowKey] = useState(false)
  const { fetchSystemStatus } = useTaskStore()

  useEffect(() => {
    fetch('/api/config').then(r => r.json()).then(d => {
      setCfg({
        llm: { ...defaults.llm, ...(d.llm || {}) },
        redis: { ...defaults.redis, ...(d.redis || {}) },
        system: { ...defaults.system, ...(d.system || {}) },
      })
    }).catch(() => setError('Failed to load config')).finally(() => setLoading(false))
  }, [])

  const update = useCallback((section: string, key: string, value: any) => {
    setCfg(prev => {
      const s = section as keyof Config
      const next = {
        ...prev,
        [s]: { ...(prev[s] as Record<string, unknown>), [key]: value },
      } as Config
      return next
    })
    setSaved(false)
  }, [])

  const save = useCallback(async () => {
    setError('')
    try {
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
      })
      if (!res.ok) throw new Error('Save failed')
      setSaved(true)
      fetchSystemStatus()
      setTimeout(() => setSaved(false), 2000)
    } catch (e: any) {
      setError(e.message || 'Save failed')
    }
  }, [cfg])

  const reset = useCallback(() => {
    setCfg(defaults)
    setSaved(false)
    setError('')
  }, [])

  if (loading) return <div className="flex items-center justify-center h-64 text-slate-500">Loading...</div>

  const Field = ({ label, icon: Icon, children }: { label: string; icon: any; children: React.ReactNode }) => (
    <div className="space-y-1.5">
      <label className="flex items-center gap-2 text-xs text-slate-400 uppercase tracking-wider">
        <Icon className="w-3.5 h-3.5" /> {label}
      </label>
      {children}
    </div>
  )

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-slate-200 font-semibold text-lg">Settings</h1>
        <div className="flex items-center gap-3">
          <button onClick={reset}
            className="flex items-center gap-2 px-4 py-2 text-sm text-slate-400 hover:text-slate-300 bg-slate-800/50 hover:bg-slate-800 rounded-lg transition-colors border border-slate-700/50">
            <RotateCcw className="w-4 h-4" /> Reset
          </button>
          <button onClick={save}
            className="flex items-center gap-2 px-5 py-2 text-sm font-semibold bg-cyan-500 hover:bg-cyan-400 text-slate-950 rounded-lg transition-colors">
            <Save className="w-4 h-4" /> {saved ? 'Saved!' : 'Save'}
          </button>
        </div>
      </div>

      {error && <div className="bg-red-500/10 border border-red-500/30 text-red-400 text-sm rounded-lg px-4 py-3">{error}</div>}

      {/* LLM Settings */}
      <section className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-5">
        <h2 className="flex items-center gap-2 text-sm text-slate-300 font-medium">
          <Brain className="w-4 h-4 text-cyan-400" /> LLM Configuration
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Field label="API Key" icon={Key}>
            <div className="flex gap-2">
              <input type={showKey ? 'text' : 'password'}
                value={cfg.llm.api_key}
                onChange={e => update('llm', 'api_key', e.target.value)}
                className="flex-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50"
                placeholder="sk-..." />
              <button onClick={() => setShowKey(!showKey)}
                className="px-3 py-2 text-xs text-slate-500 hover:text-slate-300 bg-slate-800 border border-slate-700 rounded-lg">
                {showKey ? 'Hide' : 'Show'}
              </button>
            </div>
          </Field>
          <Field label="Base URL" icon={Globe}>
            <input type="text" value={cfg.llm.base_url}
              onChange={e => update('llm', 'base_url', e.target.value)}
              className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50" />
          </Field>
          <Field label="Model" icon={Cpu}>
            <input type="text" value={cfg.llm.model}
              onChange={e => update('llm', 'model', e.target.value)}
              className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50" />
          </Field>
        </div>
      </section>

      {/* Redis Settings */}
      <section className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-5">
        <h2 className="flex items-center gap-2 text-sm text-slate-300 font-medium">
          <Server className="w-4 h-4 text-amber-400" /> Redis Configuration
        </h2>
        <div className="grid grid-cols-2 gap-4">
          <Field label="Host" icon={Globe}>
            <input type="text" value={cfg.redis.host}
              onChange={e => update('redis', 'host', e.target.value)}
              className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50" />
          </Field>
          <Field label="Port" icon={Server}>
            <input type="number" value={cfg.redis.port}
              onChange={e => update('redis', 'port', parseInt(e.target.value) || 6379)}
              className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50" />
          </Field>
        </div>
      </section>

      {/* System Settings */}
      <section className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-5">
        <h2 className="flex items-center gap-2 text-sm text-slate-300 font-medium">
          <Clock className="w-4 h-4 text-violet-400" /> System Parameters
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Field label="Task Timeout (s)" icon={Clock}>
            <input type="number" value={cfg.system.task_timeout}
              onChange={e => update('system', 'task_timeout', parseInt(e.target.value) || 90)}
              className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50" />
          </Field>
          <Field label="Max Retry" icon={Shield}>
            <input type="number" value={cfg.system.max_retry}
              onChange={e => update('system', 'max_retry', parseInt(e.target.value) || 2)}
              className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50" />
          </Field>
          <Field label="Replan Depth" icon={Shield}>
            <input type="number" value={cfg.system.replan_depth}
              onChange={e => update('system', 'replan_depth', parseInt(e.target.value) || 2)}
              className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50" />
          </Field>
          <Field label="Guardian Heartbeat" icon={Shield}>
            <input type="number" value={cfg.system.guardian_heartbeat}
              onChange={e => update('system', 'guardian_heartbeat', parseInt(e.target.value) || 20)}
              className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50" />
          </Field>
        </div>
      </section>
    </div>
  )
}

function Brain({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z" />
      <path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z" />
      <path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4" />
      <path d="M17.599 6.5a3 3 0 0 0 .399-1.375" />
      <path d="M6.003 5.125A3 3 0 0 0 6.401 6.5" />
      <path d="M3.477 10.896a4 4 0 0 1 .585-.396" />
      <path d="M19.938 10.5a4 4 0 0 1 .585.396" />
      <path d="M6 18a4 4 0 0 1-1.967-.516" />
      <path d="M19.967 17.484A4 4 0 0 1 18 18" />
    </svg>
  )
}
