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

    def _recency_retrieved_at_check(project: Path) -> tuple[bool, str]:
        """P1-1：报告日期 vs 结构化数据 retrieved_at 矛盾检查。
        报告明确标注"数据截至/报告日期"且比 retrieved_at 早 >7 天 → FAIL，
        触发反思重做（修复模型回忆日期与结构化数据时间矛盾）。"""
        import re
        from datetime import date

        sd = project / "structured_data.json"
        if not sd.exists():
            return (True, "无结构化数据（跳过 retrieved_at 矛盾检查）")
        try:
            sd_data = json.loads(sd.read_text(encoding="utf-8"))
            retrieved_at = str(
                (sd_data.get("metadata") or {}).get("retrieved_at") or ""
            )
        except Exception:
            return (True, "结构化数据解析失败（跳过 retrieved_at 矛盾检查）")
        m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", retrieved_at)
        if not m:
            return (True, "结构化数据无 retrieved_at 日期（跳过矛盾检查）")
        try:
            retrieved = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return (True, "retrieved_at 日期无法解析（跳过矛盾检查）")
        report = project.parent / "reports" / "report.md"
        if not report.exists():
            return (True, "无报告文件（跳过报告日期矛盾检查）")
        try:
            txt = report.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return (True, "报告读取失败（跳过报告日期矛盾检查）")
        # 只认"数据截至/报告日期/更新日期/生成日期"等标签后的日期，
        # 避免把数据表里的历史序列日期误当成报告日期
        pat = re.compile(
            r"(?:数据截至|数据截止|报告日期|报告时间|更新日期|更新时间|"
            r"生成日期|检索时间|截至)"
            r"[^0-9年/.\-]{0,12}?"
            r"(20\d{2})\s*[年./\-]\s*(\d{1,2})\s*(?:月|[./\-])?\s*(\d{1,2})?\s*日?"
        )
        hit = pat.search(txt)
        if not hit:
            return (True, "报告未标注数据截至/报告日期（无法核对 retrieved_at 矛盾）")
        try:
            report_date = date(
                int(hit.group(1)), int(hit.group(2)), int(hit.group(3) or 1)
            )
        except ValueError:
            return (True, "报告日期无法解析（跳过矛盾检查）")
        gap = (retrieved - report_date).days
        if gap > 7:
            return (
                False,
                f"报告日期 {report_date.isoformat()} 明显早于数据获取时间 "
                f"{retrieved.isoformat()}（差 {gap} 天 >7 天），"
                "必须按结构化数据 retrieved_at 重写报告日期",
            )
        return (
            True,
            f"报告日期 {report_date.isoformat()} 与数据获取时间 "
            f"{retrieved.isoformat()} 一致（差 {gap} 天）",
        )

    def _recency_check(project: Path, task_id: str, goal: str):
        """时效性审查（修复"最新财报返回旧年份" + P1-1"报告日期早于 retrieved_at"）：
        目标含"最新/最近/当前/现在"时，报告日期不得明显早于结构化数据
        retrieved_at（>7 天 → FAIL 触发反思）；"最新/最近"类目标另要求
        检索结果与报告含近期年份（当前年-1 起）。"""
        import re
        import time
        g = str(goal or "")
        freshness_requested = any(
            k in g for k in ("最新", "最近", "最新季度", "最新一期",
                             "current", "当前", "现在")
        )
        if freshness_requested:
            ok, detail = _recency_retrieved_at_check(project)
            if not ok:
                return (False, detail)
        if not any(k in g for k in (
            "最新", "最近", "最新季度", "最新一期", "latest", "current",
        )):
            if freshness_requested:
                return (True, "目标含当前/现在：已核对数据获取时间与报告日期")
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
