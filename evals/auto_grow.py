# -*- coding: utf-8 -*-
"""评测集自动生长（对标标准 C3-3.6 评测驱动开发，Roadmap 余项①）。

真实任务验收 fail 时，把 (目标, 缺口, 报告) 自动沉淀为评测案例：
- 去重：goal 归一化后与已有案例比对，重复不写；
- 上限：EVAL_AUTO_GROWN_MAX（默认 200）防止评测集无限膨胀；
- 静默：任何失败只记日志，绝不影响任务主线；
- 产出：evals/cases/auto_grown.json，与现有评测集同 schema，
  evals.run.load_cases() 的 glob("*.json") 自动纳入。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

AUTO_GROWN_FILE = "auto_grown.json"
AUTO_GROWN_MAX = int(os.environ.get("EVAL_AUTO_GROWN_MAX", "200"))

# 缺口 → 评测要点 的启发式改写（保留原文，避免丢失信息）
_GAP_TO_POINT = [
    (re.compile(r"溯源率.*低于|数字.*无法溯源|不可溯源", re.S), "报告中的关键数字必须可溯源至检索/结构化数据来源"),
    (re.compile(r"主体归属|主体.*污染|串味", re.S), "报告数据必须归属正确主体，不得与其他公司数据串味"),
    (re.compile(r"来源标注|来源声明|来源.*模糊|来源.*缺失", re.S), "每条数据声明必须诚实标注来源，不得模糊或编造来源"),
    (re.compile(r"缺口|缺失|缺少|未覆盖", re.S), "报告必须覆盖验收缺口项，不得遗漏关键维度"),
    (re.compile(r"图表|QA|渲染|字号|重叠", re.S), "图表必须通过质量检查（无重叠、字号可读、渲染正确）"),
    (re.compile(r"规模|top_n|前.*%|排行", re.S), "排行/统计类结果必须与请求规模口径一致（如前5%）"),
]


def _normalize_goal(goal: str) -> str:
    """goal 归一化：去空白与标点，用于去重比较。"""
    return re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’()（）\[\]【】\-_/\\]+", "", str(goal or "")).lower()


def _case_file() -> Path:
    return Path(__file__).parent / "cases" / AUTO_GROWN_FILE


def _load_existing(path: Path | None = None) -> list[dict]:
    p = path or _case_file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return list(data.get("cases") or [])
    except Exception as exc:
        logger.warning("auto_grown 读取失败（按空处理）: %s", str(exc)[:120])
        return []


def _gap_to_points(gaps: list) -> list[str]:
    """把验收缺口改写成评测要点（正向陈述），同时保留原文作为依据。"""
    points: list[str] = []
    for g in gaps or []:
        text = str(g or "").strip()
        if not text:
            continue
        rewritten = False
        for pat, point in _GAP_TO_POINT:
            if pat.search(text):
                if point not in points:
                    points.append(point)
                rewritten = True
        if not rewritten:
            points.append(f"必须解决：{text}")
    return points


def harvest_failure(
    task_id: str,
    goal: str,
    acceptance_result: dict | None,
    report: str = "",
    output_path: str | None = None,
) -> dict:
    """验收 fail → 沉淀评测案例。返回 {harvested, case_id, reason}。

    - 仅 overall == "fail" 且任务 id 形如 ui-* 的真实任务才沉淀；
    - 去重（归一化 goal）；上限保护；任何异常静默降级。
    """
    result = {"harvested": False, "case_id": None, "reason": ""}
    try:
        if not acceptance_result or acceptance_result.get("overall") != "fail":
            return result
        if not str(task_id or "").startswith("ui-"):
            result["reason"] = "非真实任务（未以 ui- 开头）"
            return result
        g = str(goal or "").strip()
        if len(g) < 8:
            result["reason"] = "目标过短，无沉淀价值"
            return result

        p = Path(output_path) if output_path else _case_file()
        existing = _load_existing(p)
        norm = _normalize_goal(g)
        if any(_normalize_goal(c.get("goal", "")) == norm for c in existing):
            result["reason"] = "已存在同目标案例"
            return result
        if len(existing) >= AUTO_GROWN_MAX:
            result["reason"] = f"评测集已达上限 {AUTO_GROWN_MAX}"
            return result

        gaps = list(acceptance_result.get("gaps") or [])
        points = _gap_to_points(gaps)
        if not points:
            result["reason"] = "缺口为空，无可沉淀要点"
            return result

        case_id = f"ag-{len(existing) + 1:03d}"
        new_case = {
            "id": case_id,
            "goal": g[:300],
            "expected_deliverable": "验收复盘报告（Markdown，含来源标注与图表）",
            "ground_truth_points": points,
            "rubric": {
                "验收通过": "必须补齐全部验收缺口（overall=pass）",
                "可溯源": "报告关键数字可追溯至来源",
            },
            "source_task": task_id,
            "gaps": [str(x)[:300] for x in gaps][:10],
            "report_excerpt": str(report or "")[:1200],
            "harvested_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        }
        existing.append(new_case)
        payload = {
            "type": "auto_grown",
            "description": "真实任务验收 fail 自动沉淀的评测案例（评测驱动闭环）",
            "cases": existing,
        }
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        result.update({"harvested": True, "case_id": case_id, "reason": "ok"})
        logger.info("评测集自动生长: task=%s → case=%s (%d 条)", task_id, case_id, len(existing))
    except Exception as exc:  # 静默降级，绝不干扰任务主线
        result["reason"] = f"异常: {str(exc)[:100]}"
        logger.warning("harvest_failure 异常（静默）: %s", result["reason"])
    return result


if __name__ == "__main__":
    import sys
    # 调试：python evals/auto_grow.py <task_id> <goal> <overall>
    if len(sys.argv) >= 4:
        r = harvest_failure(
            sys.argv[1], sys.argv[2],
            {"overall": sys.argv[3], "gaps": sys.argv[4:]},
        )
        print(json.dumps(r, ensure_ascii=False))
    else:
        print("用法: python evals/auto_grow.py <task_id> <goal> <pass|fail> [gap...]")
