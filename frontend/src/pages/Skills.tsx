import { useState, useEffect } from 'react'
import { useVisibleInterval } from '../lib/useVisibleInterval'
import { formatLocalTimeLabel } from '../lib/timeText'
import { lessonTruncatedNote, skillLessonView } from '../lib/skillLessons'
import { Layers, RefreshCw } from 'lucide-react'

interface Skill { name: string; description: string; owner?: string; version?: string; applies?: string[]; lessons?: any[] }

export default function Skills() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [error, setError] = useState('')

  const load = async () => {
    try {
      const d = await (await fetch('/api/skills')).json()
      setSkills(d.skills ?? [])
      setError('')
    } catch {
      setError('加载 Skill 失败，请检查后端服务')
    }
  }

  useEffect(() => { load() }, [])
  useVisibleInterval(load, 15000)

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <Layers className="w-6 h-6 text-brand" />
        <h1 className="text-ink text-lg font-semibold">Skill 管理</h1>
        <button onClick={load} aria-label="刷新 Skill 列表" className="ml-auto text-ink-faint hover:text-brand">
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {error && <div className="text-red-700 text-xs bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">{error}</div>}
      {!error && skills.length === 0 && <div className="text-ink-faint text-xs">暂无 Skill</div>}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {skills.map((s) => (
          <div key={s.name} className="bg-surface border border-line rounded-2xl p-5">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-brand text-sm font-semibold">{s.name}</span>
              {s.version && <span className="text-xs text-ink-faint">v{s.version}</span>}
              {s.owner && <span className="text-xs text-ink-faint">负责方：{s.owner}</span>}
            </div>
            <div className="text-ink-muted text-xs mb-2">{s.description}</div>
            {s.applies && s.applies.length > 0 && (
              <div className="flex flex-wrap gap-1 mb-2">
                {s.applies.map((c) => (
                  <span key={c} className="px-2 py-0.5 rounded-full bg-surface-raised text-xs text-ink-muted">{c}</span>
                ))}
              </div>
            )}
            {s.lessons && s.lessons.length > 0 && (
              <div className="mt-2 pt-2 border-t border-line">
                <div className="text-xs text-amber-700 mb-1">自动沉淀教训</div>
                {s.lessons.map((l, i) => {
                  // 正文完整显示（旧实现 slice(0, 80) 会切成断句且不加省略号）：
                  // 按分号拆成多条并列问题，逐条折行，不硬截断。
                  const view = skillLessonView(l)
                  const truncatedNote = lessonTruncatedNote(view)
                  return (
                    <div key={l.id || i} className="text-xs text-ink-faint mb-2">
                      {view.scopeNote && (
                        <div className="mb-0.5 text-ink-faint">（{view.scopeNote}）</div>
                      )}
                      {view.segments.length > 1 ? (
                        <ul className="list-disc list-inside space-y-0.5">
                          {view.segments.map((seg, j) => (
                            <li key={j} className="break-words">{seg}</li>
                          ))}
                        </ul>
                      ) : (
                        <div className="break-words whitespace-pre-wrap">{view.segments[0] || '（这条教训没有正文）'}</div>
                      )}
                      {view.tailNote && (
                        <div className="mt-0.5 text-amber-700/90">{view.tailNote}</div>
                      )}
                      <div className="mt-0.5 text-ink-faint">
                        共 {view.length} 字{truncatedNote}
                        {l.ts ? ` · 记于 ${formatLocalTimeLabel(l.ts)}` : ''}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
