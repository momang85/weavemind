import { useState, useEffect, useCallback, useRef } from 'react'
import { Save, RotateCcw, Key, Cpu, Clock, Shield, Server, Bell, CalendarClock, Plus, Trash2, Activity, Users, AlertTriangle, CheckCircle2, PlugZap } from 'lucide-react'
import { useTaskStore } from '../stores/useTaskStore'

// ── T1 通知配置 ───────────────────────────────────────────────
function NotificationsSection() {
  const [ncfg, setNcfg] = useState<any>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    fetch('/api/notifications').then(r => r.json()).then(d => setNcfg(d.notifications || {})).catch(() => setError('加载通知配置失败'))
  }, [])
  useEffect(() => { load() }, [load])

  const save = async () => {
    setError('')
    try {
      const res = await fetch('/api/notifications', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ notifications: ncfg }),
      })
      if (!res.ok) throw new Error('保存失败')
      setSaved(true); setTimeout(() => setSaved(false), 2000)
    } catch (e: any) { setError(e.message || '保存失败') }
  }

  if (!ncfg) return null
  const channels = [
    { key: 'webhook', label: '通用 Webhook（企业微信/钉钉/自建）', fields: ['url'] },
    { key: 'serverchan', label: 'Server酱', fields: ['sendkey'] },
    { key: 'email', label: '邮件', fields: ['host', 'port', 'user', 'password', 'to'] },
  ]
  return (
    <section className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-5">
      <h2 className="flex items-center gap-2 text-sm text-slate-300 font-medium">
        <Bell className="w-4 h-4 text-amber-400" /> 通知配置（任务完成时推送报告链接）
      </h2>
      {channels.map(ch => {
        const item = ncfg[ch.key] || {}
        return (
          <div key={ch.key} className="border border-slate-800 rounded-lg p-4 space-y-3">
            <label className="flex items-center gap-3 text-sm text-slate-300">
              <input type="checkbox" checked={!!item.enabled}
                onChange={e => setNcfg({ ...ncfg, [ch.key]: { ...item, enabled: e.target.checked } })}
                className="accent-cyan-500" />
              {ch.label}
            </label>
            {item.enabled && ch.fields.map(f => (
              <input key={f}
                type={f === 'password' ? 'password' : f === 'port' ? 'number' : 'text'}
                value={item[f] || ''}
                placeholder={f}
                onChange={e => setNcfg({ ...ncfg, [ch.key]: { ...item, [f]: e.target.value } })}
                className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50" />
            ))}
          </div>
        )
      })}
      <div className="flex items-center gap-3">
        <button onClick={save} className="px-4 py-2 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-400 text-sm">保存通知配置</button>
        {saved && <span className="text-emerald-400 text-xs">已保存</span>}
        {error && <span className="text-amber-400 text-xs">{error}</span>}
      </div>
    </section>
  )
}

