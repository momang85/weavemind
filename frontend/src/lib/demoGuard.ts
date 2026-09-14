// 演示模式的"真实操作边界"。
//
// 背景（架构审查 F1 补修 ②）：此前只禁用了"提交任务"与"快答"两个按钮，界面却对外宣称
// "不调用真实接口"——而设置保存/连通性测试、记忆删除、演化触发、通知发送等入口仍会真实
// 落库、触发后台任务甚至产生费用。承诺与事实不符。
//
// 处理方式：在 fetch 层**统一拦截写操作与付费操作**（演示模式下请求不会离开浏览器，
// 直接返回 403 + 可读原因），只读接口仍走真实服务。因此文案必须如实写明：
// 演示只替换"控制台展示的数据"，其余页面读的是真实环境，写操作已被拦截。

const state = { active: false }

// 演示模式下会被拦截的路径（写 / 付费 / 后台任务 / 管理操作）。
// 前缀匹配：`/task` 同时覆盖 `/task/<id>/cancel`、`/task/<id>/retry` 等派生动作。
export const DEMO_BLOCKED_PREFIXES = [
  '/task',                 // 提交任务、重跑、取消（真实 LLM 费用）
  '/api/quick-answer',     // 快答（真实检索 + 模型调用）
  '/api/memory',           // 记忆/策略的删除、审批等写操作
  '/api/evolution',        // 触发/审批演化（真实后台任务，会烧 token）
  '/api/settings',         // 设置写回
  '/api/notifications',    // 通知配置写回与测试发送
  '/api/scheduled-jobs',   // 定时任务增删改
  '/api/users',            // 用户管理
  '/api/config',           // 配置写回与连通性测试
  '/api/agents',           // 终止/直发等管理动作
] as const

/** 是否为演示模式（显式开关，或 URL 带 ?demo —— 与 store 的初始判定一致）。 */
export function demoActive(): boolean {
  if (state.active) return true
  try {
    return new URLSearchParams(window.location.search).has('demo')
  } catch {
    return false
  }
}

/** 由 store 的 toggleDemo 调用，保持守卫与界面状态一致。 */
export function setDemoActive(on: boolean): void {
  state.active = !!on
}

/** 该请求在演示模式下是否应被拦截（只读放行）。纯函数，便于测试与复用。 */
export function blockedInDemo(path: string, method?: string, active?: boolean): boolean {
  const on = active === undefined ? demoActive() : active
  if (!on) return false
  const m = String(method || 'GET').toUpperCase()
  if (m === 'GET' || m === 'HEAD' || m === 'OPTIONS') return false
  const p = String(path || '').replace(/^https?:\/\/[^/]+/, '')
  return DEMO_BLOCKED_PREFIXES.some(pre => p === pre || p.startsWith(pre))
}

const DEMO_BLOCK_MESSAGE =
  '演示模式：该操作会真实修改数据或产生费用，已被拦截。退出演示模式（侧栏「演示 ON」）后可执行。'

let installed = false

/** 安装拦截层（幂等）。必须在 installAuthFetch 之后调用，保证最外层先看到请求。 */
export function installDemoGuard(): void {
  if (installed || typeof window === 'undefined') return
  installed = true
  const prev = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input
      : input instanceof Request ? input.url
      : input.toString()
    const method = init?.method || (input instanceof Request ? input.method : 'GET')
    if (blockedInDemo(url, method)) {
      return new Response(JSON.stringify({ error: DEMO_BLOCK_MESSAGE, demo_blocked: true }), {
        status: 403,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    return prev(input, init)
  }
}
