import { useState, useEffect, useCallback, useRef } from 'react'
import { Save, RotateCcw, Key, Cpu, Clock, Shield, Server, Bell, CalendarClock, Plus, Trash2, Activity, Users, AlertTriangle, CheckCircle2, PlugZap } from 'lucide-react'
import { useTaskStore } from '../stores/useTaskStore'
import {
  groupDegradedRequirements, requirementFieldLabel,
} from '../lib/depHealth'
import { balanceStatusText, roleLabel, scheduledResultLabel } from '../lib/settingsText'
import { formatLocalTimeLabel } from '../lib/timeText'
import { LoadingBlock } from '../components/ui'

// ── T1 通知配置 ───────────────────────────────────────────────
function NotificationsSection() {
  const [ncfg, setNcfg] = useState<any>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  // hook 在早退之前（`if (!ncfg) return null` 之后调 hook 会触发 React #310）
  const demoMode = useTaskStore(s => s.demoMode)

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
    <section className="bg-surface border border-line rounded-xl p-6 space-y-5">
      <h2 className="flex items-center gap-2 text-sm text-ink font-medium">
        <Bell className="w-4 h-4 text-amber-700" /> 通知配置（任务完成时推送报告链接）
      </h2>
      {channels.map(ch => {
        const item = ncfg[ch.key] || {}
        return (
          <div key={ch.key} className="border border-line rounded-lg p-4 space-y-3">
            <label className="flex items-center gap-3 text-sm text-ink">
              <input type="checkbox" checked={!!item.enabled}
                onChange={e => setNcfg({ ...ncfg, [ch.key]: { ...item, enabled: e.target.checked } })}
                className="accent-brand" />
              {ch.label}
            </label>
            {item.enabled && ch.fields.map(f => (
              <input key={f}
                type={f === 'password' ? 'password' : f === 'port' ? 'number' : 'text'}
                value={item[f] || ''}
                placeholder={f}
                onChange={e => setNcfg({ ...ncfg, [ch.key]: { ...item, [f]: e.target.value } })}
                className="w-full bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink focus:outline-none focus:border-brand/50" />
            ))}
          </div>
        )
      })}
      <div className="flex items-center gap-3">
        <button onClick={save} disabled={demoMode}
          title={demoMode ? '演示模式下已停用（保存会真实改写通知配置）' : '保存通知配置'}
          className="px-4 py-2 rounded-lg bg-brand/15 hover:bg-brand/25 text-brand text-sm disabled:opacity-40 disabled:cursor-not-allowed">保存通知配置</button>
        {saved && <span className="text-emerald-700 text-xs">已保存</span>}
        {error && <span className="text-amber-700 text-xs">{error}</span>}
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
  const demoMode = useTaskStore(s => s.demoMode)

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
    <section className="bg-surface border border-line rounded-xl p-6 space-y-4">
      <h2 className="flex items-center gap-2 text-sm text-ink font-medium">
        <CalendarClock className="w-4 h-4 text-brand" /> 定时任务（每日 HH:MM 触发一次）
      </h2>
      {jobs.map(j => (
        <div key={j.name} className="flex items-center gap-3 border border-line rounded-lg px-4 py-3">
          <input type="checkbox" checked={!!j.enabled} disabled={demoMode}
            title={demoMode ? '演示模式下已停用（会真实改定时任务）' : '启用/停用该定时任务'}
            onChange={e => act('update', { job: { ...j, enabled: e.target.checked }, name: j.name })}
            className="accent-brand disabled:opacity-40" />
          <div className="flex-1 min-w-0">
            <div className="text-sm text-ink break-words">{j.name}
              <span className="ml-2 text-xs px-2 py-0.5 rounded bg-surface-raised text-ink-muted">{j.cron ? `每日 ${j.cron}` : `每 ${j.interval_minutes} 分钟`}</span>
              <span className="ml-2 text-xs px-2 py-0.5 rounded bg-violet-500/10 text-violet-700">{j.project}</span>
            </div>
            <div className="text-xs text-ink-faint break-words mt-1">{j.goal}</div>
            {(() => {
              const rec = lastFire(j.name)
              if (!rec) return null
              const bad = rec.result === 'error'
              return (
                <div className={`text-xs mt-1 ${bad ? 'text-red-700' : 'text-ink-faint'}`}>
                  上次：{rec.time ? formatLocalTimeLabel(rec.time) : '未知（本次运行内尚无该任务的触发记录）'} · {scheduledResultLabel(rec.result)}
                  {rec.task_id ? ` · ${rec.task_id}` : ''}
                  {rec.detail ? `（${rec.detail}）` : ''}
                </div>
              )
            })()}
          </div>
          <button onClick={() => act('delete', { name: j.name })} disabled={demoMode}
            title={demoMode ? '演示模式下已停用（会真实删除定时任务）' : '删除该定时任务'}
            className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-700 text-xs disabled:opacity-40">
            <Trash2 className="w-3 h-3" /> 删除
          </button>
        </div>
      ))}
      <div className="border-t border-line pt-4 grid grid-cols-1 md:grid-cols-4 gap-3">
        <input value={nf.name} placeholder="任务名" onChange={e => setNf({ ...nf, name: e.target.value })}
          className="bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink" />
        <input value={nf.cron} placeholder="cron 如 16:00" onChange={e => setNf({ ...nf, cron: e.target.value })}
          className="bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink" />
        <input value={nf.goal} placeholder="任务目标" onChange={e => setNf({ ...nf, goal: e.target.value })}
          className="bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink md:col-span-2" />
        <button onClick={() => act('add', { job: nf })} disabled={demoMode || !nf.name || !nf.goal}
          title={demoMode ? '演示模式下已停用（新增后会真实触发任务）' : '新增定时任务'}
          className="flex items-center justify-center gap-1 px-4 py-2 rounded-lg bg-brand/15 hover:bg-brand/25 text-brand text-sm disabled:opacity-40">
          <Plus className="w-3.5 h-3.5" /> 新增任务
        </button>
      </div>
      {error && <div className="text-amber-700 text-xs">{error}</div>}
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
  health_state?: string | null; health_stale?: boolean; health_checked_at?: number
}
type ReqSection = { key: string; label: string; note: string; entries: ReqEntry[] }
type ReqPayload = {
  sections: ReqSection[]; health: any[]; balance: any; budget: any
  missing_required: string[]; degraded: string[]; unknown_health?: string[]
  test_targets: string[]
}

