import { Component, Suspense, lazy, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Routes, Route } from 'react-router-dom'
import { AlertTriangle, Loader2, RefreshCw } from 'lucide-react'
import AppLayout from './components/AppLayout'
import Login from './components/Login'
import { useTaskStore } from './stores/useTaskStore'
import { installAuthFetch, isAuthed, verifySession } from './auth'

// 路由级代码分割：重页面独立 chunk，主包不承载 markdown/syntax-highlighter 重依赖
const TaskConsole = lazy(() => import('./pages/TaskConsole'))
const AgentsPage = lazy(() => import('./pages/Agents'))
const History = lazy(() => import('./pages/History'))
const HealthPage = lazy(() => import('./pages/Health'))
const SettingsPage = lazy(() => import('./pages/Settings'))
const AuditPage = lazy(() => import('./pages/AuditPage'))
const MetricsPage = lazy(() => import('./pages/MetricsPage'))
const MemoryPage = lazy(() => import('./pages/Memory'))
const EvalsPage = lazy(() => import('./pages/Evals'))
const SkillsPage = lazy(() => import('./pages/Skills'))

function PageFallback() {
  return (
    <div className="flex items-center justify-center py-20 text-slate-500 text-sm">
      <Loader2 className="w-4 h-4 animate-spin mr-2" /> 页面加载中…
    </div>
  )
}

// 全局 fetch 包装：会话凭据由浏览器自动携带的 session Cookie 完成，无需前端注入。
// 数据接口 401 时回到登录页。
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
  const toggleDemo = useTaskStore(s => s.toggleDemo)
  const fetchSystemStatus = useTaskStore(s => s.fetchSystemStatus)
  const isDemo = () => new URLSearchParams(window.location.search).has('demo')
  const [authed, setAuthed] = useState(() => isAuthed() || isDemo())

  // 登录/登出/会话失效后同步登录态
  useEffect(() => {
    const sync = () => setAuthed(isAuthed() || isDemo())
    window.addEventListener('weavemind:auth-changed', sync)
    return () => window.removeEventListener('weavemind:auth-changed', sync)
  }, [])

  // 刷新后与服务端核对会话 Cookie 是否仍有效：本地缓存存在但会话已过期/清空时退回登录页。
  useEffect(() => {
    if (isDemo()) return
    verifySession().then(valid => {
      if (!valid) setAuthed(false)
    })
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

  const content = useMemo(() => (
    <AppLayout>
      <Suspense fallback={<PageFallback />}>
        <Routes>
          <Route path="/" element={<TaskConsole />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/history" element={<History />} />
          <Route path="/health" element={<HealthPage />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/evals" element={<EvalsPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/metrics" element={<MetricsPage />} />
        </Routes>
      </Suspense>
    </AppLayout>
  ), [])

  return <ErrorBoundary>{content}</ErrorBoundary>
}
