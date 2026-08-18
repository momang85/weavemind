# -*- coding: utf-8 -*-
"""图表视觉质量 QA + 自动调整闭环（无多模态 LLM 的确定性替代）。

LLM 是纯文本模型，看不到渲染出的图；但图的质量问题（标签重叠/图例遮挡/
字号过小/不适合打印）可以【程序化检测 + 自动修复】：
    渲染 → matplotlib renderer 实测文本包围盒 → 检测问题 →
    自动调整（旋转标签/图例外置/加大字号/加宽画布）→ 重渲染 → 直到通过或达上限。

供 render_charts.py / make_charts.py 调用：
    from chart_qa import render_with_qa
    issues = render_with_qa(fig, ax, path)
    # issues = 最终残留问题（空列表 = 图表质量达标）
"""

import logging

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)

MIN_TICK_FONT = 8
MIN_LABEL_FONT = 10
OVERLAP_PX = 3          # 相邻标签重叠超过 3px 判定为重叠
LEGEND_CORE_RATIO = 0.38  # 图例 bbox 与轴中央区域相交比例阈值


def _tick_overlap(labels) -> list[dict]:
    """检测同一轴相邻刻度标签的包围盒重叠。labels: [(bbox, text)] 按轴顺序。"""
    issues = []
    boxes = [(l.get_window_extent(), str(l.get_text())) for l in labels if l.get_text()]
    boxes.sort(key=lambda b: b[0].x0 if b[0].width > b[0].height else b[0].y0)
    for i in range(1, len(boxes)):
        b0, t0 = boxes[i - 1]
        b1, t1 = boxes[i]
        inter_w = min(b0.x1, b1.x1) - max(b0.x0, b1.x0)
        inter_h = min(b0.y1, b1.y1) - max(b0.y0, b1.y0)
        if inter_w > OVERLAP_PX and inter_h > OVERLAP_PX * 0.5:
            issues.append({
                "type": "tick_overlap",
                "axis": "x" if b0.width > b0.height else "y",
                "detail": f"标签重叠: {t0[:12]} 与 {t1[:12]}",
            })
    return issues


def check_figure(fig, ax, renderer) -> list[dict]:
    """返回图表质量 issues（空列表 = 通过）。"""
    issues: list[dict] = []
    try:
        issues += _tick_overlap(ax.get_xticklabels())
        issues += _tick_overlap(ax.get_yticklabels())
    except Exception:
        pass
    # 字号检查
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        try:
            if lbl.get_text() and lbl.get_fontsize() < MIN_TICK_FONT:
                issues.append({"type": "font_too_small", "kind": "tick",
                               "detail": f"刻度字号 {lbl.get_fontsize()} < {MIN_TICK_FONT}"})
                break
        except Exception:
            pass
    for lbl in (ax.xaxis.label, ax.yaxis.label):
        try:
            if lbl.get_text() and lbl.get_fontsize() < MIN_LABEL_FONT:
                issues.append({"type": "font_too_small", "kind": "axis_label",
                               "detail": f"轴标签字号 {lbl.get_fontsize()} < {MIN_LABEL_FONT}"})
        except Exception:
            pass
    # 图例遮挡轴中央数据区
    try:
        leg = ax.get_legend()
        if leg is not None:
            leg_bb = leg.get_window_extent(renderer)
            ax_bb = ax.get_window_extent(renderer)
            cx = ax_bb.x0 + (ax_bb.x1 - ax_bb.x0) * LEGEND_CORE_RATIO
            cy = ax_bb.y0 + (ax_bb.y1 - ax_bb.y0) * LEGEND_CORE_RATIO
            core_bb = matplotlib.transforms.Bbox.from_extents(
                cx, cy,
                ax_bb.x1 - (ax_bb.x1 - ax_bb.x0) * LEGEND_CORE_RATIO,
                ax_bb.y1 - (ax_bb.y1 - ax_bb.y0) * LEGEND_CORE_RATIO,
            )
            inter = leg_bb.intersection(core_bb)
            if inter is not None and inter.width > 0 and inter.height > 0:
                issues.append({"type": "legend_overlap",
                               "detail": "图例遮挡轴中央数据区"})
    except Exception:
        pass
    return issues


def _apply_fixes(fig, ax, issues: list[dict]) -> bool:
    """对问题应用确定性修复；返回是否发生了改动。"""
    changed = False
    for it in issues:
        if it["type"] == "tick_overlap":
            axis = it.get("axis", "x")
            if axis == "x":
                for lbl in ax.get_xticklabels():
                    lbl.set_rotation(30)
                    lbl.set_ha("right")
            else:
                for lbl in ax.get_yticklabels():
                    lbl.set_rotation(0)
            # 加宽画布给旋转后的标签留空间
            w, h = fig.get_size_inches()
            fig.set_size_inches(w + 1.2 if axis == "x" else w, h + 1.0 if axis == "y" else h)
            changed = True
        elif it["type"] == "font_too_small":
            target = MIN_LABEL_FONT if it.get("kind") == "axis_label" else MIN_TICK_FONT
            for lbl in (ax.xaxis.label, ax.yaxis.label, *ax.get_xticklabels(), *ax.get_yticklabels()):
                if lbl.get_text():
                    lbl.set_fontsize(max(lbl.get_fontsize(), target))
            changed = True
        elif it["type"] == "legend_overlap":
            leg = ax.get_legend()
            if leg is not None:
                leg.set_bbox_to_anchor((1.02, 1))
                leg.set_loc("upper left")
                w, _ = fig.get_size_inches()
                fig.set_size_inches(w + 1.5, fig.get_size_inches()[1])
                changed = True
    if changed:
        try:
            fig.tight_layout()
        except Exception:
            pass
    return changed


def render_with_qa(fig, ax, path, max_rounds: int = 3, dpi: int = 110) -> list[dict]:
    """渲染 + QA + 自动修复循环；返回最终残留 issues（空 = 达标）。"""
    residual: list[dict] = []
    for rnd in range(max_rounds + 1):
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        issues = check_figure(fig, ax, renderer)
        if not issues:
            residual = []
            break
        if rnd >= max_rounds:
            residual = issues
            break
        fixed = _apply_fixes(fig, ax, issues)
        logger.info("chart_qa round %d: %d issues, fixed=%s -> %s",
                    rnd, len(issues), fixed,
                    "；".join(i["detail"] for i in issues)[:120])
        if not fixed:
            residual = issues
            break
    try:
        fig.savefig(path, dpi=dpi, bbox_inches="tight")
    except Exception:
        fig.savefig(path, dpi=dpi)
    return residual