const STATUS_BADGE: Record<string, { text: string; cls: string }> = {
  active: { text: '生效中', cls: 'bg-emerald-500/10 text-emerald-700' },
  env_only: { text: '仅环境变量', cls: 'bg-line/40 text-ink-muted' },
  reserved: { text: '预留未接线', cls: 'bg-amber-500/10 text-amber-700' },
  unused: { text: '模板字段·代码未读取', cls: 'bg-amber-500/10 text-amber-700' },
}

function healthBadge(entry: ReqEntry) {
  // 必需项未配置优先于健康状态：健康判据取自进程内端点状态，缺 key 时它仍可能是
  // "正常"（新实例实测：llm.api_key 标"必需项未配置"、同行徽章却显示"正常"，
  // 徽章的 tooltip 还留着上一次 401 的原因——三处自相矛盾）。
  if (entry.required && !entry.configured) {
    return {
      text: '未配置',
      cls: 'bg-amber-500/15 text-amber-700',
      title: entry.health_reason
        ? `必需项未配置：${entry.health_reason}`
        : '该项为必需项，缺失会影响任务执行',
    }
  }
  if (entry.health_ok === false) {
    const reason = entry.health_reason || ''
    // 状态来自 health_registry：unknown = 没有观测证据（无快照/已过期/来自别的实例），
    // 不能显示成"异常"，也不能显示成"正常"——两者都会误导排查方向。
    if (entry.health_state === 'unknown') {
      return {
        text: entry.health_stale ? '读数过期' : '未检查',
        cls: 'bg-line/40 text-ink',
        title: reason || '暂无该依赖的观测快照（未检查或已过期）',
      }
    }
    const quota = /402|insufficient|balance|credit|额度|余额/i.test(reason)
    return { text: quota ? '欠费/额度不足' : '异常', cls: 'bg-red-500/15 text-red-700', title: reason }
  }
  if (entry.health_state === 'degraded') {
    return {
      text: '降级',
      cls: 'bg-amber-500/15 text-amber-700',
      title: entry.health_reason || '部分能力不可用，其余仍可用',
    }
  }
  // 未配置的可选项不显示"正常"（那只是进程内默认状态，不代表真的可用）
  if (entry.health_ok === true && entry.health && entry.configured) {
    return { text: '正常', cls: 'bg-emerald-500/10 text-emerald-700', title: entry.health_reason }
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

  // 演示模式：本页的保存 / 连通性测试会真实写配置或调用付费端点，逐项停用
  //（fetch 层另有统一兜底拦截，见 lib/demoGuard.ts）
  // 注意：必须放在下面的早退（if (loading && !req) return …）**之前**——
  // hook 在早退之后会让登录/加载态翻转时 hook 数量变化，React 抛 #310 卸载整棵树。
  const demoMode = useTaskStore(s => s.demoMode)

  if (loading && !req) return <LoadingBlock label="正在读取配置…" />

  const renderEntry = (e: ReqEntry, sectionTarget?: string) => {
    const badge = healthBadge(e)
    const disabled = !e.editable
    return (
      <div key={e.path} className={`border border-line rounded-lg p-3 space-y-2 ${e.kind === 'list' ? 'md:col-span-2' : ''}`}>
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs text-ink">{e.label}</span>
          <span className="text-xs px-1.5 py-0.5 rounded bg-surface-raised/70 text-ink-faint font-mono">{e.path}</span>
          {badge && <span title={badge.title} className={`text-xs px-1.5 py-0.5 rounded ${badge.cls}`}>{badge.text}</span>}
          {STATUS_BADGE[e.status] && e.status !== 'active' && (
            <span className={`text-xs px-1.5 py-0.5 rounded ${STATUS_BADGE[e.status].cls}`}>{STATUS_BADGE[e.status].text}</span>
          )}
          <span className="text-xs text-ink-faint">来源：{e.source === 'config' ? 'config.json' : e.source === 'env' ? `环境变量 ${e.env_name || ''}` : '默认值'}</span>
          {/* 数据源列表的探测是**逐源**的（每个源一个测试按钮），段级按钮会误打 llm */}
          {e.testable && e.path !== 'data_sources' && (
            <button onClick={() => runTest(sectionTarget || 'llm')}
              disabled={testing !== ''}
              className="ml-auto flex items-center gap-1 px-2 py-1 rounded bg-brand/10 hover:bg-brand/20 text-brand text-xs disabled:opacity-40">
              <PlugZap className="w-3 h-3" /> {testing === (sectionTarget || 'llm') ? '测试中…' : '测试'}
            </button>
          )}
        </div>
        {e.kind === 'list' ? (
          e.path === 'data_sources' ? (
            <DataSourcesEditor
              value={Array.isArray(values[e.path]) ? values[e.path] : []}
              onChange={v => update(e.path, v)}
              disabled={disabled || demoMode}
              onTest={runTest}
              testing={testing}
              resultOf={target => testResult[target]} />
          ) : (
            <McpServersEditor disabled={disabled} onChange={v => update(e.path, v)}
              value={Array.isArray(values[e.path]) ? values[e.path] : []} />
          )
        ) : e.kind === 'bool' ? (
          <label className="flex items-center gap-2 text-xs text-ink">
            <input type="checkbox" disabled={disabled} checked={!!values[e.path]}
              onChange={ev => update(e.path, ev.target.checked)} className="accent-brand" />
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
            className="w-full bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink focus:outline-none focus:border-brand/50 disabled:opacity-50" />
        )}
        {e.affects && <div className="text-xs text-ink-faint">影响：{e.affects}</div>}
      </div>
    )
  }

  const problemCount = (req?.degraded?.length || 0) + (req?.missing_required?.length || 0)
  // 降级项按**相同原因**合并：实机上 3 个字段共用同一条 400（embedding 端点），
  // 旧实现逐字段直出原始 JSON，看起来像 3 个不同故障。
  const allEntries = (req?.sections || []).flatMap(s => s.entries)
  const issueGroups = groupDegradedRequirements(
    (req?.degraded || []).map(p => {
      const e = allEntries.find(x => x.path === p)
      return { path: p, label: e?.label, reason: e?.health_reason }
    }),
  )

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-surface p-4 shadow-sm">
        <h1 className="text-ink font-semibold text-lg">设置</h1>
        <div className="flex items-center gap-3">
          <button onClick={() => { setValues(loadedRef.current); setDirty(new Set()); setError(''); setIssues([]) }}
            className="flex items-center gap-2 px-4 py-2 text-sm text-ink-muted hover:text-ink bg-surface-raised/50 hover:bg-surface-raised rounded-lg transition-colors border border-line-strong/50">
            <RotateCcw className="w-4 h-4" /> 重置
          </button>
          <button onClick={save} disabled={demoMode}
            title={demoMode ? '演示模式下已停用（保存会真实改写配置）' : '保存配置（写回 config.json）'}
            className="flex items-center gap-2 px-5 py-2 text-sm font-semibold bg-brand hover:bg-brand text-white rounded-lg transition-colors disabled:opacity-40">
            <Save className="w-4 h-4" /> {saved ? '已保存' : dirty.size > 0 ? `保存 (${dirty.size})` : '保存'}
          </button>
        </div>
      </div>

      {error && <div className="bg-red-500/10 border border-red-500/30 text-red-700 text-sm rounded-lg px-4 py-3">
        {error}
        {issues.length > 0 && <ul className="mt-1 text-xs list-disc list-inside">{issues.map((i, idx) => <li key={idx}>{i}</li>)}</ul>}
      </div>}

      {/* 顶置：需要处理的问题（欠费/未配置）——embedding 欠费会出现在这里 */}
      <section className={`border rounded-xl p-5 space-y-3 ${problemCount > 0 ? 'bg-red-500/5 border-red-500/30' : 'bg-emerald-500/5 border-emerald-500/20'}`}>
        <h2 className="flex items-center gap-2 text-sm font-medium text-ink">
          {problemCount > 0 ? <AlertTriangle className="w-4 h-4 text-red-700" /> : <CheckCircle2 className="w-4 h-4 text-emerald-700" />}
          {problemCount > 0
            ? `需要处理：${problemCount} 项配置${issueGroups.length > 0 ? `（由 ${issueGroups.length} 个原因引起）` : ''}`
            : '所有必需的端点与依赖均正常'}
        </h2>
        {issueGroups.length > 0 && (
          <div className="text-xs text-red-700 space-y-3">
            {issueGroups.map(g => (
              <div key={g.key} className="border border-red-500/20 rounded-lg px-3 py-2 space-y-1">
                <div className="text-red-700">结论：{g.view.conclusion}</div>
                <div className="text-red-700/90">建议：{g.view.advice}</div>
                <div className="text-ink-muted">
                  受影响 {g.fields.length} 项配置：{g.fields.join('、')}
                </div>
                <details className="text-ink-muted">
                  <summary className="cursor-pointer select-none">查看服务端原始报文（原样，不做截断）</summary>
                  <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-ink-faint">
                    {g.view.raw || '（服务端未回传 health_reason）'}
                  </pre>
                  {g.view.rawNote && <div className="mt-1 text-amber-700/90">{g.view.rawNote}</div>}
                </details>
              </div>
            ))}
          </div>
        )}
        {req?.missing_required?.length ? (
          <div className="text-xs text-amber-700">
            · 必需项未配置（{req.missing_required.length} 项）：
            {req.missing_required
              .map(p => requirementFieldLabel(allEntries.find(e => e.path === p)?.label, p))
              .join('、')}
          </div>
        ) : null}
        <div className="text-xs text-ink-faint">
          余额：主端点 {balanceStatusText(req?.balance?.primary?.ok, req?.balance?.primary?.reason, req?.balance?.primary?.message)}
          {' · '}备用 {balanceStatusText(req?.balance?.backup?.ok, req?.balance?.backup?.reason, req?.balance?.backup?.message)}
          {req?.budget?.limit_usd ? ` · 本月已用 $${req.budget.spend_usd ?? 0} / $${req.budget.limit_usd}` : ''}
          <span className="text-ink-faint">
            （口径：主/备为 LLM 端点预检结果——能否连通、鉴权是否通过、额度是否够用，不是金额读数；
            「本月已用」取 Redis llm_usage_month 月度台账，与指标页「本月累计成本」同本账，不是历史总额）
          </span>
        </div>
      </section>

      {/* Schema 驱动的全部端点/参数 */}
      {req?.sections.map(sec => {
        const entries = sec.entries
        const groups = Array.from(new Set(entries.map(e => e.group).filter(Boolean)))
        const sectionTarget = ({ llm: 'llm', planner: 'planner', backup: 'backup', embedding: 'embedding' } as Record<string, string>)[sec.key]
        const testable = sec.key === 'mcp_servers' ? 'mcp' : sectionTarget
        return (
          <section key={sec.key} className="bg-surface border border-line rounded-xl p-6 space-y-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="flex items-center gap-2 text-sm text-ink font-medium">
                  {sec.key === 'llm' || sec.key === 'planner' || sec.key === 'backup' || sec.key === 'embedding'
                    ? <Cpu className="w-4 h-4 text-brand" />
                    : sec.key === 'redis' ? <Server className="w-4 h-4 text-amber-700" />
                    : sec.key === 'system' ? <Clock className="w-4 h-4 text-violet-700" />
                    : <Shield className="w-4 h-4 text-ink-muted" />}
                  {sec.label}
                </h2>
                {sec.note && <p className="text-xs text-ink-faint mt-1">{sec.note}</p>}
              </div>
              {testable && (
                <button onClick={() => runTest(testable)} disabled={testing !== '' || demoMode}
                  title={demoMode ? '演示模式下已停用（测试会调用真实付费端点）' : '测试该功能是否可用'}
                  className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-brand/10 hover:bg-brand/20 text-brand text-xs disabled:opacity-40">
                  <Activity className="w-3.5 h-3.5" /> {testing === testable ? '测试中…' : '测试连通性'}
                </button>
              )}
            </div>
            {testResult[testable || ''] && (
              <div className={`text-xs rounded-lg px-3 py-2 ${testResult[testable || ''].ok ? 'bg-emerald-500/10 text-emerald-700' : 'bg-red-500/10 text-red-700'}`}>
                {testResult[testable || ''].ok ? '连通正常' : '异常'} · {testResult[testable || ''].latency_ms}ms
                {testResult[testable || ''].reason || testResult[testable || ''].detail
                  ? ` · ${balanceStatusText(testResult[testable || ''].ok, testResult[testable || ''].reason, testResult[testable || ''].detail)}`
                  : ''}
                {/* detail 是后端给的「带端点域名的整句原因」（含 base/model 回显）：失败时
                    单独一行显示，避免与上面的状态词挤成一句读不完的长行。 */}
                {!testResult[testable || ''].ok && testResult[testable || ''].detail ? (
                  <div className="mt-1 text-ink-muted break-all">{testResult[testable || ''].detail}</div>
                ) : null}
              </div>
            )}
            {groups.length > 0 ? groups.map(g => (
              <div key={g} className="space-y-3">
                <div className="text-xs text-ink-faint uppercase tracking-wider">{g}</div>
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
  const [selfRoles, setSelfRoles] = useState<string[]>([])
  const demoMode = useTaskStore(s => s.demoMode)

  const load = useCallback(() => {
    fetch('/api/users').then(r => r.json()).then(d => setUsers(d.users || [])).catch(() => setError('加载用户失败'))
  }, [])
  // 自助研究角色由 config.json → screen.research_roles 决定（B1）：只在管理员页动态出现，
  // 不把角色集合写死在前端，也不放开任意字符串。
  useEffect(() => {
    fetch('/api/screen/capabilities').then(r => r.json())
      .then(d => setSelfRoles(Array.isArray(d?.self_service_roles) ? d.self_service_roles : []))
      .catch(() => setSelfRoles([]))
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
    <section className="bg-surface border border-line rounded-xl p-6 space-y-4">
      <h2 className="flex items-center gap-2 text-sm text-ink font-medium">
        <Users className="w-4 h-4 text-brand" /> 用户（密码哈希仅服务端保存，不回显）
      </h2>
      {users.map(u => (
        <div key={u.username} className="flex items-center gap-3 border border-line rounded-lg px-4 py-3">
          <div className="flex-1 min-w-0">
            <div className="text-sm text-ink">
              {u.username}
              <span className={`ml-2 text-xs px-2 py-0.5 rounded ${u.role === 'admin' ? 'bg-brand/10 text-brand' : 'bg-line/40 text-ink-muted'}`}>{roleLabel(u.role)}</span>
            </div>
            <div className="text-xs text-ink-faint mt-1">
              创建于 {u.created_at ? formatLocalTimeLabel(u.created_at) : '未知（服务端未记录创建时间）'}
            </div>
          </div>
          <select value={u.role} disabled={demoMode}
            title={demoMode ? '演示模式下已停用（会真实改用户权限）' : '修改该用户角色'}
            onChange={e => act('POST', '/api/users', { username: u.username, role: e.target.value })}
            className="bg-surface-raised border border-line-strong rounded-lg px-2 py-1 text-xs text-ink disabled:opacity-40">
            <option value="admin">管理员</option>
            <option value="viewer">只读用户</option>
            {selfRoles.map(r => <option key={r} value={r}>{roleLabel(r)}</option>)}
          </select>
          <button onClick={() => {
            const pwd = window.prompt(`为 ${u.username} 设置新密码（至少 8 位）`)
            if (pwd) act('POST', '/api/users', { username: u.username, password: pwd })
          }} disabled={demoMode}
            title={demoMode ? '演示模式下已停用（会真实改密码）' : '设置新密码'}
            className="px-2.5 py-1.5 rounded-lg bg-surface-raised hover:bg-line text-ink text-xs disabled:opacity-40">
            <Key className="w-3 h-3 inline" /> 改密
          </button>
          <button onClick={() => act('DELETE', `/api/users/${encodeURIComponent(u.username)}`)} disabled={demoMode}
            title={demoMode ? '演示模式下已停用（会真实删除用户）' : '删除该用户'}
            className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-700 text-xs disabled:opacity-40">
            <Trash2 className="w-3 h-3" /> 删除
          </button>
        </div>
      ))}
      <div className="border-t border-line pt-4 grid grid-cols-1 md:grid-cols-4 gap-3">
        <input value={form.username} placeholder="用户名" onChange={e => setForm({ ...form, username: e.target.value })}
          className="bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink" />
        <input value={form.password} type="password" placeholder="密码（≥8 位）" onChange={e => setForm({ ...form, password: e.target.value })}
          className="bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink" />
        <select value={form.role} onChange={e => setForm({ ...form, role: e.target.value })}
          className="bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink">
          <option value="viewer">只读用户</option>
          <option value="admin">管理员</option>
          {selfRoles.map(r => <option key={r} value={r}>{roleLabel(r)}</option>)}
        </select>
        <button onClick={async () => {
          const ok = await act('POST', '/api/users', form)
          if (ok) setForm({ username: '', password: '', role: 'viewer' })
        }} disabled={demoMode || !form.username || form.password.length < 8}
          title={demoMode ? '演示模式下已停用（会真实创建用户）' : '创建用户'}
          className="flex items-center justify-center gap-1 px-4 py-2 rounded-lg bg-brand/15 hover:bg-brand/25 text-brand text-sm disabled:opacity-40">
          <Plus className="w-3.5 h-3.5" /> 新增用户
        </button>
      </div>
      {error && <div className="text-amber-700 text-xs">{error}</div>}
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
            className="bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink" />
          <input value={r.url || ''} disabled={disabled} placeholder="HTTP 端点（url）"
            onChange={e => setRow(i, { url: e.target.value })}
            className="bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink" />
          <div className="flex gap-2">
            <input value={r.command || ''} disabled={disabled} placeholder="或本地命令（command）"
              onChange={e => setRow(i, { command: e.target.value })}
              className="flex-1 bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink" />
            <button onClick={() => onChange(rows.filter((_, idx) => idx !== i))} disabled={disabled}
              className="px-2 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-700 text-xs disabled:opacity-40">
              <Trash2 className="w-3 h-3" />
            </button>
          </div>
        </div>
      ))}
      <button onClick={() => onChange([...rows, { name: '', url: '', command: '' }])} disabled={disabled}
        className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-surface-raised hover:bg-line text-ink text-xs disabled:opacity-40">
        <Plus className="w-3 h-3" /> 添加 MCP 服务
      </button>
      <div className="text-xs text-ink-faint">
        常见来源：Wind（商业授权）、同花顺 iFinD（付费）、SEC EDGAR（已内置适配器，无需 MCP）。
      </div>
    </div>
  )
}

// ── 数据源列表编辑（用户自加：HTTP JSON / MCP，密钥走本机密钥仓）────────────
// 密钥永不出进程：GET 只回 api_key_set 布尔；提交的 api_key 由后端转入
// credentials.json 并从 config.json 剔除（空值=保持不变，勾选清除=删除）。
const kvText = (o: any) =>
  Object.entries(o || {}).map(([k, v]) => `${k}=${v}`).join('\n')
const parseKv = (t: string) => {
  const out: Record<string, string> = {}
  t.split('\n').forEach(line => {
    const s = line.trim()
    if (!s) return
    const i = s.indexOf('=')
    if (i > 0) out[s.slice(0, i).trim()] = s.slice(i + 1).trim()
  })
  return out
}

function KvField({ label, value, onChange, disabled, placeholder }: {
  label: string; value: any; onChange: (v: any) => void; disabled?: boolean; placeholder?: string
}) {
  const [text, setText] = useState(kvText(value))
  return (
    <div className="space-y-1">
      <div className="text-xs text-ink-faint">{label}</div>
      <textarea value={text} disabled={disabled} rows={2} placeholder={placeholder}
        onChange={e => { setText(e.target.value); onChange(parseKv(e.target.value)) }}
        className="w-full bg-surface border border-line-strong rounded-lg px-3 py-2 text-xs font-mono text-ink focus:outline-none focus:border-brand/50 disabled:opacity-50" />
    </div>
  )
}

function DataSourcesEditor({ value, onChange, disabled, onTest, testing, resultOf }: {
  value: any[]
  onChange: (v: any[]) => void
  disabled?: boolean
  onTest: (target: string) => void
  testing: string
  resultOf: (target: string) => any
}) {
  const rows = value.filter(v => !(v && v._comment))
  const setRow = (idx: number, patch: any) => {
    onChange(rows.map((r, i) => i === idx ? { ...r, ...patch } : r))
  }
  const inputCls = 'bg-surface-raised border border-line-strong rounded-lg px-3 py-2 text-sm text-ink focus:outline-none focus:border-brand/50 disabled:opacity-50'
  return (
    <div className="space-y-3">
      <div className="text-xs text-ink-faint leading-relaxed border border-line rounded-lg px-3 py-2 bg-surface-raised/40">
        提示（只需读一次）：密钥保存在本机 <span className="font-mono">credentials.json</span>
        （0600 权限，不随配置进 git、不进日志；任何接口只回「已配置」，不回明文——留空=保持不变）。
        授权数据源（Wind/iFinD 等）返回的正文默认进入模型上下文用于研究——若该源许可禁止外发给
        第三方模型，把该源的「上下文」改为「仅派生结果」。
      </div>
      {rows.map((r, i) => {
        const sid = String(r.id || '')
        const target = sid ? `data_source:${sid}` : ''
        const res = target ? resultOf(target) : undefined
        return (
          <div key={i} className="border border-line rounded-lg p-3 space-y-2 bg-surface-raised/30">
            <div className="grid grid-cols-1 md:grid-cols-4 gap-2">
              <input value={sid} disabled={disabled} placeholder="id（如 wind，小写字母/数字/-/_）"
                onChange={e => setRow(i, { id: e.target.value })} className={inputCls} />
              <input value={r.label || ''} disabled={disabled} placeholder="名称（如 Wind 数据）"
                onChange={e => setRow(i, { label: e.target.value })} className={inputCls} />
              <select value={r.kind || 'http_json'} disabled={disabled}
                onChange={e => setRow(i, { kind: e.target.value })} className={inputCls}>
                <option value="http_json">HTTP JSON 接口</option>
                <option value="mcp">MCP 工具</option>
              </select>
              <label className="flex items-center gap-2 text-xs text-ink">
                <input type="checkbox" disabled={disabled} checked={r.enabled !== false}
                  onChange={e => setRow(i, { enabled: e.target.checked })} className="accent-brand" />
                {r.enabled !== false ? '启用' : '停用'}
              </label>
            </div>
            {r.kind === 'mcp' ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                <input value={r.tool || ''} disabled={disabled} placeholder="MCP 工具名（tool，如 wind_query）"
                  onChange={e => setRow(i, { tool: e.target.value })} className={inputCls} />
                <input value={r.server || ''} disabled={disabled} placeholder="所属 mcp_servers 服务名（可选）"
                  onChange={e => setRow(i, { server: e.target.value })} className={inputCls} />
              </div>
            ) : (
              <>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  <input value={r.base_url || ''} disabled={disabled}
                    placeholder="base_url（如 https://api.example.com/v1）"
                    onChange={e => setRow(i, { base_url: e.target.value })} className={inputCls} />
                  <div className="grid grid-cols-2 gap-2">
                    <select value={r.method || 'GET'} disabled={disabled}
                      onChange={e => setRow(i, { method: e.target.value })} className={inputCls}>
                      <option value="GET">GET</option>
                      <option value="POST">POST</option>
                    </select>
                    <input value={r.path || ''} disabled={disabled} placeholder="路径后缀（可选，如 quote）"
                      onChange={e => setRow(i, { path: e.target.value })} className={inputCls} />
                  </div>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                  <select value={r.auth || 'bearer'} disabled={disabled}
                    onChange={e => setRow(i, { auth: e.target.value })} className={inputCls}>
                    <option value="bearer">Bearer（Authorization 头）</option>
                    <option value="header">自定义头（X-API-Key 等）</option>
                    <option value="query">查询参数（api_key=…）</option>
                    <option value="none">无鉴权</option>
                  </select>
                  {r.auth === 'header' && (
                    <input value={r.auth_header || 'X-API-Key'} disabled={disabled}
                      placeholder="头名（如 X-API-Key）"
                      onChange={e => setRow(i, { auth_header: e.target.value })} className={inputCls} />
                  )}
                  {r.auth === 'query' && (
                    <input value={r.query_param || 'api_key'} disabled={disabled}
                      placeholder="参数名（如 api_key）"
                      onChange={e => setRow(i, { query_param: e.target.value })} className={inputCls} />
                  )}
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  <KvField label="固定查询参数（每行 参数=值）" value={r.params} disabled={disabled}
                    placeholder={'lang=cn\nfreq=annual'} onChange={v => setRow(i, { params: v })} />
                  <KvField label="额外请求头（每行 头=值，不放密钥）" value={r.headers} disabled={disabled}
                    placeholder={'X-Tenant=acme'} onChange={v => setRow(i, { headers: v })} />
                </div>
              </>
            )}
            <div className="grid grid-cols-1 md:grid-cols-4 gap-2">
              <input value={(r.markets || []).join(', ')} disabled={disabled}
                placeholder="市场（逗号分隔，如 cn, hk）"
                onChange={e => setRow(i, { markets: e.target.value.split(',').map((s: string) => s.trim()).filter(Boolean) })}
                className={inputCls} />
              <input value={(r.metrics || []).join(', ')} disabled={disabled}
                placeholder="指标（逗号分隔，如 营业收入, 净利润）"
                onChange={e => setRow(i, { metrics: e.target.value.split(',').map((s: string) => s.trim()).filter(Boolean) })}
                className={inputCls} />
              <select value={r.model_context || 'full'} disabled={disabled}
                onChange={e => setRow(i, { model_context: e.target.value })} className={inputCls}>
                <option value="full">上下文：全文可入</option>
                <option value="derived">上下文：仅派生结果</option>
              </select>
              <label className="flex items-center gap-2 text-xs text-ink" title="仅自建/本机网关勾选：允许 127.0.0.1、局域网地址">
                <input type="checkbox" disabled={disabled} checked={!!r.loopback}
                  onChange={e => setRow(i, { loopback: e.target.checked })} className="accent-brand" />
                内网服务
              </label>
            </div>
            <div className="flex items-center gap-3 flex-wrap">
              <input type="password" value={r.api_key || ''} disabled={disabled}
                placeholder={r.api_key_set ? '••••••••（已配置，留空保持不变）' : 'API 密钥（保存后转入密钥仓）'}
                onChange={e => setRow(i, { api_key: e.target.value, clear_key: false })}
                className={`flex-1 min-w-48 ${inputCls} font-mono`} />
              {r.api_key_set && (
                <label className="flex items-center gap-1.5 text-xs text-red-700">
                  <input type="checkbox" disabled={disabled} checked={!!r.clear_key}
                    onChange={e => setRow(i, { clear_key: e.target.checked, api_key: '' })}
                    className="accent-red-600" />
                  清除已存密钥
                </label>
              )}
              <button onClick={() => onTest(target)} disabled={disabled || !target || testing !== ''}
                title="对已保存的配置做一次真实探测（鉴权/状态码）；刚修改请先点右上角保存"
                className="ml-auto flex items-center gap-1 px-3 py-1.5 rounded-lg bg-brand/10 hover:bg-brand/20 text-brand text-xs disabled:opacity-40">
                <PlugZap className="w-3 h-3" /> {testing === target ? '测试中…' : '测试'}
              </button>
              <button onClick={() => onChange(rows.filter((_, idx) => idx !== i))} disabled={disabled}
                title="删除该数据源（登记的端点与密钥一并撤销）"
                className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-700 text-xs disabled:opacity-40">
                <Trash2 className="w-3 h-3" /> 删除
              </button>
            </div>
            {res && (
              <div className={`text-xs rounded-lg px-3 py-2 ${res.ok ? 'bg-emerald-500/10 text-emerald-700' : 'bg-red-500/10 text-red-700'}`}>
                {res.ok ? '连通正常' : '异常'}{res.reason ? ` · ${res.reason}` : ''}{res.message ? ` · ${res.message}` : ''}
                {res.error ? ` · ${res.error}` : ''}
              </div>
            )}
          </div>
        )
      })}
      <button onClick={() => onChange([...rows, { id: '', label: '', kind: 'http_json',
        base_url: '', auth: 'bearer', method: 'GET', enabled: true,
        model_context: 'full' }])}
        disabled={disabled || rows.length >= 20}
        title={rows.length >= 20 ? '最多 20 个数据源' : '添加数据源'}
        className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-surface-raised hover:bg-line text-ink text-xs disabled:opacity-40">
        <Plus className="w-3 h-3" /> 添加数据源
      </button>
    </div>
  )
}