// ── T2 定时任务管理 ───────────────────────────────────────────
function ScheduledJobsSection() {
  const [jobs, setJobs] = useState<any[]>([])
  const [recent, setRecent] = useState<any[]>([])
  const [error, setError] = useState('')
  const [nf, setNf] = useState({ name: '', goal: '', cron: '09:00' })

  const load = useCallback(() => {
    fetch('/api/scheduled-jobs').then(r => r.json()).then(d => {
      setJobs(d.jobs || [])
      setRecent(d.recent || [])
    }).catch(() => setError('加载定时任务失败'))
  }, [])
  useEffect(() => { load() }, [load])

  const lastFire = (name: string) => recent.find((r: any) => r.job === name) || null

  const act = async (action: string, payload: any) => {
    setError('')
    try {
      const res = await fetch('/api/scheduled-jobs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, ...payload }),
      })
      if (!res.ok) throw new Error('操作失败')
      load()
    } catch (e: any) { setError(e.message || '操作失败') }
  }

  return (
    <section className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-4">
      <h2 className="flex items-center gap-2 text-sm text-slate-300 font-medium">
        <CalendarClock className="w-4 h-4 text-cyan-400" /> 定时任务（每日 HH:MM 触发一次）
      </h2>
      {jobs.map(j => (
        <div key={j.name} className="flex items-center gap-3 border border-slate-800 rounded-lg px-4 py-3">
          <input type="checkbox" checked={!!j.enabled}
            onChange={e => act('update', { job: { ...j, enabled: e.target.checked }, name: j.name })}
            className="accent-cyan-500" />
          <div className="flex-1 min-w-0">
            <div className="text-sm text-slate-200 truncate">{j.name}
              <span className="ml-2 text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-400">{j.cron ? `每日 ${j.cron}` : `每 ${j.interval_minutes} 分钟`}</span>
              <span className="ml-2 text-[10px] px-2 py-0.5 rounded bg-violet-500/10 text-violet-400">{j.project}</span>
            </div>
            <div className="text-xs text-slate-500 truncate mt-1">{j.goal}</div>
            {(() => {
              const rec = lastFire(j.name)
              if (!rec) return null
              const bad = rec.result === 'error'
              return (
                <div className={`text-[10px] mt-1 ${bad ? 'text-red-400' : 'text-slate-600'}`}>
                  上次：{rec.time || '-'} · {rec.result || '-'}
                  {rec.task_id ? ` · ${rec.task_id}` : ''}
                  {rec.detail ? `（${rec.detail}）` : ''}
                </div>
              )
            })()}
          </div>
          <button onClick={() => act('delete', { name: j.name })}
            className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 text-xs">
            <Trash2 className="w-3 h-3" /> 删除
          </button>
        </div>
      ))}
      <div className="border-t border-slate-800 pt-4 grid grid-cols-1 md:grid-cols-4 gap-3">
        <input value={nf.name} placeholder="任务名" onChange={e => setNf({ ...nf, name: e.target.value })}
          className="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200" />
        <input value={nf.cron} placeholder="cron 如 16:00" onChange={e => setNf({ ...nf, cron: e.target.value })}
          className="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200" />
        <input value={nf.goal} placeholder="任务目标" onChange={e => setNf({ ...nf, goal: e.target.value })}
          className="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 md:col-span-2" />
        <button onClick={() => act('add', { job: nf })} disabled={!nf.name || !nf.goal}
          className="flex items-center justify-center gap-1 px-4 py-2 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-400 text-sm disabled:opacity-40">
          <Plus className="w-3.5 h-3.5" /> 新增任务
        </button>
      </div>
      {error && <div className="text-amber-400 text-xs">{error}</div>}
    </section>
  )
}

// ── 依赖与端点总览：系统到底需要哪些 API + 当前状态 ──────────────
type ReqEntry = {
  path: string; label: string; kind: string; status: string; required: boolean
  editable: boolean; testable: boolean; affects: string; group: string
  env_name: string; default: any; value: any; configured: boolean
  api_key_set: boolean; source: string; health: string
  health_ok: boolean | null; health_reason: string
}
type ReqSection = { key: string; label: string; note: string; entries: ReqEntry[] }
type ReqPayload = {
  sections: ReqSection[]; health: any[]; balance: any; budget: any
  missing_required: string[]; degraded: string[]; test_targets: string[]
}

const STATUS_BADGE: Record<string, { text: string; cls: string }> = {
  active: { text: '生效中', cls: 'bg-emerald-500/10 text-emerald-400' },
  env_only: { text: '仅环境变量', cls: 'bg-slate-700/40 text-slate-400' },
  reserved: { text: '预留未接线', cls: 'bg-amber-500/10 text-amber-400' },
  unused: { text: '模板字段·代码未读取', cls: 'bg-amber-500/10 text-amber-400' },
}

function healthBadge(entry: ReqEntry) {
  if (entry.health_ok === false) {
    const reason = entry.health_reason || ''
    const quota = /402|insufficient|balance|credit|额度|余额/i.test(reason)
    return { text: quota ? '欠费/额度不足' : '异常', cls: 'bg-red-500/15 text-red-400', title: reason }
  }
  if (entry.health_ok === true && entry.health) {
    return { text: '正常', cls: 'bg-emerald-500/10 text-emerald-400', title: entry.health_reason }
  }
  if (entry.required && !entry.configured) {
    return { text: '未配置', cls: 'bg-amber-500/15 text-amber-400', title: '该项为必需项，缺失会影响任务执行' }
  }
  return null
}

