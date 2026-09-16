// 演示模式的"真实操作边界"。
//
// 背景（架构审查 F1 补修 ②）：此前只禁用了"提交任务"与"快答"两个按钮，界面却对外宣称
// "不调用真实接口"——而设置保存/连通性测试、记忆删除、演化触发、通知发送等入口仍会真实
// 落库、触发后台任务甚至产生费用。承诺与事实不符。
//
// 处理方式：在 fetch 层**统一拦截写操作与付费操作**（演示模式下请求不会离开浏览器，
// 直接返回 403 + 可读原因），只读接口仍走真实服务。因此文案必须如实写明：
// 演示只替换"控制台展示的数据"，其余页面读的是真实环境，写操作已被拦截。

// 演示模式下会被拦截的路径（写 / 付费 / 后台任务 / 管理操作）。前缀匹配。
// 注意：任务相关端点有两种前缀——提交是 `/task`，而取消/重跑等动作是 `/api/task/...`，
// 两者都要列（此前只写 `/task`，导致"停止"在演示模式下仍会真的发出去）。
//
// 这里的清单按**实际调用清单**核对：终止 worker 的真实路由是 `POST /api/kill-worker`
// （不匹配 `/api/agents`），单智能体直发是 `POST /api/quick-answer` 与 `/api/single-agent`。
export const DEMO_BLOCKED_PREFIXES = [
  '/task',                 // 提交任务、重跑、重新运行（真实 LLM 费用）
  '/api/task',             // 取消/重跑等任务动作（此前漏拦）
  '/api/quick-answer',     // 快答 / 单智能体直发（真实检索 + 模型调用）
  '/api/memory',           // 记忆/策略的删除等写操作
  '/api/evolution',        // 触发/审批演化（真实后台任务，会烧 token）
  '/api/notifications',    // 通知配置写回与测试发送
  '/api/scheduled-jobs',   // 定时任务增删改（会导致日后真实任务运行）
  '/api/users',            // 用户管理
  '/api/config',           // 配置写回与连通性测试（会调用付费端点）
  '/api/plan',             // 计划确认/放弃（确认后继续花钱执行）
  '/api/step',             // 步骤级人工确认（同上）
  '/api/context',          // 上下文文件提取（服务端解析）
  '/api/deliverable',      // 运行交付物代码（服务端执行）
  '/api/verify',           // 数据溯源复核（LLM 费用）
  '/api/share',            // 生成/撤销分享链接（写状态）
  '/api/kill-worker',      // 终止 worker（改进程池）
  '/api/llm-mode',         // 切换 LLM 路由（改全局模式）
  '/api/single-agent',     // 单智能体直发（费用）
] as const

// **付费的 GET**：不能按"只读一律放行"处理。
// `GET /api/memory/summary` 的冷缓存与 `?refresh=1` 都会走 `_build_memory_summary` → `call_llm`，
// 也就是"只读方法照样花钱"。这类接口按付费操作拦截，与写操作同等待遇。
export const DEMO_BLOCKED_GET = [
  '/api/memory/summary',   // 记忆摘要：冷缓存 / ?refresh=1 会生成摘要（付费）
] as const

// 显式放行（不拦截）：会话相关操作必须可用，否则用户退出不了演示/登不回来。
//   POST /api/logout、/api/login、/api/setup-admin、/api/auth/*
// 其余只读请求（GET/HEAD/OPTIONS）放行——演示只替换控制台展示数据，其余页面读真实环境。

// 演示状态：URL 参数**只用于初始化**。一旦显式开关过（setDemoActive），
// 就以显式状态为准——否则 `?demo` 进入后 toggleDemo(false) 会"界面显示已退出、守卫仍在拦截"。
const state = { active: false, resolved: false }

export const DEMO_REASON_WRITE =
  '演示模式：该操作会真实修改数据或产生费用，已被拦截。退出演示模式（侧栏「演示 ON」）后可执行。'
export const DEMO_REASON_PAID_GET =
  '演示模式：该接口会调用付费模型（记忆摘要的冷缓存与 ?refresh=1 都会生成摘要），已按付费操作拦截。'

/** 是否为演示模式：显式开关优先，未显式设置过才回落到 URL 的 `?demo`。 */
export function demoActive(): boolean {
  if (state.resolved) return state.active
  try {
    return new URLSearchParams(window.location.search).has('demo')
  } catch {
    return false
  }
}

/** 由 store 的 toggleDemo 调用，保持守卫与界面状态一致（并锁定显式状态）。 */
export function setDemoActive(on: boolean): void {
  state.active = !!on
  state.resolved = true
}

/** 供测试复位（模块级状态与安装标记都不该跨用例泄漏）。 */
export function resetDemoGuardForTest(): void {
  state.active = false
  state.resolved = false
  installed = false
}

function _pathOf(path: string): string {
  return String(path || '').replace(/^https?:\/\/[^/]+/, '')
}

/** 拦截原因；不拦截时返回 null。纯函数，便于真实行为测试。 */
export function demoBlockReason(path: string, method?: string, active?: boolean): string | null {
  const on = active === undefined ? demoActive() : active
  if (!on) return null
  const p = _pathOf(path)
  if (DEMO_BLOCKED_GET.some(pre => p === pre || p.startsWith(pre + '?') || p.startsWith(pre))) {
    return DEMO_REASON_PAID_GET
  }
  const m = String(method || 'GET').toUpperCase()
  if (m === 'GET' || m === 'HEAD' || m === 'OPTIONS') return null
  return DEMO_BLOCKED_PREFIXES.some(pre => p === pre || p.startsWith(pre)) ? DEMO_REASON_WRITE : null
}

/** 该请求在演示模式下是否应被拦截（只读放行，付费 GET 除外）。 */
export function blockedInDemo(path: string, method?: string, active?: boolean): boolean {
  return demoBlockReason(path, method, active) !== null
}

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
    const reason = demoBlockReason(url, method)
    if (reason) {
      return new Response(JSON.stringify({ error: reason, demo_blocked: true }), {
        status: 403,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    return prev(input, init)
  }
}
