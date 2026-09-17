import { memo } from 'react'
import { AlertTriangle, CheckCircle2, FileSpreadsheet, Link2, Sigma } from 'lucide-react'

import { summarizeWorkingPaper } from '../lib/researchGoal'

/**
 * S2：可重算底稿面板（结果页"先结论与目标达成、再关键指标/来源与缺口"）。
 *
 * 只显示底稿里真实存在的东西：
 * - 目标达成与否按**底稿自身**判定（缺证据/主体冲突 → 未达成，并说明为什么）；
 * - 每条明细带 `fact_id`、主体、期间、币种单位、来源链接与核实状态（关键数字据此定位）；
 * - 计算值单独一段，显示**公式与输入 fact_id**（读者可自己算一遍）。
 */
function WorkingPaperPanelImpl({ paper }: { paper: unknown }) {
  const s = summarizeWorkingPaper(paper)
  if (!s.hasPaper) return null

  return (
    <div className="border border-slate-800 rounded-xl overflow-hidden bg-slate-900/40">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-slate-800">
        <FileSpreadsheet className="w-4 h-4 text-cyan-400" />
        <span className="text-sm text-slate-200">可重算底稿</span>
        <span className={`ml-auto inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs ${
          s.goalMet ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400'}`}>
          {s.goalMet
            ? <><CheckCircle2 className="w-3 h-3" />目标达成（{s.present}/{s.required}）</>
            : <><AlertTriangle className="w-3 h-3" />未达成（{s.present}/{s.required}）</>}
        </span>
        {s.asOf && <span className="text-xs text-slate-500">资料截至 {s.asOf}</span>}
      </div>

      {(s.missing.length > 0 || s.gaps.length > 0 || s.problems.length > 0) && (
        <div className="px-4 py-3 border-b border-slate-800 space-y-1">
          <div className="text-xs text-slate-400">缺口与待核验项（不得据此声称已达成）</div>
          <ul className="text-xs space-y-0.5">
            {s.missing.map(m => (
              <li key={`m-${m}`} className="text-amber-400">· 缺少必需事实：{m}</li>
            ))}
            {s.gaps.map(g => <li key={`g-${g}`} className="text-amber-400">· {g}</li>)}
            {s.problems.map(p => <li key={`p-${p}`} className="text-rose-400">· 待核验：{p}</li>)}
          </ul>
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-400 bg-slate-900/60">
              <th className="text-left px-3 py-2">指标</th>
              <th className="text-left px-3 py-2">主体</th>
              <th className="text-left px-3 py-2">期间</th>
              <th className="text-right px-3 py-2">数值</th>
              <th className="text-left px-3 py-2">来源</th>
              <th className="text-left px-3 py-2">核实状态</th>
              <th className="text-left px-3 py-2">fact_id</th>
            </tr>
          </thead>
          <tbody>
            {s.facts.map(f => (
              <tr key={f.factId} className="border-t border-slate-800/60">
                <td className="px-3 py-2 text-slate-200">{f.label}</td>
                <td className="px-3 py-2 text-slate-300">{f.entity}</td>
                <td className="px-3 py-2 text-slate-400">{f.period}</td>
                <td className="px-3 py-2 text-right text-slate-200">
                  {f.value} <span className="text-slate-500">{f.unit}</span>
                </td>
                <td className="px-3 py-2">
                  {f.sourceUrl
                    ? <a href={f.sourceUrl} target="_blank" rel="noreferrer"
                        className="inline-flex items-center gap-1 text-cyan-400 hover:underline">
                        <Link2 className="w-3 h-3" />来源位置
                      </a>
                    : <span className="text-amber-400">无来源位置</span>}
                </td>
                <td className={`px-3 py-2 ${f.verifyState === 'verified' ? 'text-emerald-400' : 'text-slate-400'}`}>
                  {f.verifyState}
                </td>
                <td className="px-3 py-2 text-slate-500 font-mono">{f.factId}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {s.derived.length > 0 && (
        <div className="px-4 py-3 border-t border-slate-800">
          <div className="flex items-center gap-1.5 text-xs text-slate-400 mb-1">
            <Sigma className="w-3 h-3" />计算值（公式与输入已附，可自行重算）
          </div>
          <ul className="text-xs space-y-1">
            {s.derived.map(d => (
              <li key={d.factId} className="text-slate-300">
                <span className="text-slate-200">{d.label || d.period}</span>{' '}
                <span className="text-slate-400">{d.period}</span>{' '}
                <span className="text-cyan-300">{d.value}</span>
                <span className="text-slate-500"> · {d.formula}</span>
                {d.inputs.length > 0 && (
                  <span className="text-slate-500 font-mono"> · 输入 {d.inputs.join(', ')}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

export const WorkingPaperPanel = memo(WorkingPaperPanelImpl)
