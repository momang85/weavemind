import { useEffect, useRef } from 'react'

/**
 * 可见性感知的 setInterval：页面隐藏（切后台标签）时暂停计时，
 * 重新可见时立即补跑一次再恢复节拍——后台不浪费请求（审计 #18）。
 */
export function useVisibleInterval(cb: () => void, ms: number | null) {
  const saved = useRef(cb)
  saved.current = cb
  useEffect(() => {
    if (ms === null || ms <= 0) return
    let timer: ReturnType<typeof setInterval> | null = null
    let stopped = false

    const start = () => {
      if (timer || stopped) return
      timer = setInterval(() => saved.current(), ms)
    }
    const stop = () => {
      if (timer) { clearInterval(timer); timer = null }
    }
    const onVis = () => {
      if (document.hidden) { stop() } else { saved.current(); start() }
    }
    if (!document.hidden) start()
    document.addEventListener('visibilitychange', onVis)
    return () => {
      stopped = true
      stop()
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [ms])
}
