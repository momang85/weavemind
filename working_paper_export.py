# -*- coding: utf-8 -*-
"""把底稿落到任务产物里（S1 收尾）。

为什么单独一步：`working_paper.py` 会算，但只有**落进任务工作区**才算交付的一部分——
用户要能拿到 `working_paper.csv/json`（可重算底稿 + 缺口表），而不是只在日志里看到结论。

出口（都写在任务的 project 目录，与 `financials.json` 同级）：
- `working_paper.json`：明细 + 派生 + 缺口 + 问题 + 请求契约 + 完备度；
- `working_paper.csv`：同一内容的三段式表格，便于人工核对与二次计算。

没有结构化财务时不产出底稿（返回 `skipped`）——**不编一份空底稿**冒充已复核。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from facts import parse_research_request
from workspace import task_project_dir
from working_paper import build_working_paper, paper_csv
import facts as _facts

logger = logging.getLogger(__name__)

PAPER_JSON = "working_paper.json"
PAPER_CSV = "working_paper.csv"


def _market_of(metadata: dict, resolution: dict | None) -> str:
    """市场：优先适配器/解析器给的语义值，其次按代码形状推断（cn/hk/us）。"""
    for src in (metadata or {}, resolution or {}):
        mk = str(src.get("market") or "").strip().lower()
        if mk in ("cn", "hk", "us"):
            return mk
        code = str(src.get("ticker") or src.get("code") or src.get("stock_code") or "")
        if code:
            up = code.upper()
            if up.endswith((".SZ", ".SH", ".SS")):
                return "cn"
            if up.endswith(".HK"):
                return "hk"
            if up.isalpha() and 1 <= len(up) <= 5:
                return "us"
    return ""


def write_working_paper(task_id: str, goal: str, *,
                        project: str | None = None) -> dict:
    """读任务的结构化财务 → 生成事实与底稿 → 落盘两件套。

    返回 {ok, skipped?, rows, derived, gaps, problems, files}；任何异常都不抛出
    （底稿是交付增强，不能拖垮主线），失败时记日志并把原因放进返回值。
    """
    try:
        proj = task_project_dir(task_id, project) if project else task_project_dir(task_id)
        fin_path = Path(proj) / "financials.json"
        if not fin_path.exists():
            return {"ok": False, "skipped": True, "reason": "没有结构化财务，不产出底稿"}
        payload = json.loads(fin_path.read_text(encoding="utf-8"))
        md = dict(payload.get("metadata") or {})
        if str(payload.get("source") or "") == "multi_entity":
            md = dict((payload.get("companies") or [{}])[0].get("metadata") or {})
        resolution = payload.get("resolution") or {}

        request = parse_research_request(
            goal,
            company=str(md.get("company") or md.get("name") or ""),
            company_id=str(md.get("ticker") or md.get("code") or md.get("stock_code") or ""),
            market=_market_of(md, resolution),
        )
        facts = _facts.facts_from_financials(payload)
        paper = build_working_paper(facts, request)

        out_json = Path(proj) / PAPER_JSON
        out_csv = Path(proj) / PAPER_CSV
        out_json.write_text(
            json.dumps(paper.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        out_csv.write_text(paper_csv(paper), encoding="utf-8")
        logger.info("底稿已落盘（task=%s）：%d 条事实、%d 条同比、%d 项缺口、%d 项问题",
                    task_id, len(paper.rows), len(paper.derived),
                    len(paper.gaps), len(paper.problems))
        return {
            "ok": True,
            "rows": len(paper.rows), "derived": len(paper.derived),
            "gaps": paper.gaps, "problems": [p.as_dict() for p in paper.problems],
            "completeness": paper.completeness, "paper_ok": paper.ok,
            "files": {"json": str(out_json), "csv": str(out_csv)},
        }
    except Exception as exc:                     # noqa: BLE001 - 交付增强不得拖垮主线
        logger.warning("底稿产出失败（task=%s）：%s", task_id, str(exc)[:160])
        return {"ok": False, "reason": str(exc)[:200]}


def gaps_note(result: dict) -> str:
    """把缺口/问题整理成交付物里可读的一段（缺证据要说出来，不能只留文件）。"""
    if not result or not result.get("ok"):
        return ""
    gaps = result.get("gaps") or []
    problems = result.get("problems") or []
    if not gaps and not problems:
        return ""
    lines = ["## 底稿缺口与待核验项", ""]
    for g in gaps:
        lines.append(f"- 缺口：{g.get('detail') or g}")
    for p in problems:
        lines.append(f"- 待核验：{p.get('detail') or p}")
    lines.append("")
    lines.append("> 以上项目未取得可核验证据，报告不得据此声称已达成；"
                 "底稿见 `working_paper.csv`（明细 / 派生 / 缺口三段）。")
    return "\n".join(lines)