export default function SettingsPage() {
  const [req, setReq] = useState<ReqPayload | null>(null)
  const [values, setValues] = useState<Record<string, any>>({})
  const [dirty, setDirty] = useState<Set<string>>(new Set())
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  const [issues, setIssues] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [testing, setTesting] = useState('')
  const [testResult, setTestResult] = useState<Record<string, any>>({})
  const loadedRef = useRef<Record<string, any>>({})
  const { fetchSystemStatus } = useTaskStore()

  const load = useCallback(() => {
    setLoading(true)
    fetch('/api/config/requirements')
      .then(r => r.json())
      .then((d: ReqPayload) => {
        setReq(d)
        const seed: Record<string, any> = {}
        d.sections.forEach(sec => sec.entries.forEach(e => { seed[e.path] = e.value ?? '' }))
        loadedRef.current = seed
        setValues(seed)
        setDirty(new Set())
      })
      .catch(() => setError('加载配置清单失败'))
      .finally(() => setLoading(false))
  }, [])
  useEffect(() => { load() }, [load])

  const update = (path: string, value: any) => {
    setValues(prev => ({ ...prev, [path]: value }))
    setDirty(prev => new Set(prev).add(path))
    setSaved(false)
  }

  // 按点号路径构造嵌套负载（只提交被修改过的项）
  const buildPayload = () => {
    const out: Record<string, any> = {}
    dirty.forEach(path => {
      const parts = path.split('.')
      let node = out
      parts.forEach((part, i) => {
        if (i === parts.length - 1) { node[part] = values[path]; return }
        node[part] = node[part] || {}
        node = node[part]
      })
    })
    return out
  }

  const save = useCallback(async () => {
    setError(''); setIssues([])
    if (dirty.size === 0) { setSaved(true); setTimeout(() => setSaved(false), 1500); return }
    try {
      const res = await fetch('/api/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildPayload()),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        setIssues(data?.issues || [])
        throw new Error(data?.error || '保存失败')
      }
      setSaved(true); fetchSystemStatus(); load()
      setTimeout(() => setSaved(false), 2000)
    } catch (e: any) { setError(e.message || '保存失败') }
  }, [dirty, values, fetchSystemStatus, load])

  const runTest = async (target: string) => {
    setTesting(target); setError('')
    try {
      const res = await fetch('/api/config/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target }),
      })
      const data = await res.json()
      setTestResult(prev => ({ ...prev, [target]: data }))
    } catch (e: any) {
      setTestResult(prev => ({ ...prev, [target]: { ok: false, reason: e.message } }))
    } finally { setTesting('') }
  }

  if (loading && !req) return <div className="flex items-center justify-center h-64 text-slate-500">Loading...</div>

  const renderEntry = (e: ReqEntry, sectionTarget?: string) => {
    const badge = healthBadge(e)
    const disabled = !e.editable
    return (
      <div key={e.path} className="border border-slate-800 rounded-lg p-3 space-y-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs text-slate-300">{e.label}</span>
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800/70 text-slate-500 font-mono">{e.path}</span>
          {badge && <span title={badge.title} className={`text-[10px] px-1.5 py-0.5 rounded ${badge.cls}`}>{badge.text}</span>}
          {STATUS_BADGE[e.status] && e.status !== 'active' && (
            <span className={`text-[10px] px-1.5 py-0.5 rounded ${STATUS_BADGE[e.status].cls}`}>{STATUS_BADGE[e.status].text}</span>
          )}
          <span className="text-[10px] text-slate-600">来源：{e.source === 'config' ? 'config.json' : e.source === 'env' ? `环境变量 ${e.env_name || ''}` : '默认值'}</span>
          {e.testable && (
            <button onClick={() => runTest(sectionTarget || 'llm')}
              disabled={testing !== ''}
              className="ml-auto flex items-center gap-1 px-2 py-1 rounded bg-cyan-500/10 hover:bg-cyan-500/20 text-cyan-400 text-[11px] disabled:opacity-40">
              <PlugZap className="w-3 h-3" /> {testing === (sectionTarget || 'llm') ? '测试中…' : '测试'}
            </button>
          )}
        </div>
        {e.kind === 'list' ? (
          <McpServersEditor disabled={disabled} onChange={v => update(e.path, v)}
            value={Array.isArray(values[e.path]) ? values[e.path] : []} />
        ) : e.kind === 'bool' ? (
          <label className="flex items-center gap-2 text-xs text-slate-300">
            <input type="checkbox" disabled={disabled} checked={!!values[e.path]}
              onChange={ev => update(e.path, ev.target.checked)} className="accent-cyan-500" />
            {values[e.path] ? '启用' : '停用'}
          </label>
        ) : (
          <input
            type={e.kind === 'secret' ? 'password' : e.kind === 'int' || e.kind === 'float' ? 'number' : 'text'}
            disabled={disabled}
            value={values[e.path] ?? ''}
            placeholder={e.kind === 'secret'
              ? (e.configured ? '••••••••（已配置，留空保持不变）' : '未配置')
              : (e.default !== null && e.default !== undefined ? `默认 ${e.default}` : '')}
            onChange={ev => update(e.path, e.kind === 'int' ? (parseInt(ev.target.value) || 0)
              : e.kind === 'float' ? (parseFloat(ev.target.value) || 0) : ev.target.value)}
            className="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50 disabled:opacity-50" />
        )}
        {e.affects && <div className="text-[11px] text-slate-500">影响：{e.affects}</div>}
      </div>
    )
  }

  const problemCount = (req?.degraded?.length || 0) + (req?.missing_required?.length || 0)

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-slate-200 font-semibold text-lg">Settings</h1>
        <div className="flex items-center gap-3">
          <button onClick={() => { setValues(loadedRef.current); setDirty(new Set()); setError(''); setIssues([]) }}
            className="flex items-center gap-2 px-4 py-2 text-sm text-slate-400 hover:text-slate-300 bg-slate-800/50 hover:bg-slate-800 rounded-lg transition-colors border border-slate-700/50">
            <RotateCcw className="w-4 h-4" /> Reset
          </button>
          <button onClick={save}
            className="flex items-center gap-2 px-5 py-2 text-sm font-semibold bg-cyan-500 hover:bg-cyan-400 text-slate-950 rounded-lg transition-colors">
            <Save className="w-4 h-4" /> {saved ? 'Saved!' : dirty.size > 0 ? `Save (${dirty.size})` : 'Save'}
          </button>
        </div>
      </div>

      {error && <div className="bg-red-500/10 border border-red-500/30 text-red-400 text-sm rounded-lg px-4 py-3">
        {error}
        {issues.length > 0 && <ul className="mt-1 text-xs list-disc list-inside">{issues.map((i, idx) => <li key={idx}>{i}</li>)}</ul>}
      </div>}

      {/* 顶置：需要处理的问题（欠费/未配置）——embedding 欠费会出现在这里 */}
      <section className={`border rounded-xl p-5 space-y-3 ${problemCount > 0 ? 'bg-red-500/5 border-red-500/30' : 'bg-emerald-500/5 border-emerald-500/20'}`}>
        <h2 className="flex items-center gap-2 text-sm font-medium text-slate-200">
          {problemCount > 0 ? <AlertTriangle className="w-4 h-4 text-red-400" /> : <CheckCircle2 className="w-4 h-4 text-emerald-400" />}
          {problemCount > 0 ? `需要处理：${problemCount} 项` : '所有必需的端点与依赖均正常'}
        </h2>
        {req?.degraded?.length ? (
          <div className="text-xs text-red-300 space-y-1">
            {req.degraded.map(p => {
              const entry = req.sections.flatMap(s => s.entries).find(e => e.path === p)
              return <div key={p}>· {entry?.label || p}（{p}）：{entry?.health_reason?.slice(0, 120) || '异常'}</div>
            })}
          </div>
        ) : null}
        {req?.missing_required?.length ? (
          <div className="text-xs text-amber-300">· 必需项未配置：{req.missing_required.join('、')}</div>
        ) : null}
        <div className="text-[11px] text-slate-500">
          余额：主端点 {req?.balance?.primary?.reason || '-'} · 备用 {req?.balance?.backup?.reason || '-'}
          {req?.budget?.limit_usd ? ` · 本月已用 $${req.budget.spend_usd ?? 0} / $${req.budget.limit_usd}` : ''}
        </div>
      </section>

      {/* Schema 驱动的全部端点/参数 */}
      {req?.sections.map(sec => {
        const entries = sec.entries
        const groups = Array.from(new Set(entries.map(e => e.group).filter(Boolean)))
        const sectionTarget = ({ llm: 'llm', planner: 'planner', backup: 'backup', embedding: 'embedding' } as Record<string, string>)[sec.key]
        const testable = sec.key === 'mcp_servers' ? 'mcp' : sectionTarget
        return (
          <section key={sec.key} className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="flex items-center gap-2 text-sm text-slate-300 font-medium">
                  {sec.key === 'llm' || sec.key === 'planner' || sec.key === 'backup' || sec.key === 'embedding'
                    ? <Cpu className="w-4 h-4 text-cyan-400" />
                    : sec.key === 'redis' ? <Server className="w-4 h-4 text-amber-400" />
                    : sec.key === 'system' ? <Clock className="w-4 h-4 text-violet-400" />
                    : <Shield className="w-4 h-4 text-slate-400" />}
                  {sec.label}
                </h2>
                {sec.note && <p className="text-[11px] text-slate-500 mt-1">{sec.note}</p>}
              </div>
              {testable && (
                <button onClick={() => runTest(testable)} disabled={testing !== ''}
                  className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-cyan-500/10 hover:bg-cyan-500/20 text-cyan-400 text-xs disabled:opacity-40">
                  <Activity className="w-3.5 h-3.5" /> {testing === testable ? '测试中…' : '测试连通性'}
                </button>
              )}
            </div>
            {testResult[testable || ''] && (
              <div className={`text-xs rounded-lg px-3 py-2 ${testResult[testable || ''].ok ? 'bg-emerald-500/10 text-emerald-300' : 'bg-red-500/10 text-red-300'}`}>
                {testResult[testable || ''].ok ? '连通正常' : '异常'} · {testResult[testable || ''].latency_ms}ms
                {testResult[testable || ''].reason ? ` · ${testResult[testable || ''].reason}` : ''}
              </div>
            )}
            {groups.length > 0 ? groups.map(g => (
              <div key={g} className="space-y-3">
                <div className="text-[11px] text-slate-500 uppercase tracking-wider">{g}</div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {entries.filter(e => e.group === g).map(e => renderEntry(e, sectionTarget))}
                </div>
              </div>
            )) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">{entries.map(e => renderEntry(e, sectionTarget))}</div>
            )}
          </section>
        )
      })}

      <UsersSection />
      <NotificationsSection />
      <ScheduledJobsSection />
    </div>
  )
}

