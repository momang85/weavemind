# -*- coding: utf-8 -*-
"""验证器注册与运行。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_VALIDATORS: dict[str, dict] = {}


def register(name: str, applies: set[str] | None, fn) -> None:
    """注册验证器。applies=None 表示通用；否则按能力命中。"""
    _VALIDATORS[name] = {"applies": applies, "fn": fn}


def list_validators() -> list[str]:
    return list(_VALIDATORS)


def run_for_task(task_id: str, goal: str, caps: list[str]) -> list[dict]:
    """对任务运行适用验证器，返回 [{name, ok, detail}]。任何验证器异常都不阻断。"""
    from workspace import task_project_dir

    project = task_project_dir(task_id)
    results: list[dict] = []
    for name, v in _VALIDATORS.items():
        if v["applies"] and not (set(caps) & v["applies"]):
            continue
        try:
            ok, detail = v["fn"](project, task_id, goal)
            results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
        except Exception as exc:
            results.append({"name": name, "ok": False, "detail": f"验证器异常: {exc}"[:200]})
    return results


def summary_text(results: list[dict]) -> str:
    if not results:
        return ""
    return "验证器结果：\n" + "\n".join(
        f"- {r['name']}: {'通过' if r['ok'] else '未通过'}（{r['detail']}）"
        for r in results
    )


# ---------------------------------------------------------------------------
# 内置验证器
# ---------------------------------------------------------------------------


def _init_defaults() -> None:
    from chart_specs import validate_specs

    def _code_deliverable(project: Path, task_id: str, goal: str):
        files = list(project.glob("*.py")) + list(project.glob("*.html"))
        fresh = [f for f in files if f.stat().st_mtime > (os.path.getmtime(project) - 7200)]
        return (bool(fresh), f"找到 {len(fresh)} 个代码交付物: {', '.join(f.name for f in fresh[:3])}")

    def _py_compile_all(project: Path, task_id: str, goal: str):
        import py_compile
        bad = []
        for p in project.glob("*.py"):
            if p.name.startswith(("_check_", "_smoke_", "render_charts", "make_charts")):
                continue
            try:
                py_compile.compile(str(p), doraise=True)
            except Exception as exc:
                bad.append(f"{p.name}: {exc}")
        return (not bad, "；".join(bad) or "全部 .py 编译通过")

    def _html_playable(project: Path, task_id: str, goal: str):
        htmls = [p for p in project.glob("*.html")]
        if not htmls:
            return (False, "无 HTML 交付物")
        p = htmls[0]
        text = p.read_text(encoding="utf-8", errors="replace")
        issues = []
        if "charset" not in text[:500] and '<meta charset="utf-8"' not in text:
            issues.append("缺少 <meta charset>")
        if "<script" not in text and "<canvas" not in text:
            issues.append("无脚本/画布，可能不可交互")
        return (not issues, "；".join(issues) or f"{p.name} 具备可玩要素")

    def _chart_spec_valid(project: Path, task_id: str, goal: str):
        cd = project / "chart_data.json"
        if not cd.exists():
            return (True, "无图表数据（不适用）")
        try:
            specs = json.loads(cd.read_text(encoding="utf-8")).get("charts", [])
            _, issues = validate_specs(specs)
            return (not issues, str(issues) or f"{len(specs)} 张图表规格全部有效")
        except Exception as exc:
            return (False, f"图表规格解析失败: {exc}")

    def _recency_check(project: Path, task_id: str, goal: str):
        """时效性审查（修复"最新财报返回旧年份"）：
        目标要求"最新/最近"时，检索结果与报告必须含近期年份（当前年-1 起），
        否则判定未通过并作为反思证据。"""
        import re
        import time
        g = str(goal or "")
        if not any(k in g for k in ("最新", "最近", "最新季度", "最新一期", "latest", "current")):
            return (True, "目标未要求最新，跳过")
        years: set[int] = set()
        sr = project / "search_results.json"
        if sr.exists():
            try:
                for it in json.loads(sr.read_text(encoding="utf-8")):
                    t = f"{it.get('title', '')} {it.get('snippet', '')}"
                    years.update(int(m) for m in re.findall(r"(20\d{2})", t))
            except Exception:
                pass
        report = project.parent / "reports" / "report.md"
        if report.exists():
            try:
                txt = report.read_text(encoding="utf-8", errors="replace")
                years.update(int(m) for m in re.findall(r"(20\d{2})", txt))
            except Exception:
                pass
        if not years:
            return (False, "检索结果与报告未识别到任何年份，时效性无法确认")
        cur = time.localtime().tm_year
        month = time.localtime().tm_mon
        # 当年 3 月起，"最新"必须含当年数据（如 2026-08 → 要求 2026）；
        # 1-2 月允许上一年（最新财报可能尚未发布当年）
        required = cur if month >= 3 else cur - 1
        recent = sorted(y for y in years if y >= required)
        if not recent:
            return (False, f"数据均为陈旧年份（{sorted(years)}，要求 ≥{required} 年信息），必须重检索最新资料")
        return (True, f"识别到近期年份 {recent}")

    def _completeness_check(project: Path, task_id: str, goal: str):
        """完整性审查（ReAct 兜底）：报告出现"待获取/数据未获取/缺失字段"等
        明确缺口标记时判定未通过，强制反思触发定向重检索，而不是交付缺数据报告。"""
        report = project.parent / "reports" / "report.md"
        if not report.exists():
            return (True, "无报告文件（不适用）")
        try:
            txt = report.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return (True, "报告读取失败（跳过）")
        markers = ("待获取", "数据未获取", "缺失字段清单", "需重新定向搜索", "需重跑搜索")
        hits = [m for m in markers if m in txt]
        if hits:
            return (False, f"报告存在核心数据缺口（{ '、'.join(hits) }），必须定向重检索后再交付")
        return (True, "报告无明确缺口标记")

    register("code_deliverable", {"code_execution", "file_io"}, _code_deliverable)
    register("py_compile_all", {"code_execution", "file_io"}, _py_compile_all)
    register("html_playable", {"code_execution"}, _html_playable)
    register("chart_spec_valid", {"content_summary", "report_generator"}, _chart_spec_valid)
    register("recency_check", {"web_search", "content_summary", "report_generator"}, _recency_check)
    register("completeness_check", {"content_summary", "report_generator"}, _completeness_check)


_init_defaults()
