// 分析工作台状态：**全页只有一份** /api/task/<id>/analysis 结果（P0-4 的单源）。
//
// 为什么要有这个 store：分析工作台与「导出与复核」原本各拿一份状态
// （工作台 fetch analysis 的 current_package，导出面板读 report.export.package），
// 于是同一屏上出现「当前包: deliverables_*.zip」与「本次任务暂无交付包」并存。
// 现在：一个按 taskId 去重、带 TTL/上限的模块级缓存负责取数，
// 两个面板都通过 usePackageState 读同一份解析结果（resolvePackage 纯函数）。
// 展示口径与取数都只有一处实现，两边不可能再各说一套。

import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { resolvePackage, type PackageResolution } from './packageState'

export type AnalysisStatus = 'idle' | 'loading' | 'ok' | 'error'

export interface AnalysisEntry {
  status: AnalysisStatus
  error: string
  data: any
}

const EMPTY: AnalysisEntry = { status: 'idle', error: '', data: null }
/** 最多同时记几个任务的分析状态（列表页来回切任务时不无限增长） */
const MAX_TASKS = 5
const TIMEOUT_MS = 12000

const entries = new Map<string, AnalysisEntry>()
const inflight = new Map<string, Promise<void>>()
const listeners = new Set<() => void>()

function emit() { for (const l of Array.from(listeners)) l() }

function put(taskId: string, entry: AnalysisEntry) {
  entries.delete(taskId)          // 重新插入 → 最近使用排到末尾
  entries.set(taskId, entry)
  while (entries.size > MAX_TASKS) {
    const oldest = entries.keys().next().value
    if (oldest === undefined) break
    entries.delete(oldest)
  }
  emit()
}

export function analysisEntryOf(taskId?: string | null): AnalysisEntry {
  const id = String(taskId || '')
  return (id && entries.get(id)) || EMPTY
}

function load(taskId: string, force = false): Promise<void> {
  if (!taskId) return Promise.resolve()
  const running = inflight.get(taskId)
  if (running) return running
  const cur = entries.get(taskId)
  if (!force && cur && cur.status === 'ok') return Promise.resolve()
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(), TIMEOUT_MS)
  put(taskId, { status: 'loading', error: '', data: cur?.data ?? null })
  const p = (async () => {
    try {
      const res = await fetch(`/api/task/${encodeURIComponent(taskId)}/analysis`,
        { signal: ac.signal })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const d = await res.json()
      if (d && d.error) throw new Error(String(d.error))
      put(taskId, { status: 'ok', error: '', data: d })
    } catch (e: any) {
      const aborted = e?.name === 'AbortError'
      put(taskId, { status: 'error', error: aborted ? '读取超时（12 秒无响应）' : String(e?.message || e), data: null })
    } finally {
      clearTimeout(timer)
      inflight.delete(taskId)
    }
  })()
  inflight.set(taskId, p)
  return p
}

/** 订阅某个任务的分析状态（同一 taskId 全页共享一次请求）。 */
export function useAnalysisPanel(taskId: string | null) {
  const subscribe = useCallback((cb: () => void) => {
    listeners.add(cb)
    return () => { listeners.delete(cb) }
  }, [])
  const entry = useSyncExternalStore(subscribe, () => analysisEntryOf(taskId))
  useEffect(() => { void load(String(taskId || '')) }, [taskId])
  const reload = useCallback(() => load(String(taskId || ''), true), [taskId])
  return { data: entry.data, status: entry.status, error: entry.error, reload }
}

/** 交付包口径：两个面板调用同一函数、传同一组输入 → 结果必然一致。 */
export function usePackageState(
  taskId: string | null, exportPackage?: unknown, packageNote?: unknown,
) {
  const panel = useAnalysisPanel(taskId)
  const res: PackageResolution = resolvePackage({
    exportPackage,
    analysisPackage: panel.data?.current_package,
    packageNote: panel.data?.package_note ?? packageNote,
  })
  return { res, panel, loading: panel.status === 'loading' || panel.status === 'idle' }
}
