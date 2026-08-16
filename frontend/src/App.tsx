import { Component, useEffect, type ReactNode } from 'react'
import { Routes, Route } from 'react-router-dom'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import AppLayout from './components/AppLayout'
import TaskConsole from './pages/TaskConsole'
import AgentsPage from './pages/Agents'
import History from './pages/History'
import HealthPage from './pages/Health'
import SettingsPage from './pages/Settings'
import MemoryPage from './pages/Memory'
import EvalsPage from './pages/Evals'
import SkillsPage from './pages/Skills'
import { useTaskStore } from './stores/useTaskStore'

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

  // Initialize: check URL for demo mode, start polling
  useEffect(() => {
    const isDemo = new URLSearchParams(window.location.search).has('demo')
    if (isDemo) toggleDemo(true)

    fetchSystemStatus()
  }, [])

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