// ── 用户管理（admin）─────────────────────────────────────────
function UsersSection() {
  const [users, setUsers] = useState<any[]>([])
  const [error, setError] = useState('')
  const [form, setForm] = useState({ username: '', password: '', role: 'viewer' })

  const load = useCallback(() => {
    fetch('/api/users').then(r => r.json()).then(d => setUsers(d.users || [])).catch(() => setError('加载用户失败'))
  }, [])
  useEffect(() => { load() }, [load])

  const act = async (method: string, path: string, body?: any) => {
    setError('')
    try {
      const res = await fetch(path, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.error || '操作失败')
      if (data.users) setUsers(data.users); else load()
      return true
    } catch (e: any) { setError(e.message || '操作失败'); return false }
  }

  return (
    <section className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-4">
      <h2 className="flex items-center gap-2 text-sm text-slate-300 font-medium">
        <Users className="w-4 h-4 text-cyan-400" /> 用户（密码哈希仅服务端保存，不回显）
      </h2>
      {users.map(u => (
        <div key={u.username} className="flex items-center gap-3 border border-slate-800 rounded-lg px-4 py-3">
          <div className="flex-1 min-w-0">
            <div className="text-sm text-slate-200">
              {u.username}
              <span className={`ml-2 text-[10px] px-2 py-0.5 rounded ${u.role === 'admin' ? 'bg-cyan-500/10 text-cyan-400' : 'bg-slate-700/40 text-slate-400'}`}>{u.role}</span>
            </div>
            <div className="text-[10px] text-slate-600 mt-1">创建于 {u.created_at || '-'}</div>
          </div>
          <select value={u.role} onChange={e => act('POST', '/api/users', { username: u.username, role: e.target.value })}
            className="bg-slate-800 border border-slate-700 rounded-lg px-2 py-1 text-xs text-slate-200">
            <option value="admin">admin</option>
            <option value="viewer">viewer</option>
          </select>
          <button onClick={() => {
            const pwd = window.prompt(`为 ${u.username} 设置新密码（至少 8 位）`)
            if (pwd) act('POST', '/api/users', { username: u.username, password: pwd })
          }} className="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs">
            <Key className="w-3 h-3 inline" /> 改密
          </button>
          <button onClick={() => act('DELETE', `/api/users/${encodeURIComponent(u.username)}`)}
            className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 text-xs">
            <Trash2 className="w-3 h-3" /> 删除
          </button>
        </div>
      ))}
      <div className="border-t border-slate-800 pt-4 grid grid-cols-1 md:grid-cols-4 gap-3">
        <input value={form.username} placeholder="用户名" onChange={e => setForm({ ...form, username: e.target.value })}
          className="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200" />
        <input value={form.password} type="password" placeholder="密码（≥8 位）" onChange={e => setForm({ ...form, password: e.target.value })}
          className="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200" />
        <select value={form.role} onChange={e => setForm({ ...form, role: e.target.value })}
          className="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200">
          <option value="viewer">viewer</option>
          <option value="admin">admin</option>
        </select>
        <button onClick={async () => {
          const ok = await act('POST', '/api/users', form)
          if (ok) setForm({ username: '', password: '', role: 'viewer' })
        }} disabled={!form.username || form.password.length < 8}
          className="flex items-center justify-center gap-1 px-4 py-2 rounded-lg bg-cyan-500/15 hover:bg-cyan-500/25 text-cyan-400 text-sm disabled:opacity-40">
          <Plus className="w-3.5 h-3.5" /> 新增用户
        </button>
      </div>
      {error && <div className="text-amber-400 text-xs">{error}</div>}
    </section>
  )
}

