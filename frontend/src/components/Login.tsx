import { FormEvent, useEffect, useState } from 'react'
import { Brain, LogIn, ShieldCheck, Lock } from 'lucide-react'
import { setAuth, type AuthUser } from '../auth'

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
      const user: AuthUser = { username: d.user, role: d.role === 'viewer' ? 'viewer' : 'admin' }
      setAuth(d.token, user)
    } catch {
      setError('网络异常，请确认服务已启动')
    } finally {
      setLoading(false)
    }
  }

  const isSetup = mode === 'setup'

  return (
    <div className="min-h-screen bg-slate-950 flex items-center justify-center p-6">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <div className="inline-flex items-center gap-3 mb-3">
            <div className="w-12 h-12 rounded-2xl bg-cyan-500/10 border border-cyan-500/30 flex items-center justify-center">
              <Brain className="w-6 h-6 text-cyan-400" />
            </div>
          </div>
          <h1 className="text-2xl font-bold text-slate-100">织光 WeaveMind</h1>
          <p className="text-sm text-slate-500 mt-1">
            {isSetup ? '创建初始管理员（仅首次）' : '登录后开始使用'}
          </p>
        </div>

        <form onSubmit={submit} className="bg-slate-900 border border-slate-800 rounded-2xl p-8 space-y-4 shadow-xl">
          <div>
            <label className="block text-xs text-slate-400 mb-1.5">用户名</label>
            <input
              value={username}
              onChange={e => setUsername(e.target.value)}
              autoComplete="username"
              className="w-full px-3.5 py-2.5 rounded-lg bg-slate-950 border border-slate-700 text-slate-200 text-sm outline-none focus:border-cyan-500/60"
              placeholder="admin"
            />
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1.5">密码</label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              autoComplete={isSetup ? 'new-password' : 'current-password'}
              className="w-full px-3.5 py-2.5 rounded-lg bg-slate-950 border border-slate-700 text-slate-200 text-sm outline-none focus:border-cyan-500/60"
              placeholder={isSetup ? '至少 8 位' : '••••••••'}
            />
          </div>
          {isSetup && (
            <div>
              <label className="block text-xs text-slate-400 mb-1.5">确认密码</label>
              <input
                type="password"
                value={confirm}
                onChange={e => setConfirm(e.target.value)}
                autoComplete="new-password"
                className="w-full px-3.5 py-2.5 rounded-lg bg-slate-950 border border-slate-700 text-slate-200 text-sm outline-none focus:border-cyan-500/60"
                placeholder="再次输入密码"
              />
            </div>
          )}

          {error && (
            <div className="text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg bg-cyan-500/90 hover:bg-cyan-500 text-slate-950 text-sm font-semibold transition-colors disabled:opacity-50"
          >
            {isSetup ? <ShieldCheck className="w-4 h-4" /> : <LogIn className="w-4 h-4" />}
            {loading ? '请稍候…' : isSetup ? '创建管理员并进入' : '登录'}
          </button>

          {!isSetup && (
            <button
              type="button"
              onClick={() => setMode('setup')}
              className="w-full flex items-center justify-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors"
            >
              <Lock className="w-3 h-3" /> 尚未初始化？切换到创建管理员
            </button>
          )}
        </form>
      </div>
    </div>
  )
}
