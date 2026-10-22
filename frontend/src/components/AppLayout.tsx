import { ReactNode, useState, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { NavLink, useLocation } from 'react-router-dom'
import { Settings, Users, Clock, Activity, PanelLeftClose, PanelLeft, Brain, FlaskConical, Layers, LogOut, Shield, MoreHorizontal, X, Microscope, BookOpen, ChevronDown, ArrowUpRight } from 'lucide-react'
import { useTaskStore } from '../stores/useTaskStore'
import { useVisibleInterval } from '../lib/useVisibleInterval'
import { useDemoRunner } from '../stores/useDemoRunner'
import { clearAuth, getAuthUser } from '../auth'
import ModeToggle from './ModeToggle'
import { BrandMark } from './Brand'

const researchNav = [
  { to: '/', icon: BookOpen, label: '研究工作台', subtitle: '从公开资料，到可复核的研究' },
  { to: '/history', icon: Clock, label: '研究档案', subtitle: '找回研究、继续对话与查看交付' },
  { to: '/research-study', icon: Microscope, label: '研究试验', subtitle: '在本地验证研究方法与结果' },
]
const systemNav = [
  { to: '/agents', icon: Users, label: '研究团队', subtitle: '查看智能体分工与执行状态' },
  { to: '/health', icon: Activity, label: '服务健康', subtitle: '检查依赖、连接与可用能力' },
  { to: '/memory', icon: Brain, label: '记忆与进化', subtitle: '管理经验与演化记录' },
  { to: '/evals', icon: FlaskConical, label: '质量评测', subtitle: '查看评测样本、结果与缺口' },
  { to: '/skills', icon: Layers, label: '研究技能', subtitle: '管理可复用的研究能力' },
  { to: '/metrics', icon: Activity, label: '运行指标', subtitle: '观察调用、耗时与运行表现' },
  { to: '/audit', icon: Shield, label: '审计记录', subtitle: '追溯系统操作与权限记录' },
]
const settingsNav = { to: '/settings', icon: Settings, label: '设置', subtitle: '模型服务、账户与工作区配置' }
const MOBILE_PRIMARY = [...researchNav, settingsNav]
const MOBILE_MORE = systemNav
const navItems = [...researchNav, ...systemNav, settingsNav]

export default function AppLayout({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(window.innerWidth < 768)
  const [moreOpen, setMoreOpen] = useState(false)
  const demoMode = useTaskStore(store => store.demoMode)
  const connected = useTaskStore(store => store.connected)
  const systemStatus = useTaskStore(store => store.systemStatus)
  const agents = useTaskStore(store => store.agents)
  const currentTaskId = useTaskStore(store => store.currentTaskId)
  const status = useTaskStore(store => store.status)
  const liveTransport = useTaskStore(store => store.liveTransport)
  const toggleDemo = useTaskStore(store => store.toggleDemo)
  const fetchSystemStatus = useTaskStore(store => store.fetchSystemStatus)
  const user = getAuthUser()
  const location = useLocation()
  const drawerRef = useRef<HTMLDivElement>(null)
  const moreButtonRef = useRef<HTMLButtonElement>(null)
  const [systemOpen, setSystemOpen] = useState(systemNav.some(item => item.to === location.pathname))
  useDemoRunner()

  const logout = async () => {
    try { await fetch('/api/logout', { method: 'POST' }) } catch {}
    clearAuth()
  }

  useVisibleInterval(fetchSystemStatus, 3000)
  useEffect(() => {
    fetchSystemStatus()
    const onResize = () => {
      if (window.innerWidth < 768) setCollapsed(true)
      else setMoreOpen(false)
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [fetchSystemStatus])

  useEffect(() => {
    setMoreOpen(false)
    if (systemNav.some(item => item.to === location.pathname)) setSystemOpen(true)
  }, [location.pathname])

  useEffect(() => {
    if (!moreOpen) return
    drawerRef.current?.querySelector<HTMLButtonElement>('button')?.focus()
    return () => moreButtonRef.current?.focus()
  }, [moreOpen])

  const connState = connected ? 'online' : systemStatus === null ? 'connecting' : 'offline'
  const connLabel = connState === 'online' ? '服务在线' : connState === 'connecting' ? '正在连接' : '服务离线'
  const connDot = connState === 'online' ? 'bg-state-success' : connState === 'connecting' ? 'bg-state-issue' : 'bg-state-failure'
  const currentPage = navItems.find(item => item.to === location.pathname)
  const onToggleDemo = () => {
    if (demoMode) { toggleDemo(); return }
    if (window.confirm('进入演示模式后，控制台展示内置演示数据；提交、快答等写入及付费操作将停用。确认进入？')) toggleDemo()
  }

  const renderLink = ({ to, icon: Icon, label }: typeof settingsNav) => (
    <NavLink key={to} to={to} end={to === '/'} title={collapsed ? label : undefined}
      className={({ isActive }) => `app-nav-link relative flex items-center gap-3 rounded-lg px-3 py-3 text-sm transition-colors ${isActive ? 'is-active font-medium' : 'text-ink-muted hover:bg-surface-raised'} ${collapsed ? 'justify-center' : ''}`}>
      <Icon className="h-[18px] w-[18px] shrink-0" strokeWidth={1.6} />
      <span className={collapsed ? 'sr-only' : ''}>{label}</span>
    </NavLink>
  )

  return (
    <div className="console-layout flex h-[100dvh] overflow-hidden">
      <a href="#workspace-content" className="skip-link">跳到主要内容</a>
      <aside aria-label="主导航" className={`app-sidebar hidden shrink-0 flex-col border-r border-line transition-[width] duration-200 md:flex ${collapsed ? 'w-[76px]' : 'w-[240px]'}`}>
        <NavLink to="/" className="flex h-24 shrink-0 items-center gap-3 px-5 text-brand" aria-label="织光 WeaveMind 首页">
          <BrandMark className="h-9 w-9 shrink-0" />
          {!collapsed && <div><div className="editorial-title text-[26px] leading-tight tracking-[.15em] text-ink">织光</div><div className="mt-1 text-xs tracking-[.1em] text-ink-muted">WeaveMind</div></div>}
        </NavLink>
        <div className="flex-1 overflow-y-auto px-3 pt-5">
          {!collapsed && <p className="eyebrow mb-3 px-3 text-ink-faint">RESEARCH / 研究</p>}
          <nav className="space-y-1">{researchNav.map(renderLink)}</nav>
          {!collapsed && <div className="mx-3 my-6 border-t border-line" />}
          <button type="button" onClick={() => setSystemOpen(!systemOpen)} aria-expanded={systemOpen}
            aria-label="展开或收起系统管理" title={collapsed ? '系统管理' : undefined}
            className="mb-2 mt-5 flex w-full items-center gap-2 px-3 py-2 text-xs text-ink-faint">
            <Settings className="h-4 w-4 shrink-0" />
            {!collapsed && <><span>系统管理</span><ChevronDown className={`ml-auto h-3.5 w-3.5 transition-transform ${systemOpen ? 'rotate-180' : ''}`} /></>}
          </button>
          {systemOpen && <nav aria-label="系统管理" className="space-y-0.5">{systemNav.map(renderLink)}</nav>}
        </div>
        <div className="space-y-2 border-t border-line p-3">
          {renderLink(settingsNav)}
          {!collapsed && <details className="rounded-lg border border-line px-3 py-2 text-xs text-ink-muted">
            <summary>运行选项 · {agents.length} 个执行体</summary>
            <div className="space-y-3 py-3">
              <ModeToggle compact />
              <button type="button" onClick={onToggleDemo} className="flex items-center gap-2 text-brand">
                <FlaskConical className="h-4 w-4" />演示模式 {demoMode ? 'ON · 退出' : 'OFF · 进入'}
              </button>
            </div>
          </details>}
          <button onClick={() => setCollapsed(!collapsed)} title={collapsed ? '展开侧栏' : '收起侧栏'}
            className="flex w-full items-center gap-2 px-3 py-2 text-xs text-ink-faint hover:text-ink">
            {collapsed ? <PanelLeft className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
            {!collapsed && '收起侧栏'}
          </button>
        </div>
      </aside>

      <div className="main-mobile flex min-w-0 flex-1 flex-col">
        <header className="flex h-[76px] shrink-0 items-center justify-between gap-3 border-b border-line bg-surface px-4 md:px-8">
          <div className="flex min-w-0 items-center gap-3">
            <BrandMark className="h-7 w-7 shrink-0 text-brand md:hidden" />
            <div className="min-w-0">
              <h1 className="text-base font-medium text-ink">{currentPage?.label || '织光工作台'}</h1>
              <p className="hidden text-xs text-ink-faint sm:block">{currentPage?.subtitle}</p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-4 text-xs">
            {demoMode && <span className="rounded-full border border-brand/20 bg-brand-soft px-2.5 py-1 text-brand-strong">演示数据</span>}
            <NavLink to="/health" className="flex items-center gap-2 text-ink-muted" title={connState === 'offline' ? '后端不可达，正在重试；点此检查服务' : '查看服务健康'}>
              <span className={`h-1.5 w-1.5 rounded-full ${connDot}`} /><span className="hidden sm:inline">{connLabel}</span>
              <span className="sr-only sm:hidden">{connLabel}</span>
            </NavLink>
            {user && <div className="hidden items-center gap-3 border-l border-line pl-4 sm:flex">
              <span className="text-ink-muted">{user.username}<span className="ml-2 text-ink-faint">{user.role === 'admin' ? '管理员' : '只读'}</span></span>
              <button onClick={logout} title="退出登录" aria-label="退出登录" className="rounded p-2 text-ink-faint hover:bg-surface-raised"><LogOut className="h-4 w-4" /></button>
            </div>}
          </div>
        </header>
        {demoMode && <div className="shrink-0 border-b border-brand/20 bg-brand-soft px-4 py-2 text-xs text-brand-strong md:px-8">
          演示模式：控制台为内置合成数据，非本次实时生成；其余页面仍读取真实环境。写操作与付费操作已拦截。
          <button onClick={onToggleDemo} className="ml-2 underline underline-offset-4">退出演示</button>
        </div>}
        {liveTransport === 'polling' && !demoMode && <div className="shrink-0 border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800 md:px-8">
          实时通道已降级为轮询（每 2 秒更新），任务不受影响，进度可能稍有延迟。
        </div>}
        <main id="workspace-content" tabIndex={-1} className="workspace-main mobile-scroll min-w-0 flex-1 overflow-auto p-4 outline-none md:p-8">
          {children}
          <footer className="mt-10 flex flex-wrap items-center justify-between gap-2 border-t border-line pt-4 text-xs text-ink-faint">
            <span>织光 · 上市公司研究工作台</span>
            <span>研究试用阶段 · 不构成投资建议</span>
          </footer>
        </main>
      </div>

      <nav aria-label="移动导航" className="mobile-bottom-nav fixed inset-x-0 bottom-0 z-30 flex border-t border-line bg-surface md:hidden">
        {MOBILE_PRIMARY.map(({ to, icon: Icon, label }) => <NavLink key={to} to={to} end={to === '/'}
          className={({ isActive }) => `flex min-w-0 flex-1 flex-col items-center justify-center gap-1 text-xs ${isActive ? 'text-brand-strong' : 'text-ink-muted'}`}>
          <Icon className="h-5 w-5" strokeWidth={1.6} />{label}
        </NavLink>)}
        <button ref={moreButtonRef} onClick={() => setMoreOpen(true)} aria-expanded={moreOpen}
          className="flex flex-1 flex-col items-center justify-center gap-1 text-xs text-ink-muted"><MoreHorizontal className="h-5 w-5" />更多</button>
      </nav>

      {moreOpen && createPortal(
        <div className="fixed inset-0 z-50 flex items-end bg-black/30 md:hidden" onClick={() => setMoreOpen(false)}>
          <div ref={drawerRef} role="dialog" aria-modal="true" aria-labelledby="more-title"
            className="max-h-[85dvh] w-full overflow-y-auto rounded-t-2xl bg-surface p-5 pb-8 shadow-xl"
            onClick={event => event.stopPropagation()} onKeyDown={event => {
              if (event.key === 'Escape') setMoreOpen(false)
              if (event.key !== 'Tab') return
              const controls = drawerRef.current?.querySelectorAll<HTMLElement>('button, a, summary')
              if (!controls?.length) return
              const first = controls[0]
              const last = controls[controls.length - 1]
              if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
              else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
            }}>
            <div className="mb-5 flex items-center justify-between"><h2 id="more-title" className="text-base font-medium">系统与账户</h2>
              <button onClick={() => setMoreOpen(false)} aria-label="关闭更多菜单" className="rounded p-2"><X className="h-5 w-5" /></button>
            </div>
            <nav className="grid grid-cols-2 gap-2">{MOBILE_MORE.map(renderLink)}</nav>
            <div className="mt-5 flex flex-wrap items-center justify-between gap-4 border-t border-line pt-4">
              <ModeToggle compact />
              <button onClick={onToggleDemo} className="text-sm text-brand">演示模式 {demoMode ? 'ON · 退出' : 'OFF · 进入'}</button>
            </div>
            {user && <div className="mt-4 flex items-center justify-between border-t border-line pt-4 text-sm text-ink-muted">
              <span>{user.username} · {user.role === 'admin' ? '管理员' : '只读'}</span><button onClick={logout} className="flex items-center gap-2"><LogOut className="h-4 w-4" />退出登录</button>
            </div>}
          </div>
        </div>, document.body,
      )}
      {new URLSearchParams(window.location.search).has('debug') && <div className="fixed right-4 top-20 z-40 rounded-lg border border-line-strong bg-surface p-4 text-xs shadow-lg">
        <div className="mb-2 flex items-center gap-2 text-brand">运行诊断 <ArrowUpRight className="h-3 w-3" /></div>
        <div>连接：{String(connected)} · 状态：{status}</div><div className="max-w-xs break-all">任务：{currentTaskId || '无'}</div><div>执行体：{agents.length}</div>
      </div>}
    </div>
  )
}
