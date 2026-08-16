import { useState, useEffect } from 'react'
import { Layers, RefreshCw } from 'lucide-react'

interface Skill { name: string; description: string; owner?: string; version?: string; applies?: string[]; lessons?: any[] }

export default function Skills() {
  const [skills, setSkills] = useState<Skill[]>([])

  const load = async () => {
    try {
      const d = await (await fetch('/api/skills')).json()
      setSkills(d.skills ?? [])
    } catch {}
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [])

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <Layers className="w-6 h-6 text-cyan-400" />
        <h1 className="text-slate-200 text-lg font-semibold">Skill 管理</h1>
        <button onClick={load} className="ml-auto text-slate-500 hover:text-cyan-400">
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {skills.length === 0 && <div className="text-slate-600 text-xs">暂无 Skill</div>}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {skills.map((s) => (
          <div key={s.name} className="bg-slate-900 border border-slate-800 rounded-2xl p-5">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-cyan-400 text-sm font-semibold">{s.name}</span>
              {s.version && <span className="text-[10px] text-slate-600">v{s.version}</span>}
              {s.owner && <span className="text-[10px] text-slate-500">owner: {s.owner}</span>}
            </div>
            <div className="text-slate-400 text-xs mb-2">{s.description}</div>
            {s.applies && s.applies.length > 0 && (
              <div className="flex flex-wrap gap-1 mb-2">
                {s.applies.map((c) => (
                  <span key={c} className="px-2 py-0.5 rounded-full bg-slate-800 text-[10px] text-slate-400">{c}</span>
                ))}
              </div>
            )}
            {s.lessons && s.lessons.length > 0 && (
              <div className="mt-2 pt-2 border-t border-slate-800">
                <div className="text-[10px] text-amber-500 mb-1">自动沉淀教训</div>
                {s.lessons.map((l, i) => (
                  <div key={i} className="text-[11px] text-slate-500 mb-1">
                    {String(l.issue || '').slice(0, 80)}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
