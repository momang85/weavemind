// 轻量前端鉴权：token 存 localStorage，全局 fetch 自动附加 Authorization 头。
// 401 时清除本地会话并广播事件，App 据此回到登录页。
export const TOKEN_KEY = 'weavemind_token'
export const USER_KEY = 'weavemind_user'

export type AuthUser = { username: string; role: 'admin' | 'viewer' }

// 这些接口不要求登录（或 401 属于正常业务返回），不应触发“回到登录页”
const PUBLIC_API_PATHS = ['/api/login', '/api/setup-admin', '/api/health', '/api/auth/bootstrap']

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

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

export function setAuth(token: string, user: AuthUser) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
  window.dispatchEvent(new Event('weavemind:auth-changed'))
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  window.dispatchEvent(new Event('weavemind:auth-changed'))
}

export function isAuthed(): boolean {
  return !!getToken()
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
    const headers = new Headers(init?.headers)
    const token = getToken()
    if (!isPublicApi && token && !headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${token}`)
    }
    const res = await originalFetch(input, { ...init, headers })
    if (res.status === 401 && !isPublicApi && !path.startsWith('/share/')) {
      clearAuth()
    }
    return res
  }
}
