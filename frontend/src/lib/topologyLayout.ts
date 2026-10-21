// 智能体拓扑图（Canvas）的**几何与标签布局**——从组件里抽成纯函数，便于断言。
//
// 实机反例（2026-10-22，桌面与移动端都出现）：拓扑内圈标签互相叠印，渲染出
// `datalaanalymodel_train`、`webfetchwo`、`fileiowork`、`codeexecwo`、`rengengene`、
// `search age`、`content su`、`packaging` 等不可读字符串。
// 根因三条：
//   ① 标签用 `agent_id.slice(0, 10)`（10 个英文字符 ≈ 70px），而周长上相邻节点仅有约 51px 间距；
//   ② 标签一律水平居中画在节点正下方，圆环左右两侧的标签会沿切线方向互相压叠；
//   ③ 没有任何碰撞检测，"叠了就叠了"。
// 修法：标签改用能力中文短名（4 字 ≈ 48px）+ **沿半径向外**摆放（相邻标签自然发散）
//      + 放置前做碰撞检测，撞了就沿半径方向再推远一点，仍放不下才隐藏（宁可少画不留乱码）。

import { CAPABILITY_ZH } from './agentLabel.ts'

/** 画布高度（原来 260 太扁，圆环半径被压到 90，节点自然挤在一起）。 */
export const TOPO_HEIGHT = 320
/** 圆环留白：半径 = min(w, h)/2 - PAD。 */
export const TOPO_PAD = 62
export const LABEL_FONT = '12px sans-serif'

export interface TopoNode { x: number; y: number; r: number; id: string }

export interface TopoLabel {
  id: string
  text: string
  x: number
  y: number
  align: 'left' | 'center' | 'right'
  baseline: 'top' | 'middle' | 'bottom'
  w: number
  h: number
}

export interface TopoLayout {
  placed: TopoLabel[]
  /** 放不下而被隐藏的标签：由调用方在悬停时兜底显示，绝不叠印。 */
  hidden: string[]
  /** 被推到第二位置（远离原半径）的标签，用于排查"标签离节点有点远"。 */
  nudged: string[]
}

/** 标签文字：优先能力中文短名（与智能体卡片同一口径），否则退回可读的机器名。 */
export function shortAgentLabel(agent: { agent_id?: string; capabilities?: string } | null | undefined): string {
  const caps = String(agent?.capabilities || '').split(',').map(c => c.trim()).filter(Boolean)
  for (const c of caps) if (CAPABILITY_ZH[c]) return CAPABILITY_ZH[c]
  const raw = String(agent?.agent_id || '').trim()
  if (!raw) return '未命名'
  // 机器名兜底：下划线转空格、worker/agent 后缀前补空格，再压到 8 字以内
  let readable = raw.replace(/_+/g, ' ')
    .replace(/([a-z])(worker|agent|manager|runner)\b/gi, '$1 $2')
    .replace(/\s+/g, ' ').trim()
  // 太长时先丢掉末尾的通用角色词（`dataloader worker` → `dataloader`），再压长度
  if (readable.length > 12) {
    readable = readable.replace(/\s+(worker|agent|manager|runner)$/i, '').trim()
  }
  return readable.length > 12 ? readable.slice(0, 11) + '…' : readable
}

/** 圆环节点位置（角度从正上方开始顺时针均匀分布）。 */
export function topologyNodes(count: number, w: number, h: number, cx?: number, cy?: number): TopoNode[] {
  const n = Math.max(0, Math.floor(count))
  if (n === 0) return []
  const centerX = cx ?? w / 2
  const centerY = cy ?? h / 2
  const radius = Math.max(30, Math.min(w, h) / 2 - TOPO_PAD)
  const out: TopoNode[] = []
  for (let i = 0; i < n; i++) {
    const angle = (2 * Math.PI * i) / n - Math.PI / 2
    out.push({
      x: centerX + radius * Math.cos(angle),
      y: centerY + radius * Math.sin(angle),
      r: 14,
      id: `#${i}`,
    })
  }
  return out
}

/**
 * 沿半径向外摆放标签并做碰撞消解。
 *
 * @param measure 文字宽度测量函数（Canvas 的 `ctx.measureText(...).width`）；注入以便测试。
 */
export function placeTopologyLabels(
  nodes: TopoNode[],
  labelOf: (node: TopoNode, index: number) => string,
  cx: number, cy: number, w: number, h: number,
  measure: (text: string) => number,
  lineHeight = 14,
): TopoLayout {
  const placed: TopoLabel[] = []
  const hidden: string[] = []
  const nudged: string[] = []
  const GAP = 7           // 标签与节点边缘的间距
  const STEP = lineHeight + 2  // 碰撞后沿半径推远的步长

  nodes.forEach((node, i) => {
    const text = String(labelOf(node, i) || '').trim()
    if (!text) { hidden.push(node.id); return }
    let dx = node.x - cx
    let dy = node.y - cy
    const len = Math.hypot(dx, dy)
    if (len < 1e-6) { dx = 0; dy = -1 } else { dx /= len; dy /= len }

    const tw = measure(text)
    const align: TopoLabel['align'] = dx > 0.25 ? 'left' : dx < -0.25 ? 'right' : 'center'
    const baseline: TopoLabel['baseline'] = dy > 0.25 ? 'top' : dy < -0.25 ? 'bottom' : 'middle'

    // 标签锚点：从左对齐 / 右对齐 / 居中对齐换算成包围盒左上角
    const boxOf = (dist: number): TopoLabel => {
      const ax = node.x + dx * (node.r + GAP + dist)
      const ay = node.y + dy * (node.r + GAP + dist)
      const x = align === 'left' ? ax : align === 'right' ? ax - tw : ax - tw / 2
      const y = baseline === 'top' ? ay : baseline === 'bottom' ? ay - lineHeight : ay - lineHeight / 2
      return { id: node.id, text, x, y, align, baseline, w: tw, h: lineHeight }
    }
    const overlaps = (b: TopoLabel) => placed.some(p =>
      b.x < p.x + p.w + 2 && p.x < b.x + b.w + 2 &&
      b.y < p.y + p.h + 2 && p.y < b.y + b.h + 2)
    const inCanvas = (b: TopoLabel) => b.x >= 2 && b.y >= 2 && b.x + b.w <= w - 2 && b.y + b.h <= h - 2

    let chosen: TopoLabel | null = null
    for (let step = 0; step <= 4; step++) {
      const cand = boxOf(step * STEP)
      if (!inCanvas(cand)) continue
      if (!overlaps(cand)) { chosen = cand; if (step > 0) nudged.push(node.id); break }
    }
    if (chosen) placed.push(chosen)
    else hidden.push(node.id)   // 宁可少画一个标签，也不叠印出乱码
  })

  return { placed, hidden, nudged }
}
