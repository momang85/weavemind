import { Component, useEffect, useState, type ReactNode } from 'react'
import { Routes, Route } from 'react-router-dom'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import AppLayout from './components/AppLayout'
import Login from './components/Login'
import TaskConsole from './pages/TaskConsole'
import AgentsPage from './pages/Agents'
import History from './pages/History'
import HealthPage from './pages/Health'
import SettingsPage from './pages/Settings'
import MemoryPage from './pages/Memory'
import EvalsPage from './pages/Evals'
import SkillsPage from './pages/Skills'
import { useTaskStore } from './stores/useTaskStore'
import { installAuthFetch, isAuthed } from './auth'

// 全局 fetch 包装：自动附加 Authorization 头；数据接口 401 时回到登录页
installAuthFetch()

// ── Error Boundary ──
class ErrorBoundary extends Component<
  { children: ReactNode; fallback?: ReactNode },
  { hasError: boolean; error: Error | null }
> {
  state = { hasError: false, error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error }
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-slate-950 flex items-center justify-center p-8">
          <div className="bg-slate-900 border border-red-500/20 rounded-2xl p-8 max-w-md text-center space-y-4">
            <div className="w-16 h-16 rounded-full bg-red-500/10 flex items-center justify-center mx-auto">
              <AlertTriangle className="w-8 h-8 text-red-400" />
            </div>
            <h2 className="text-slate-200 text-lg font-semibold">出错了</h2>
            <p className="text-slate-500 text-sm">
              {this.state.error?.message || '发生渲染异常'}
            </p>
            <button
              onClick={() => { this.setState({ hasError: false, error: null }); window.location.reload() }}
              className="inline-flex items-center gap-2 px-5 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors"
            >
              <RefreshCw className="w-4 h-4" /> 重新加载
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

// ── App Root ──
export default function App() {
  const { toggleDemo, fetchSystemStatus } = useTaskStore()
  const isDemo = () => new URLSearchParams(window.location.search).has('demo')
  const [authed, setAuthed] = useState(() => isAuthed() || isDemo())

  // 登录/登出/会话失效后同步登录态
  useEffect(() => {
    const sync = () => setAuthed(isAuthed() || isDemo())
    window.addEventListener('weavemind:auth-changed', sync)
    return () => window.removeEventListener('weavemind:auth-changed', sync)
  }, [])

  // Initialize: check URL for demo mode, start polling
  useEffect(() => {
    if (!authed) return
    if (isDemo()) toggleDemo(true)
    fetchSystemStatus()
  }, [authed])

  if (!authed) {
    return <Login />
  }

  return (
    <ErrorBoundary>
      <AppLayout>
        <Routes>
          <Route path="/" element={<TaskConsole />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/history" element={<History />} />
          <Route path="/health" element={<HealthPage />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/evals" element={<EvalsPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </AppLayout>
    </ErrorBoundary>
  )
}
