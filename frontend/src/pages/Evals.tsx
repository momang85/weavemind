import { useState, useEffect } from 'react'
import { FlaskConical, RefreshCw } from 'lucide-react'

interface EvalScore { task_id: string; scores: Record<string, number> }

export default function Evals() {
  const [cal, setCal] = useState<Record<string, any>>({})
  const [recent, setRecent] = useState<EvalScore[]>([])
  const [loading, setLoading] = useState(true)

  const load = async () => {
    try {
      const d = await (await fetch('/api/evals')).json()
      setCal(d.calibration ?? {})
      setRecent(d.recent ?? [])
    } catch {}
    setLoading(false)
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [])

  const metrics = ['answer_correctness', 'faithfulness', 'context_recall', 'context_precision']

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <div className="flex items-center gap-3">
        <FlaskConical className="w-6 h-6 text-cyan-400" />
        <h1 className="text-slate-200 text-lg font-semibold">评测看板</h1>
        <button onClick={load} className="ml-auto text-slate-500 hover:text-cyan-400">
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5">
          <div className="text-xs text-slate-500 mb-3">Judge 校准（黄金测试集）</div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="text-2xl text-cyan-400 font-bold">
                {typeof cal.mae === 'number' ? cal.mae.toFixed(3) : '—'}
              </div>
              <div className="text-[10px] text-slate-500">MAE（误差）</div>
            </div>
            <div>
              <div className="text-2xl text-emerald-400 font-bold">
                {typeof cal.pass_agreement === 'number' ? (cal.pass_agreement * 100).toFixed(0) + '%' : '—'}
              </div>
              <div className="text-[10px] text-slate-500">通过/失败一致率</div>
            </div>
            <div>
              <div className="text-xl text-slate-300 font-bold">{cal.n ?? '—'}</div>
              <div className="text-[10px] text-slate-500">样本数</div>
            </div>
            <div>
              <div className="text-xl text-slate-300 font-bold">{typeof cal.threshold === 'number' ? cal.threshold : '—'}</div>
              <div className="text-[10px] text-slate-500">达标阈值</div>
            </div>
          </div>
        </div>

        <div className="lg:col-span-2 bg-slate-900 border border-slate-800 rounded-2xl p-5">
          <div className="text-xs text-slate-500 mb-3">最近任务评测分数（评测驱动反思）</div>
          {recent.length === 0 && <div className="text-slate-600 text-xs">暂无评测记录（任务匹配到评测案例后自动产生）</div>}
          <div className="space-y-2 max-h-96 overflow-y-auto">
            {recent.map((r, i) => (
              <div key={i} className="bg-slate-800/40 border border-slate-800 rounded-lg p-3">
                <div className="text-slate-400 text-xs mb-1 truncate">{r.task_id}</div>
                <div className="grid grid-cols-4 gap-2">
                  {metrics.map((m) => (
                    <div key={m} className="text-[10px] text-slate-500">
                      {m}: <span className="text-cyan-300 font-semibold">{typeof r.scores?.[m] === 'number' ? r.scores[m].toFixed(2) : '—'}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
      {loading && <div className="text-slate-600 text-xs">加载中…</div>}
    </div>
  )
}
