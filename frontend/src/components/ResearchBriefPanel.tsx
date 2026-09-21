// F3-B：研究简报面板——结果页按「关键发现 → 缺口 → 证据 → 修改/重验 → 导出」呈现。
//
// 为什么单独一块：此前关键发现埋在正文里、缺口散在四处（验收横幅/底稿/来源/引用），
// 来源定位（"第 N 页 · 小节：…"）根本没地方显示；金融读者要的是先看结论与缺口，
// 再决定要不要改、怎么导。
//
// 两条纪律：
//   1) 只显示后端给到的字段，缺什么就写"未提供/未取得"，不编；
//   2) 修订走**既有** `POST /api/task/<id>/review/edit`（不另造版本系统），
//      提交后刷新任务详情，用后端返回的版本号与验收结论说话。

import { useState } from 'react'
import type { ResearchBrief, ExportState } from '../stores/types'

interface Props {
  taskId?: string
  research?: ResearchBrief | null
  exportState?: ExportState | null
  onRevised?: () => void
}

const box = 'bg-slate-900 border border-slate-800 rounded-xl p-3'
const h = 'text-xs font-medium text-slate-300 mb-1.5'
const li = 'text-xs text-slate-400 leading-relaxed'

export default function ResearchBriefPanel({ taskId, research, exportState, onRevised }: Props) {
  const [editing, setEditing] = useState(false)
  const [body, setBody] = useState('')
  const [find, setFind] = useState('')
  const [replace, setReplace] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<string>('')

  if (!research) return null
  const sc = research.scope || {}
  const gaps = research.gaps || {}
  const ev = research.evidence || {}
  const findings = research.findings || []
  const charts = research.charts || []
  const evidenceGaps = gaps.evidence || []
  const citationGaps = gaps.citations || []
  const unproven = gaps.unproven || []
  const requiredGaps = gaps.required_data || []
  const rv = research.review || {}
  const claims = research.claims || []
  const unsupported = research.unsupported_claims || []
  const weakClaims = claims.filter(c => c.status === 'unsupported' || c.status === 'needs_check')
  const hasGaps = evidenceGaps.length + citationGaps.length + unproven.length + requiredGaps.length > 0

  const submitRevision = async () => {
    if (!taskId) return
    const payload: Record<string, string> = {}
    if (body.trim()) payload.body = body
    else if (find.trim()) { payload.find = find; payload.replace = replace }
    else { setResult('请填写完整正文，或填写"查找/替换"'); return }
    setBusy(true); setResult('')
    try {
      const res = await fetch(`/api/task/${taskId}/review/edit`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const d = await res.json().catch(() => ({}))
      if (!res.ok) { setResult(String(d.error || `提交失败（HTTP ${res.status}）`)); return }
      const acc = d.acceptance || {}
      const del = d.delivery || {}
      setResult(`新版本 ${String(d.version_id || '').slice(0, 12)}（父 ${String(d.parent_version_id || '').slice(0, 12)}）；`
        + `验收 ${acc.overall || '未知'}${del.draft ? '，交付为草稿' : '，交付已绑定该版本'}`
        + (d.needs_reverify ? '；需重新验收' : ''))
      setEditing(false); setBody(''); setFind(''); setReplace('')
      onRevised?.()
    } catch (e) {
      setResult(`提交失败：${String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-3">
      {/* ① 关键发现：代码装配、带 fact_id，先给结论 */}
      {findings.length > 0 && (
        <div className={box}>
          <div className={h}>关键发现（{findings.length} 条，数字来自底稿）</div>
          <ul className="space-y-1">
            {findings.map((f, i) => <li key={i} className={li}>· {f.text}</li>)}
          </ul>
        </div>
      )}

      {/* ② 缺口：必需数据 / 证据 / 引用 分开列，不混在一起 */}
      {hasGaps && (
        <div className={box}>
          <div className={h}>缺口（分开列：必需数据 / 证据 / 引用）</div>
          {requiredGaps.length > 0 && (
            <div className="mb-1.5">
              <div className="text-xs text-amber-400">必需数据</div>
              <ul>{requiredGaps.map((g, i) => (
                <li key={i} className={li}>· {g.text}{g.materials?.length ? `（需补：${g.materials.join('、')}）` : ''}</li>
              ))}</ul>
            </div>
          )}
          {evidenceGaps.length > 0 && (
            <div className="mb-1.5">
              <div className="text-xs text-amber-400">证据（未取得正文定位）</div>
              <ul><li className={li}>· {evidenceGaps.join('、')}</li></ul>
            </div>
          )}
          {citationGaps.length > 0 && (
            <div className="mb-1.5">
              <div className="text-xs text-amber-400">引用（未能在采用来源中唯一对应）</div>
              <ul><li className={li}>· 正文引用 {citationGaps.map(n => `[${n}]`).join('、')} 已去编号</li></ul>
            </div>
          )}
          {unproven.length > 0 && (
            <div>
              <div className="text-xs text-amber-400">还不能证明什么</div>
              <ul>{unproven.map((u, i) => (
                <li key={i} className={li}>· {u.label}：需先取得{u.materials?.join('、') || '补充材料'}</li>
              ))}</ul>
            </div>
          )}
          {gaps.as_of && <div className="text-xs text-slate-500 mt-1">资料截至 {gaps.as_of}</div>}
        </div>
      )}

      {/* ③ 证据：准入情况 + 未采用材料（含原因）+ 来源定位 */}
      <div className={box}>
        <div className={h}>
          证据（已取得定位 {ev.located ?? 0} 条
          {ev.rules_version ? `；校验规则 ${ev.rules_version}` : ''}）
        </div>
        {(ev.excluded || []).length > 0 && (
          <div className="mb-1.5">
            <div className="text-xs text-slate-500">未采用的材料</div>
            <ul>{(ev.excluded || []).slice(0, 6).map((e, i) => (
              <li key={i} className={li}>· {e.title || e.url}（{e.validation_status}
                {e.published_at ? `；发布 ${e.published_at}` : ''}）</li>
            ))}</ul>
          </div>
        )}
        {(ev.rejected_citations || []).length > 0 && (
          <div className="mb-1.5">
            <div className="text-xs text-slate-500">正文引用但未准入</div>
            <ul>{(ev.rejected_citations || []).slice(0, 6).map((r, i) => (
              <li key={i} className={li}>· {r.title || r.url}（{r.reason}）</li>
            ))}</ul>
          </div>
        )}
        {(research.evidence_locations || []).length > 0 && (
          <div className="mb-1.5">
            <div className="text-xs text-slate-500">解释的出处（小节/页码）</div>
            <ul>{(research.evidence_locations || []).slice(0, 6).map((r, i) => (
              <li key={i} className={li}>· {r.kind}{r.source_n ? ` [${r.source_n}]` : ''}：{r.locator}</li>
            ))}</ul>
          </div>
        )}
        {(research.locators || []).length > 0 && (
          <div>
            <div className="text-xs text-slate-500">字段位置（底稿）</div>
            <ul>{(research.locators || []).slice(0, 6).map((l, i) => (
              <li key={i} className={li}>· {l.label}：{l.locator}</li>
            ))}</ul>
          </div>
        )}
      </div>

      {/* ④ 图表：每张回答一个问题，附数据观察；草稿级不进正文 */}
      {charts.length > 0 && (
        <div className={box}>
          <div className={h}>图表（{charts.length} 张）</div>
          <ul>{charts.map((c, i) => (
            <li key={i} className={li}>
              · {c.question || c.file}{c.observation ? `——${c.observation}` : ''}
              {c.grade && c.grade !== 'publish' ? `（草稿级：${c.draft_reason || '未达发布标准'}）` : ''}
            </li>
          ))}</ul>
        </div>
      )}

      {/* ⑤ 修改与重验：复用既有 review/edit（新版本 + 重验 + 重装配） */}
      {taskId && (
        <div className={box}>
          <div className={h}>修改与重验（新建版本，不覆盖历史）</div>
          {/* 主张与支持状态：与正文标记、缺口、风险、导出同一状态 */}
          {weakClaims.length > 0 && (
            <ul className={li}>
              {weakClaims.slice(0, 5).map((c, i) => (
                <li key={i} className={c.status === 'unsupported' ? 'text-amber-400' : ''}>
                  · {c.status === 'unsupported' ? '未采用来源' : '待核查'}
                  {c.type === 'target_claim' ? '（目标类判断）' : ''}：{String(c.text || '').slice(0, 60)}
                  {c.reason ? `——${c.reason}` : ''}
                </li>
              ))}
              {unsupported.length > 0 && (
                <li className="text-slate-500">
                  · 未采用来源共 {unsupported.length} 条，正文对应句已逐句标『待核查』（不删句）
                </li>
              )}
            </ul>
          )}
          {research.structure_current === false && (
            <div className="text-xs text-amber-400">
              · 面板（发现/缺口/证据）绑定版本 {String(research.structure_version || '未知').slice(0, 12)}
              ，与当前版本 {String(rv.machine?.version_id || '未知').slice(0, 12)} 不一致——
              修订后需重新装配，勿把旧发现当新版
            </div>
          )}
          {/* 机器通过与人工复核**分开**说：自动重验通过不等于研究员已复核 */}
          <ul className={li}>
            <li>· 机器验收：{rv.machine?.overall || '未知'}
              {rv.machine?.version_id ? `（本版 ${String(rv.machine.version_id).slice(0, 12)}` : '（版本未知'}
              {rv.machine?.bound ? '，绑定本版正文）' : '，未绑定本版正文）'}</li>
            {rv.human?.status === 'recorded'
              ? <li>· 人工复核：{rv.human.approver} 于 {rv.human.at || '时间未记录'} 批准
                  {rv.human.version_id ? `（版本 ${String(rv.human.version_id).slice(0, 12)}）` : ''}</li>
              : <li className="text-amber-400">· 人工复核：待复核（无研究员批准记录）——机器重验通过不代表已复核</li>}
          </ul>
          {!editing ? (
            <button type="button" onClick={() => setEditing(true)}
              className="px-3 py-1.5 bg-cyan-500/10 hover:bg-cyan-500/20 text-cyan-400 rounded-lg text-xs">
              修改正文并重验
            </button>
          ) : (
            <div className="space-y-2">
              <textarea value={body} onChange={e => setBody(e.target.value)} rows={5}
                placeholder="替换整份研究正文（留空则用下面的查找/替换）"
                className="w-full bg-slate-800 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-slate-200" />
              <div className="flex gap-2">
                <input value={find} onChange={e => setFind(e.target.value)} placeholder="查找（原文片段）"
                  className="flex-1 bg-slate-800 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-slate-200" />
                <input value={replace} onChange={e => setReplace(e.target.value)} placeholder="替换为"
                  className="flex-1 bg-slate-800 border border-slate-700 rounded-lg px-2 py-1.5 text-xs text-slate-200" />
              </div>
              <div className="flex items-center gap-2">
                <button type="button" disabled={busy} onClick={submitRevision}
                  className="px-3 py-1.5 bg-cyan-500/10 hover:bg-cyan-500/20 text-cyan-400 rounded-lg text-xs disabled:opacity-40">
                  {busy ? '提交中…' : '提交修订并重验'}
                </button>
                <button type="button" disabled={busy} onClick={() => { setEditing(false); setResult('') }}
                  className="px-3 py-1.5 text-slate-400 hover:text-slate-200 text-xs">取消</button>
              </div>
            </div>
          )}
          {result && <div className="text-xs text-slate-300 mt-2">{result}</div>}
        </div>
      )}

      {/* ⑥ 导出版本绑定：包可能早于当前版本，如实标注 */}
      {exportState && (
        <div className={box}>
          <div className={h}>导出</div>
          <ul className={li}>
            <li>· Markdown / PDF 按**当前版本**导出（响应头带版本号）</li>
            {exportState.package
              ? <li>· 交付包 {exportState.package}（生成于 {exportState.package_generated_at || '未知时间'}）</li>
              : <li>· 暂无交付包</li>}
            {exportState.package && (() => {
              // 版本对比统一用正文版号（与复核栏"本版"同一口径）；
              // 老后端没有这两个字段时退回谱系标识。
              const pkgV = exportState.package_body_version_id || exportState.manifest_version_id || ''
              const curV = exportState.current_body_version_id || exportState.current_version_id || ''
              return pkgV && curV && pkgV !== curV
                ? <li className="text-amber-400">· 包生成于 {pkgV.slice(0, 12)}，
                    当前版本 {curV.slice(0, 12)}——包不含最新修订，请重新导出</li>
                : null
            })()}
          </ul>
        </div>
      )}
      {sc.company && (
        <div className="text-xs text-slate-500">
          {sc.company}｜期间 {(sc.periods || []).join('、')}｜口径 {sc.caliber || '未声明'}｜
          资料截至 {sc.as_of || '未声明'}｜单位 {sc.unit || '见表中标注'}
        </div>
      )}
    </div>
  )
}
