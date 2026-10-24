import { FormEvent, useEffect, useState } from 'react'
import { LogIn, ShieldCheck, Lock } from 'lucide-react'
import { setAuth, type AuthUser } from '../auth'
import { BrandMark, WeaveLines } from './Brand'

type Mode = 'login' | 'setup'

export default function Login() {
  const [mode, setMode] = useState<Mode>('login')
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // 首次访问且系统还没有用户时，直接展示“创建初始管理员”表单
  useEffect(() => {
    fetch('/api/auth/bootstrap')
      .then(r => r.json())
      .then(d => { if (d?.setup_required) setMode('setup') })
      .catch(() => {})
  }, [])

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setError('')
    if (mode === 'setup' && password.length < 8) {
      setError('密码至少 8 位')
      return
    }
    if (mode === 'setup' && password !== confirm) {
      setError('两次输入的密码不一致')
      return
    }
    setLoading(true)
    try {
      const path = mode === 'login' ? '/api/login' : '/api/setup-admin'
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      const d = await res.json()
      if (!res.ok) {
        if (mode === 'login' && d?.setup_required) {
          setMode('setup')
          setError('系统尚未初始化管理员，请先创建初始管理员')
        } else {
          setError(d?.error || '请求失败，请稍后重试')
        }
        return
      }
      // 角色原样保存：研究/复核等角色不归到 admin（B1 真机反例：researcher 被显示成「管理员」）
      const user: AuthUser = { username: d.user, role: String(d.role || 'viewer') }
      setAuth(user) // 会话由后端 session Cookie 承载，前端仅保存展示用用户名/角色
    } catch {
      setError('网络异常，请确认服务已启动')
    } finally {
      setLoading(false)
    }
  }

  const isSetup = mode === 'setup'

  return (
    <div className="grid min-h-[100dvh] lg:grid-cols-[1.15fr_1fr]">
      <section className="relative isolate hidden min-h-screen overflow-hidden border-r border-line bg-[#f1eee5] p-12 lg:flex lg:flex-col xl:p-16">
        <WeaveLines className="pointer-events-none absolute -bottom-10 -right-44 -z-10 h-[500px] w-[1050px] text-brand" />
        <div className="flex items-center gap-3 text-brand"><BrandMark className="h-11 w-11" /><div className="editorial-title text-3xl tracking-[.15em] text-ink">织光 <span className="font-sans text-sm tracking-normal text-ink-muted">WeaveMind</span></div></div>
        <div className="my-auto py-20">
          <p className="eyebrow mb-6 text-brand">INDEPENDENT RESEARCH, TRACEABLE INSIGHT</p>
          <h1 className="editorial-title text-5xl leading-[1.5] tracking-wide">让研究有底稿，<br />让判断有来处。</h1>
          <p className="mt-8 max-w-md text-base leading-loose text-ink-muted">为个人投资者与小团队打造的上市公司研究工作台。将公开披露，织成可复核的研究。</p>
          <div className="mt-10 flex gap-8 text-xs text-brand-strong"><span>研究正文</span><span>可复算底稿</span><span>来源与证据包</span></div>
        </div>
        <p className="text-xs text-ink-faint">研究试用阶段 · 不构成投资建议</p>
      </section>
      <section className="flex items-center justify-center bg-surface px-6 py-12 sm:px-12">
        <div className="w-full max-w-sm">
          <div className="mb-10 flex items-center gap-3 text-brand lg:hidden"><BrandMark className="h-10 w-10" /><span className="editorial-title text-2xl text-ink">织光 WeaveMind</span></div>
          <p className="eyebrow text-brand">YOUR RESEARCH DESK</p>
          <h2 className="editorial-title mt-3 text-3xl text-ink">{isSetup ? '建立你的研究工作台' : '欢迎回到织光'}</h2>
          <p className="mb-8 mt-3 text-sm text-ink-muted">{isSetup ? '创建初始管理员，仅首次需要。' : '登录工作区，继续你的独立研究。'}</p>
          <form onSubmit={submit} className="space-y-5">
            <label className="research-field">用户名
              <input value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" placeholder="请输入用户名" required />
            </label>
            <label className="research-field">密码
              <input type="password" value={password} onChange={event => setPassword(event.target.value)}
                autoComplete={isSetup ? 'new-password' : 'current-password'} placeholder={isSetup ? '至少 8 位' : '请输入密码'} required />
            </label>
            {isSetup && <label className="research-field">确认密码
              <input type="password" value={confirm} onChange={event => setConfirm(event.target.value)} autoComplete="new-password" placeholder="再次输入密码" required />
            </label>}
            {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-state-failure">{error}</div>}
            <button type="submit" disabled={loading} className="button-primary flex w-full items-center justify-center gap-2 rounded-lg px-4 py-3 text-sm font-medium">
              {isSetup ? <ShieldCheck className="h-4 w-4" /> : <LogIn className="h-4 w-4" />}
              {loading ? '请稍候…' : isSetup ? '创建管理员并进入' : '进入研究工作台'}
            </button>
            {!isSetup && <button type="button" onClick={() => setMode('setup')} className="flex w-full items-center justify-center gap-2 py-2 text-xs text-ink-muted hover:text-brand">
              <Lock className="h-3.5 w-3.5" />首次使用？创建初始管理员
            </button>}
          </form>
          <p className="mt-10 border-t border-line pt-5 text-xs leading-relaxed text-ink-faint">本地部署不等于离线运行。研究会使用你配置的模型与数据服务，密钥请在设置中管理。</p>
        </div>
      </section>
    </div>
  )
}
