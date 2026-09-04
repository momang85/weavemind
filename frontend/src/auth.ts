// 轻量前端鉴权：会话由后端 HttpOnly、SameSite=Lax 的 session Cookie 承载，
// 前端不接触、不存储 token（避免 XSS 窃取）。localStorage 仅保留展示用的非敏感用户信息。
// 401 时清除本地会话并广播事件，App 据此回到登录页。
export const USER_KEY = 'weavemind_user'

export type AuthUser = { username: string; role: 'admin' | 'viewer' }

// 这些接口不要求登录（或 401 属于正常业务返回），不应触发“回到登录页”
const PUBLIC_API_PATHS = ['/api/login', '/api/setup-admin', '/api/health', '/api/auth/bootstrap']

export function getAuthUser(): AuthUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    if (!raw) return null
    const u = JSON.parse(raw) as AuthUser
    return u && typeof u.username === 'string' ? u : null
  } catch {
    return null
  }
}

export function setAuth(user: AuthUser) {
  localStorage.setItem(USER_KEY, JSON.stringify(user))
  window.dispatchEvent(new Event('weavemind:auth-changed'))
}

export function clearAuth() {
  localStorage.removeItem(USER_KEY)
  window.dispatchEvent(new Event('weavemind:auth-changed'))
}

// 本地是否认为已登录（存在已保存的用户）。真实会话由服务端 session Cookie 判定，
// 组件挂载后再用 verifySession() 与 /api/status 对齐，避免刷新后误判。
export function isAuthed(): boolean {
  return !!getAuthUser()
}

// 异步探测服务端会话是否仍有效；会话失效/未登录时清除本地用户并返回 false。
export async function verifySession(): Promise<boolean> {
  try {
    const res = await fetch('/api/status')
    if (res.ok) return true
    if (res.status === 401) clearAuth()
    return false
  } catch {
    return false
  }
}

let installed = false

export function installAuthFetch() {
  if (installed || typeof window === 'undefined') return
  installed = true
  const originalFetch = window.fetch.bind(window)

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input
      : input instanceof Request ? input.url
      : input.toString()
    const path = url.replace(/^https?:\/\/[^/]+/, '')
    const isPublicApi = PUBLIC_API_PATHS.some(p => path.startsWith(p))
    // 不注入任何 Authorization；仅凭浏览器自动携带的 session Cookie 完成鉴权。
    const res = await originalFetch(input, init)
    if (res.status === 401 && !isPublicApi && !path.startsWith('/share/')) {
      clearAuth()
    }
    return res
  }
}