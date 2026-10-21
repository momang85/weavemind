import { useEffect, useRef, useState } from 'react'
import { useTaskStore } from '../stores/useTaskStore'
import {
  LABEL_FONT, TOPO_HEIGHT, placeTopologyLabels, shortAgentLabel, topologyNodes,
} from '../lib/topologyLayout'

const COLORS = ['#38bdf8', '#6ee7b7', '#fbbf24', '#fca5a5', '#c084fc', '#f472b6', '#818cf8']

export default function AgentTopology() {
  const agents = useTaskStore(s => s.agents)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const positionsRef = useRef<ReturnType<typeof topologyNodes>>([])
  const [hovered, setHovered] = useState<string | null>(null)
  const [hiddenCount, setHiddenCount] = useState(0)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const w = canvas.width = canvas.parentElement?.clientWidth || 400
    const h = canvas.height = TOPO_HEIGHT
    const cx = w / 2, cy = h / 2
    ctx.clearRect(0, 0, w, h)

    const HUB_R = 22

    // 几何来自 lib/topologyLayout（可测）：半径随画布缩放，节点按角度均布
    const nodes = topologyNodes(agents.length, w, h, cx, cy)
    nodes.forEach((node, i) => { node.r = agents[i].status?.includes('active') ? 18 : 14 })
    positionsRef.current = nodes

    // 标签：能力中文短名 + 沿半径向外 + 碰撞检测（撞了就推远，放不下就不画）
    ctx.font = LABEL_FONT
    const layout = placeTopologyLabels(
      nodes, (_n, i) => shortAgentLabel(agents[i]), cx, cy, w, h,
      t => ctx.measureText(t).width,
    )
    setHiddenCount(layout.hidden.length)

    nodes.forEach((node, i) => {
      const a = agents[i]
      const busy = a.status?.includes('active')

      // 连线：从中心圆**边缘**起画（原来从圆心起画，辐条横穿"中枢"二字把它盖住）
      const ang = Math.atan2(node.y - cy, node.x - cx)
      ctx.beginPath()
      ctx.moveTo(cx + Math.cos(ang) * HUB_R, cy + Math.sin(ang) * HUB_R)
      ctx.lineTo(node.x, node.y)
      ctx.strokeStyle = busy ? '#38bdf8' : '#334155'
      ctx.lineWidth = busy ? 1.5 : 0.5; ctx.stroke()

      // 节点
      ctx.beginPath(); ctx.arc(node.x, node.y, node.r, 0, Math.PI * 2)
      ctx.fillStyle = COLORS[i % COLORS.length]; ctx.fill()
      if (hovered === a.agent_id) {
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.stroke()
      }

      // 忙碌指示
      if (busy) {
        ctx.beginPath(); ctx.arc(node.x, node.y, node.r + 3, 0, Math.PI * 2)
        ctx.strokeStyle = '#38bdf8'; ctx.lineWidth = 1
        ctx.setLineDash([3, 2]); ctx.stroke(); ctx.setLineDash([])
      }
    })

    // 中枢节点：挪到连线之后画，避免被辐条压住
    ctx.beginPath(); ctx.arc(cx, cy, HUB_R, 0, Math.PI * 2)
    ctx.fillStyle = '#38bdf8'; ctx.fill()
    ctx.strokeStyle = '#0f172a'; ctx.lineWidth = 2; ctx.stroke()
    ctx.fillStyle = '#fff'; ctx.font = 'bold 12px sans-serif'
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'
    ctx.fillText('中枢', cx, cy)

    // 标签最后画，保证压在连线上方、清晰可读
    ctx.font = LABEL_FONT
    for (const l of layout.placed) {
      const owner = nodes[Number(l.id.slice(1))]
      const isHovered = owner && agents[Number(l.id.slice(1))]?.agent_id === hovered
      ctx.fillStyle = isHovered ? '#e2e8f0' : '#94a3b8'
      ctx.textAlign = l.align
      ctx.textBaseline = l.baseline
      ctx.fillText(l.text, l.align === 'left' ? l.x : l.align === 'right' ? l.x + l.w : l.x + l.w / 2,
        l.baseline === 'top' ? l.y : l.baseline === 'bottom' ? l.y + l.h : l.y + l.h / 2)
    }
  }, [agents, hovered])

  const handleMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const rect = canvas.getBoundingClientRect()
    // 画布按 CSS 宽度铺满，命中检测要按缩放比换算回画布坐标
    const scale = rect.width ? canvas.width / rect.width : 1
    const mx = (e.clientX - rect.left) * scale
    const my = (e.clientY - rect.top) * scale
    // positions 与 agents 同序，命中下标即 agent 下标
    let hit = -1
    positionsRef.current.forEach((p, i) => {
      if (hit < 0 && Math.hypot(mx - p.x, my - p.y) <= p.r + 4) hit = i
    })
    setHovered(hit >= 0 ? (agents[hit]?.agent_id ?? null) : null)
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
          ? <span className="text-cyan-400">{hoveredAgent.agent_id} · {hoveredAgent.status || '未知'}</span>
          : <span>
              {agents.length} 个智能体在线
              {hiddenCount > 0 && `（${hiddenCount} 个标签画布放不下已省略，鼠标移到节点上可看名称）`}
            </span>}
      </div>
    </div>
  )
}
