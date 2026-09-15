import { ReactNode, useState, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { NavLink, useLocation } from 'react-router-dom'
import { Settings } from 'lucide-react'
import { Play, Users, Clock, Activity, PanelLeftClose, PanelLeft, Brain, FlaskConical, Layers, LogOut, Shield, MoreHorizontal, X } from 'lucide-react'
import { useTaskStore } from '../stores/useTaskStore'
import { useVisibleInterval } from '../lib/useVisibleInterval'
import { useDemoRunner } from '../stores/useDemoRunner'
import { clearAuth, getAuthUser } from '../auth'
import ModeToggle from './ModeToggle'

const navItems = [
  { to: '/', icon: Play, label: '任务控制台' },
  { to: '/agents', icon: Users, label: '智能体' },
  { to: '/history', icon: Clock, label: '历史' },
  { to: '/health', icon: Activity, label: '健康' },
  { to: '/memory', icon: Brain, label: '记忆与进化' },
  { to: '/evals', icon: FlaskConical, label: '评测' },
  { to: '/skills', icon: Layers, label: 'Skill' },
  { to: '/metrics', icon: Activity, label: '指标' },
  { to: '/audit', icon: Shield, label: '审计' },
  { to: '/settings', icon: Settings, label: '设置' },
]

// 窄屏底栏 5 项：前 4 个高频页 + 「更多」。10 项平铺时每项约 37px，
// 且演示开关与退出登录此前在小屏完全不可达（都带 hidden sm:flex/md:flex）。
const MOBILE_PRIMARY = navItems.slice(0, 4)
const MOBILE_MORE = navItems.slice(4)

export default function AppLayout({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(window.innerWidth < 768)
  const [moreOpen, setMoreOpen] = useState(false)
  const demoMode = useTaskStore(s => s.demoMode)
  const connected = useTaskStore(s => s.connected)
  const systemStatus = useTaskStore(s => s.systemStatus)
  const agents = useTaskStore(s => s.agents)
  const currentTaskId = useTaskStore(s => s.currentTaskId)
  const planTree = useTaskStore(s => s.planTree)
  const logs = useTaskStore(s => s.logs)
  const report = useTaskStore(s => s.report)
  const status = useTaskStore(s => s.status)
  const liveTransport = useTaskStore(s => s.liveTransport)
  const toggleDemo = useTaskStore(s => s.toggleDemo)
  const fetchSystemStatus = useTaskStore(s => s.fetchSystemStatus)
  const user = getAuthUser()
  const location = useLocation()
  useDemoRunner()

  const logout = async () => {
    try {
      await fetch('/api/logout', { method: 'POST' })
    } catch { /* 服务不可达时也允许本地退出 */ }
    clearAuth()
  }

  useVisibleInterval(fetchSystemStatus, 3000)
  useEffect(() => {
    fetchSystemStatus()
    const onResize = () => { if (window.innerWidth < 768) setCollapsed(true) }
    window.addEventListener('resize', onResize)

    return () => { window.removeEventListener('resize', onResize) }
  }, [])

  // 窄屏抽屉在切页后自动收起（否则点完页面还盖着）
  useEffect(() => { setMoreOpen(false) }, [location.pathname])

  const titles: Record<string, string> = {
    '/': '任务控制台',
    '/agents': '智能体团队',
    '/history': '历史任务',
    '/health': '系统健康',
    '/memory': '记忆与进化',
    '/evals': '评测看板',
    '/skills': 'Skill 管理',
    '/metrics': '指标看板',
    '/audit': '审计日志',
    '/settings': '设置',
  }

  // 连接状态三态：首帧还没拿到过 /api/status 时不能说"离线"——那是"连接中"，
  // 否则用户刚打开页面就看到"离线"，会以为服务没起来（实测：约 3 秒后才变"在线"）。
  const connState: 'connecting' | 'online' | 'offline' =
    connected ? 'online' : (systemStatus === null ? 'connecting' : 'offline')
  const connLabel = connState === 'online' ? '在线' : connState === 'connecting' ? '连接中' : '离线'
  const connDot = connState === 'online' ? 'bg-emerald-400 animate-pulse'
    : connState === 'connecting' ? 'bg-amber-400 animate-pulse' : 'bg-red-400'

  const onToggleDemo = () => {
    if (demoMode) { toggleDemo(); return }
    if (!window.confirm('进入演示模式后，界面展示内置演示数据、不调用真实接口（提交与快答都会被停用）。确认进入？')) return
    toggleDemo()
  }

  return (
    <div className="flex h-screen overflow-hidden console-layout">
      {/* Mobile bottom nav：4 个高频页 + 更多（抽屉内含其余页面、演示开关、退出登录） */}
      <nav className="md:hidden fixed inset-x-0 bottom-0 z-30 flex bg-slate-900 border-t border-slate-800 h-14">
        {MOBILE_PRIMARY.map(({ to, icon: Icon, label }) => (
          <NavLink key={to} to={to}
            className={({ isActive }) =>
              `flex-1 flex flex-col items-center justify-center gap-0.5 text-xs transition-opacity duration-200 ${
                isActive ? 'text-cyan-400 opacity-100' : 'text-slate-500 opacity-70'
              }`}>
            <Icon className="w-5 h-5" />{label}
          </NavLink>
        ))}
        <button onClick={() => setMoreOpen(true)}
          className={`flex-1 flex flex-col items-center justify-center gap-0.5 text-xs ${
            moreOpen ? 'text-cyan-400' : 'text-slate-500 opacity-70'}`}>
          <MoreHorizontal className="w-5 h-5" />更多{demoMode ? ' · 演示' : ''}
        </button>
      </nav>

      {/* 窄屏「更多」抽屉：挂到 body，避免被带 transform 的祖先改变 fixed 参照 */}
      {moreOpen && createPortal(
        <div className="fixed inset-0 z-50 flex items-end bg-black/60 md:hidden"
          onClick={() => setMoreOpen(false)}>
          <div className="w-full max-h-[80vh] overflow-y-auto rounded-t-2xl border-t border-slate-700 bg-slate-900 p-4"
            onClick={e => e.stopPropagation()}>
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2 text-sm text-slate-300">
                <MoreHorizontal className="w-4 h-4 text-cyan-400" /> 更多
              </div>
              <button onClick={() => setMoreOpen(false)} className="text-slate-500 hover:text-slate-300">
                <X className="w-4 h-4" />
              </button>
            </div>
            {user && (
              <div className="mb-3 flex items-center justify-between rounded-lg border border-slate-800 bg-slate-800/30 px-3 py-2">
                <span className="text-xs text-slate-400">
                  {user.username} · {user.role === 'admin' ? '管理员' : '只读'}
                </span>
                <button onClick={logout}
                  className="flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-slate-300 hover:bg-slate-800">
                  <LogOut className="w-3.5 h-3.5" /> 退出登录
                </button>
              </div>
            )}
            <button onClick={onToggleDemo}
              className={`mb-3 flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
                demoMode ? 'border-purple-500/30 bg-purple-500/10 text-purple-400' : 'border-slate-700 text-slate-400'}`}>
              <FlaskConical className="w-4 h-4" /> 演示模式 {demoMode ? 'ON（点此关闭）' : 'OFF（点此开启）'}
            </button>
            <div className="grid grid-cols-2 gap-2">
              {MOBILE_MORE.map(({ to, icon: Icon, label }) => (
                <NavLink key={to} to={to} onClick={() => setMoreOpen(false)}
                  className={({ isActive }) =>
                    `flex items-center gap-2 rounded-lg px-3 py-2.5 text-sm ${
                      isActive ? 'bg-slate-800 text-cyan-400' : 'text-slate-400 hover:bg-slate-800/50'
                    }`}>
                  <Icon className="w-4 h-4 shrink-0" />{label}
                </NavLink>
              ))}
            </div>
          </div>
        </div>,
        document.body,
      )}

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
          <button onClick={onToggleDemo} title={demoMode ? '退出演示模式' : '进入演示模式（用内置数据，不调用真实接口）'} className={`mx-3 mb-1 flex items-center gap-2 px-3 py-2 rounded-lg text-sm border transition-opacity duration-200 opacity-80 hover:opacity-100
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
            {/* LLM 运行模式切换：全商业 API / 本地 LoRA 混合 */}
            <ModeToggle compact />
            {demoMode && (
              <span className="hidden sm:inline px-2.5 py-0.5 rounded-full text-xs font-semibold bg-purple-500/20 text-purple-400">演示</span>
            )}
            <div className="flex items-center gap-2"
              title={connState === 'connecting' ? '正在连接后端…'
                : connState === 'offline' ? '后端不可达，正在自动重试' : '后端连接正常'}>
              <span className={`w-2 h-2 rounded-full ${connDot}`} />
              <span className="hidden sm:inline text-slate-400">{connLabel}</span>
            </div>
            <div className="hidden sm:block text-slate-400">
              智能体: <span className="text-slate-200 font-mono">{agents.length}</span>
            </div>
            {user && (
              <div className="hidden sm:flex items-center gap-2 text-xs text-slate-400">
                <span className="px-2 py-0.5 rounded-full bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
                  {user.username} · {user.role === 'admin' ? '管理员' : '只读'}
                </span>
                <button
                  onClick={logout}
                  className="flex items-center gap-1 px-2 py-1 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-slate-200 transition-colors"
                  title="退出登录"
                >
                  <LogOut className="w-3.5 h-3.5" /> 退出
                </button>
              </div>
            )}
          </div>
        </header>
        {demoMode && (
          <div className="shrink-0 bg-violet-500/10 border-b border-violet-500/30 text-violet-300 text-xs px-4 md:px-6 py-2">
            演示模式：<span className="font-medium">控制台展示内置演示数据</span>，其余页面读的是真实环境（只读接口仍访问真实服务）；
            写操作与付费操作（提交任务、快答、设置保存、记忆删除、触发演化等）已被拦截，不会真的改数据或花钱。
            点左侧栏的「演示 ON」退出（窄屏在底栏「更多」里）。
          </div>
        )}
        {liveTransport === 'polling' && !demoMode && (
          <div className="shrink-0 bg-amber-500/10 border-b border-amber-500/30 text-amber-300 text-xs px-4 md:px-6 py-2">
            实时通道已降级为轮询（每 2 秒拉一次）：进度仍会更新，但延迟比推送高；
            通常是浏览器/代理不支持 SSE 或连接被中断，任务本身不受影响。
          </div>
        )}
        <main className="flex-1 overflow-auto p-4 md:p-6 mobile-scroll">
          {children}
        </main>
      </div>
    
      {/* Debug Panel (activated with ?debug=true) */}
      {new URLSearchParams(window.location.search).has('debug') && (
        <div className="fixed top-16 right-4 z-50 bg-slate-950/95 border border-cyan-500/30 rounded-lg p-4 text-xs font-mono text-slate-400 max-w-sm max-h-[80vh] overflow-auto opacity-90">
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
