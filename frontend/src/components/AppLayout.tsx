import { ReactNode, useState, useEffect } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { Settings } from 'lucide-react'
import { Play, Users, Clock, Activity, PanelLeftClose, PanelLeft, Brain, FlaskConical } from 'lucide-react'
import { useTaskStore } from '../stores/useTaskStore'
import { useDemoRunner } from '../stores/useDemoRunner'

const navItems = [
  { to: '/', icon: Play, label: '任务控制台' },
  { to: '/agents', icon: Users, label: '智能体' },
  { to: '/history', icon: Clock, label: '历史' },
  { to: '/health', icon: Activity, label: '健康' },
  { to: '/memory', icon: Brain, label: '记忆与进化' },
  { to: '/settings', icon: Settings, label: '设置' },
]

export default function AppLayout({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(window.innerWidth < 768)
  const { demoMode, connected, toggleDemo, fetchSystemStatus, agents, currentTaskId, planTree, logs, report, status } = useTaskStore()
  const location = useLocation()
  useDemoRunner()

  useEffect(() => {
    fetchSystemStatus()
    const t = setInterval(fetchSystemStatus, 3000)
    const onResize = () => { if (window.innerWidth < 768) setCollapsed(true) }
    window.addEventListener('resize', onResize)

    return () => { clearInterval(t); window.removeEventListener('resize', onResize) }
  }, [])

  const titles: Record<string, string> = {
    '/': '任务控制台',
    '/agents': '智能体团队',
    '/history': '历史任务',
    '/health': '系统健康',
    '/memory': '记忆与进化',
  }

  return (
    <div className="flex h-screen overflow-hidden console-layout">
      {/* Mobile bottom nav */}
      <nav className="md:hidden fixed inset-x-0 bottom-0 z-20 flex bg-slate-900 border-t border-slate-800 h-14">
        {navItems.map(({ to, icon: Icon, label }) => (
          <NavLink key={to} to={to}
            className={({ isActive }) =>
              `flex-1 flex flex-col items-center justify-center gap-0.5 text-[10px] transition-opacity duration-200 ${
                isActive ? 'text-cyan-400 opacity-100' : 'text-slate-500 opacity-70'
              }`}>
            <Icon className="w-5 h-5" />{label}
          </NavLink>
        ))}
      </nav>

      {/* Desktop sidebar */}
      <aside className={`hidden md:flex flex-col ${collapsed ? 'w-16' : 'w-60'} bg-slate-900 border-r border-slate-800 transition-all duration-200 shrink-0`}>
        <div className="flex items-center gap-3 px-4 h-16 border-b border-slate-800">
          <Brain className="w-6 h-6 text-cyan-400 shrink-0" />
          {!collapsed && <span className="text-cyan-400 font-bold text-lg tracking-wide">织光 WeaveMind</span>}
        </div>
        <nav className="flex-1 p-3 space-y-1">
          {navItems.map(({ to, icon: Icon, label }) => (
            <NavLink key={to} to={to}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-opacity duration-200 ${
                  isActive ? 'bg-slate-800 text-cyan-400 opacity-100' : 'text-slate-400 opacity-80 hover:opacity-100 hover:bg-slate-800/50'
                }`}>
              <Icon className="w-5 h-5 shrink-0" />{!collapsed && <span>{label}</span>}
            </NavLink>
          ))}
        </nav>
        {!collapsed && (
          <button onClick={() => toggleDemo()} className={`mx-3 mb-1 flex items-center gap-2 px-3 py-2 rounded-lg text-sm border transition-opacity duration-200 opacity-80 hover:opacity-100
            ${demoMode ? 'bg-purple-500/10 border-purple-500/30 text-purple-400' : 'border-slate-700 text-slate-500'}`}>
            <FlaskConical className="w-4 h-4" /> 演示 {demoMode ? 'ON' : 'OFF'}
          </button>
        )}
        <button onClick={() => setCollapsed(!collapsed)}
          className="hidden md:flex items-center gap-2 px-4 py-3 text-slate-500 hover:text-slate-300 border-t border-slate-800 text-sm transition-opacity duration-200 opacity-60 hover:opacity-100">
          {collapsed ? <PanelLeft className="w-4 h-4" /> : <PanelLeftClose className="w-4 h-4" />}
          {!collapsed && <span>收起侧栏</span>}
        </button>
      </aside>

      {/* Main */}
      <div className="flex-1 flex flex-col min-w-0 md:ml-0 main-mobile">
        <header className="flex items-center justify-between h-16 px-6 bg-slate-900 border-b border-slate-800 shrink-0">
          <h1 className="text-slate-200 font-semibold text-lg">
            {titles[location.pathname] || location.pathname.slice(1)}
          </h1>
          <div className="flex items-center gap-5 text-sm">
            {demoMode && (
              <span className="hidden sm:inline px-2.5 py-0.5 rounded-full text-xs font-semibold bg-purple-500/20 text-purple-400">演示</span>
            )}
            <div className="flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full ${connected ? 'bg-green-400 animate-pulse' : 'bg-red-400'}`} />
              <span className="hidden sm:inline text-slate-400">{connected ? '在线' : '离线'}</span>
            </div>
            <div className="hidden sm:block text-slate-400">
              智能体: <span className="text-slate-200 font-mono">{agents.length}</span>
            </div>
          </div>
        </header>
        <main className="flex-1 overflow-auto p-4 md:p-6 mobile-scroll">
          {children}
        </main>
      </div>
    
      {/* Debug Panel (activated with ?debug=true) */}
      {new URLSearchParams(window.location.search).has('debug') && (
        <div className="fixed top-16 right-4 z-50 bg-slate-950/95 border border-cyan-500/30 rounded-lg p-4 text-[10px] font-mono text-slate-400 max-w-sm max-h-[80vh] overflow-auto opacity-90">
          <div className="text-cyan-400 text-xs mb-2 font-bold">Debug Panel</div>
          <div className="space-y-2">
            <div><span className="text-yellow-400">connected:</span> {String(connected)}</div>
            <div><span className="text-yellow-400">status:</span> {status}</div>
            <div><span className="text-yellow-400">taskId:</span> {currentTaskId || 'none'}</div>
            <div><span className="text-yellow-400">planTree:</span> {planTree ? planTree.children?.length + ' steps' : 'null'}</div>
            <div><span className="text-yellow-400">agents:</span> {agents.length}</div>
            <div><span className="text-yellow-400">logs:</span> {logs.length}</div>
            <div><span className="text-yellow-400">report:</span> {report ? report.summary : 'null'}</div>
            {agents.map(a => (
              <div key={a.agent_id} className="pl-2">{a.agent_id}: {a.status}</div>
            ))}
          </div>
        </div>
      )}

</div>
  )
}
