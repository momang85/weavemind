import { useState, memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
// types used: TaskReport

import { useTaskStore } from '../stores/useTaskStore'
import {
  FileDown, Package, ScrollText, Clock, CheckCircle2,
  ChevronDown, ChevronRight, Award, Zap
} from 'lucide-react'

export default memo(function ReportViewer() {
  const { report, logs } = useTaskStore()
  const [showLogs, setShowLogs] = useState(false)
  const [expandedFiles, setExpandedFiles] = useState(false)

  if (!report) return null

  const s = report.stats || { totalSteps: report.steps.length, successSteps: 0, failedSteps: 0, duration: 0 }
  const rate = s.totalSteps > 0 ? Math.round((s.successSteps / s.totalSteps) * 100) : 100

  const downloadMarkdown = () => {
    const blob = new Blob([report.final_report], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = 'report.md'; a.click()
    URL.revokeObjectURL(url)
  }

  const downloadPDF = () => {
    const w = window.open('', '_blank')
    if (!w) return
    w.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Report</title>
<style>body{font-family:system-ui;max-width:800px;margin:40px auto;padding:20px;color:#1a1a2e;line-height:1.8}
h1,h2{color:#16213e} pre{background:#f5f5f5;padding:16px;border-radius:8px;overflow-x:auto}
code{background:#f0f0f0;padding:2px 6px;border-radius:4px} table{border-collapse:collapse;width:100%}
th,td{border:1px solid #ddd;padding:8px;text-align:left} th{background:#16213e;color:#fff}
</style></head><body><div id="content"></div></body></html>`)
    w.document.close()
    // Convert markdown to HTML inline
    const md = report.final_report
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      .replace(/^- (.+)$/gm, '<li>$1</li>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\n/g, '<br/>')
    w.document.getElementById('content')!.innerHTML = md
    w.print()
  }

  return (
    <div className="animate-fade-in space-y-5">
      {/* Stats bar */}
      <div className="grid grid-cols-4 gap-3">
        {[
          { icon: Clock, label: '耗时', value: `${s.duration}s`, color: 'text-slate-400' },
          { icon: CheckCircle2, label: '成功率', value: `${rate}%`, color: 'text-emerald-400' },
          { icon: Award, label: 'Steps', value: `${s.successSteps}/${s.totalSteps}`, color: 'text-cyan-400' },
          { icon: Zap, label: 'Files', value: `${report.files?.length ?? 0}`, color: 'text-violet-400' },
        ].map(item => (
          <div key={item.label} className="bg-slate-900 border border-slate-800 rounded-xl p-4 text-center">
            <item.icon className={`w-5 h-5 mx-auto mb-2 ${item.color}`} />
            <div className={`text-xl font-bold ${item.color}`}>{item.value}</div>
            <div className="text-slate-500 text-xs">{item.label}</div>
          </div>
        ))}
      </div>

      {/* Report card */}
      <div className="bg-slate-900 border border-emerald-500/20 rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-5 py-3 bg-emerald-500/5 border-b border-emerald-500/20">
          <div className="flex items-center gap-2.5">
            <CheckCircle2 className="w-5 h-5 text-emerald-400" />
            <span className="text-emerald-400 font-semibold text-sm">任务完成</span>
            <span className="text-slate-600 text-xs ml-1">— {report.summary}</span>
          </div>
        </div>
        <div className="p-6 text-sm leading-relaxed text-slate-300 max-w-none
          [&_h1]:text-xl [&_h1]:font-bold [&_h1]:text-slate-100 [&_h1]:mb-3
          [&_h2]:text-lg [&_h2]:font-semibold [&_h2]:text-cyan-400 [&_h2]:mt-6 [&_h2]:mb-3
          [&_h3]:text-base [&_h3]:font-medium [&_h3]:text-slate-200 [&_h3]:mb-2
          [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:space-y-1 [&_li]:text-slate-400
          [&_strong]:text-slate-200 [&_code]:text-cyan-400 [&_code]:bg-slate-800 [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:rounded [&_code]:text-xs
          [&_table]:w-full [&_table]:text-xs [&_th]:text-left [&_th]:text-slate-400 [&_th]:font-medium [&_th]:px-2 [&_th]:py-1 [&_th]:border-b [&_th]:border-slate-800
          [&_td]:px-2 [&_td]:py-1 [&_td]:border-b [&_td]:border-slate-800/50">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              code({ className, children, ...props }) {
                const match = /language-(\w+)/.exec(className || '')
                const inline = !match
                return !inline && match ? (
                  <SyntaxHighlighter style={oneDark} language={match[1]} PreTag="div"
                    customStyle={{ background: '#0f172a', borderRadius: '8px', padding: '16px', fontSize: '12px', margin: '8px 0' }}>
                    {String(children).replace(/\n$/, '')}
                  </SyntaxHighlighter>
                ) : (
                  <code className={className} {...props}>{children}</code>
                )
              }
            }}
          >
            {report.final_report}
          </ReactMarkdown>
        </div>
      </div>

      {/* Files section */}
      {report.files && report.files.length > 0 && (
        <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
          <button onClick={() => setExpandedFiles(!expandedFiles)}
            className="w-full flex items-center justify-between px-5 py-3 hover:bg-slate-800/30 transition-colors">
            <div className="flex items-center gap-2 text-sm text-slate-300">
              <Package className="w-4 h-4 text-violet-400" />
              生成文件 ({report.files.length})
            </div>
            {expandedFiles ? <ChevronDown className="w-4 h-4 text-slate-500" /> : <ChevronRight className="w-4 h-4 text-slate-500" />}
          </button>
          {expandedFiles && (
            <div className="px-5 pb-4 space-y-1">
              {report.files.map((f, i) => (
                <div key={i} className="flex items-center gap-2 px-3 py-1.5 text-xs font-mono text-slate-400 bg-slate-800/40 rounded">
                  <FileDown className="w-3 h-3 text-slate-500" /> {f}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Bottom actions */}
      <div className="flex gap-3 flex-wrap">
        <button onClick={downloadMarkdown}
          className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors">
          <FileDown className="w-4 h-4" /> 下载Markdown
        </button>
        <button onClick={downloadPDF}
          className="flex items-center gap-2 px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors">
          <FileDown className="w-4 h-4" /> 打印/PDF
        </button>
        <button onClick={() => setShowLogs(!showLogs)}
          className={`flex items-center gap-2 px-4 py-2.5 rounded-lg text-sm transition-colors ${
            showLogs ? 'bg-cyan-500/20 text-cyan-400' : 'bg-slate-800 hover:bg-slate-700 text-slate-300'
          }`}>
          <ScrollText className="w-4 h-4" /> {showLogs ? 'Hide' : 'View'} 完整日志 ({logs.length})
        </button>
      </div>

      {/* Inline logs */}
      {showLogs && (
        <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden animate-fade-in">
          <div className="px-5 py-3 border-b border-slate-800 text-sm text-slate-400 font-medium">执行日志</div>
          <div className="max-h-64 overflow-y-auto font-mono text-xs">
            {logs.map((l, i) => (
              <div key={l.id || i} className={`flex gap-3 px-4 py-1.5 border-b border-slate-800/30 ${
                l.type === 'error' ? 'bg-red-500/5' : l.type === 'review' ? 'bg-purple-500/5' : ''
              }`}>
                <span className="text-slate-600 shrink-0 w-16">{l.timestamp.slice(0, 8)}</span>
                <span className={`shrink-0 w-12 text-xs ${
                  l.type === 'error' ? 'text-red-400' : l.type === 'plan' ? 'text-blue-400' :
                  l.type === 'review' ? 'text-purple-400' : l.type === 'dispatch' ? 'text-cyan-400' :
                  l.type === 'memory' ? 'text-amber-400' : 'text-slate-500'
                }`}>{l.type}</span>
                <span className="text-slate-400">{l.message}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
})