// ── MCP 服务列表编辑 ─────────────────────────────────────────
function McpServersEditor({ value, onChange, disabled }: {
  value: any[]; onChange: (v: any[]) => void; disabled?: boolean
}) {
  const rows = value.filter(v => !(v && v._comment))
  const setRow = (idx: number, patch: any) => {
    const next = rows.map((r, i) => i === idx ? { ...r, ...patch } : r)
    onChange(next)
  }
  return (
    <div className="space-y-2">
      {rows.map((r, i) => (
        <div key={i} className="grid grid-cols-1 md:grid-cols-3 gap-2">
          <input value={r.name || ''} disabled={disabled} placeholder="名称（如 wind）"
            onChange={e => setRow(i, { name: e.target.value })}
            className="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200" />
          <input value={r.url || ''} disabled={disabled} placeholder="HTTP 端点（url）"
            onChange={e => setRow(i, { url: e.target.value })}
            className="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200" />
          <div className="flex gap-2">
            <input value={r.command || ''} disabled={disabled} placeholder="或本地命令（command）"
              onChange={e => setRow(i, { command: e.target.value })}
              className="flex-1 bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200" />
            <button onClick={() => onChange(rows.filter((_, idx) => idx !== i))} disabled={disabled}
              className="px-2 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-400 text-xs disabled:opacity-40">
              <Trash2 className="w-3 h-3" />
            </button>
          </div>
        </div>
      ))}
      <button onClick={() => onChange([...rows, { name: '', url: '', command: '' }])} disabled={disabled}
        className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs disabled:opacity-40">
        <Plus className="w-3 h-3" /> 添加 MCP 服务
      </button>
      <div className="text-[11px] text-slate-500">
        常见来源：Wind（商业授权）、同花顺 iFinD（付费）、SEC EDGAR（已内置适配器，无需 MCP）。
      </div>
    </div>
  )
}
