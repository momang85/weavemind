import { useEffect, useRef, useState } from 'react'
import { useTaskStore } from '../stores/useTaskStore'

const COLORS = ['#38bdf8', '#6ee7b7', '#fbbf24', '#fca5a5', '#c084fc', '#f472b6', '#818cf8']

interface NodePos {
  x: number; y: number; r: number; id: string
}

export default function AgentTopology() {
  const agents = useTaskStore(s => s.agents)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const positionsRef = useRef<NodePos[]>([])
  const [hovered, setHovered] = useState<string | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const w = canvas.width = canvas.parentElement?.clientWidth || 400
    const h = canvas.height = 260
    const cx = w / 2, cy = h / 2
    ctx.clearRect(0, 0, w, h)

    // 中枢节点
    ctx.beginPath(); ctx.arc(cx, cy, 22, 0, Math.PI * 2)
    ctx.fillStyle = '#38bdf8'; ctx.fill()
    ctx.strokeStyle = '#0f172a'; ctx.lineWidth = 2; ctx.stroke()
    ctx.fillStyle = '#fff'; ctx.font = 'bold 10px sans-serif'
    ctx.textAlign = 'center'; ctx.fillText('中枢', cx, cy + 4)

    const n = agents.length
    const radius = Math.min(w, h) / 2 - 40
    const positions: NodePos[] = []

    agents.forEach((a, i) => {
      const angle = (2 * Math.PI * i) / n - Math.PI / 2
      const x = cx + radius * Math.cos(angle)
      const y = cy + radius * Math.sin(angle)
      const busy = a.status?.includes('active')
      const size = busy ? 18 : 14
      positions.push({ x, y, r: size, id: a.agent_id })

      // 连线
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(x, y)
      ctx.strokeStyle = busy ? '#38bdf8' : '#334155'
      ctx.lineWidth = busy ? 1.5 : 0.5; ctx.stroke()

      // 节点
      ctx.beginPath(); ctx.arc(x, y, size, 0, Math.PI * 2)
      ctx.fillStyle = COLORS[i % COLORS.length]; ctx.fill()
      if (hovered === a.agent_id) {
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.stroke()
      }

      // 标签
      ctx.fillStyle = '#94a3b8'; ctx.font = '9px sans-serif'
      ctx.textAlign = 'center'
      ctx.fillText(a.agent_id.replace('_', ' ').slice(0, 10), x, y + size + 12)

      // 忙碌指示
      if (busy) {
        ctx.beginPath(); ctx.arc(x, y, size + 3, 0, Math.PI * 2)
        ctx.strokeStyle = '#38bdf8'; ctx.lineWidth = 1
        ctx.setLineDash([3, 2]); ctx.stroke(); ctx.setLineDash([])
      }
    })

    positionsRef.current = positions
  }, [agents, hovered])

  const handleMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    const mx = e.clientX - rect.left
    const my = e.clientY - rect.top
    let found: string | null = null
    for (const p of positionsRef.current) {
      if (Math.hypot(mx - p.x, my - p.y) <= p.r + 4) {
        found = p.id
        break
      }
    }
    setHovered(found)
  }

  const hoveredAgent = agents.find(a => a.agent_id === hovered) || null

  return (
    <div className="relative">
      <canvas
        ref={canvasRef}
        className="w-full bg-gray-900/50 rounded-lg cursor-pointer"
        onMouseMove={handleMove}
        onMouseLeave={() => setHovered(null)}
      />
      <div className="text-center text-xs text-gray-500 mt-1">
        {hoveredAgent
          ? <span className="text-cyan-400">{hoveredAgent.agent_id} · {hoveredAgent.status || 'unknown'}</span>
          : `${agents.length} agents active`}
      </div>
    </div>
  )
}
