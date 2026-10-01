# -*- coding: utf-8 -*-
"""X0 证据：一次有限交付收口的**可核对清单**（边界、方向、角色、切法、版面）。

为什么单独一份：阶段X §3 的可见结果分散在两个正常任务的产物里（正文 markdown、PDF 版面、
另附的 `analysis_detail.md` 与包内清单）。本脚本**只读**这些产物，逐条判定"做到了没有"，
并把读数写进 `docs/evidence/x0_delivery_closure.json`——不重跑模型、不重写报告。

前置：先跑 `scripts/v0_yanghe_normal_task.py` 与 `scripts/v0_yanghe_normal_task.py --case sany`
（两个隔离正常任务会各自写出 report.md / report.pdf / analysis_detail.md 与证据 JSON）。

判定项（与架构复核 `20261001-W-architecture-review.md` 的表格一一对应）：
1. 三项主判断在**前两页**，且含销量 −16.30%／推算吨价 +3.93%（标推算身份）／库存 +16.38%／产量 −8.40%；
2. 不再虚列"吨价输入缺口"（原始披露 facts/derived 真的进了判断）；
3. 现金方向：洋河"利润下降主导 + 营运资本缓冲"、三一"主要由营运资本构成"；
4. 证据角色：派生算式不标披露原句；计划/未来段落不作已发生解释；公司归因可引用且保留身份；
5. 公司边界：洋河的酒类表缺口不串到三一；
6. 直接法/间接法分开、不相加、不嵌套（三一），并展示第 19 页发行人归因；
7. 版面：正文 4–6 页、详表/ID/七段式另附（`analysis/analysis_detail.md` 在包内）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES = {
    "洋河股份": {"dir": ROOT / "docs" / "evidence" / "v0_yanghe_normal_task",
                 "report": ROOT / "docs" / "evidence" / "v0_yanghe_normal_task.json"},
    "三一重工": {"dir": ROOT / "docs" / "evidence" / "x0_sany_normal_task",
                 "report": ROOT / "docs" / "evidence" / "x0_sany_normal_task.json"},
}
OUT = ROOT / "docs" / "evidence" / "x0_delivery_closure.json"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:                              # noqa: BLE001
        return ""


def _check(ok: bool, detail: str = "") -> dict:
    return {"ok": bool(ok), "detail": detail}


def _structure_block(detail_md: str) -> str:
    """七段式明细里**结构判断那一条**的正文（判断条之间按序号切）。"""
    text = str(detail_md or "")
    key = "产品/区域结构是"
    idx = text.find(key)
    if idx < 0:
        return ""
    nxt = len(text)
    for n in range(1, 10):
        j = text.find(f"\n{n}. ", idx + len(key))
        if j >= 0:
            nxt = min(nxt, j)
    return text[idx:nxt]


def _case(name: str, spec: dict) -> dict:
    md = _read(spec["dir"] / "report.md")
    detail = _read(spec["dir"] / "analysis_detail.md")
    front = _read(spec["dir"] / "analysis_front.md")
    ev = {}
    try:
        ev = json.loads(spec["report"].read_text(encoding="utf-8"))
    except Exception:                              # noqa: BLE001
        ev = {}
    layout = ((ev.get("pdf") or {}).get("layout") or {})
    pkg = ((ev.get("package") or {}).get("has_analysis") or [])
    body_pages = layout.get("analysis_body_pages")
    front_page = (layout.get("first_page_of") or {}).get("三项主判断")
    checks: dict = {
        "front_judgments_on_page_1_or_2": _check(
            bool(front_page and front_page <= 2), f"三项主判断 第 {front_page} 页"),
        "analysis_body_4_to_6_pages": _check(
            body_pages is not None and 4 <= int(body_pages) <= 6,
            f"正文 {body_pages} 页（经营驱动分析正文 → 变化解释前）"),
        "detail_is_separate_and_packed": _check(
            bool(front and detail) and "analysis/analysis_detail.md" in pkg,
            "front/detail 均生成；包内 " + ("含" if "analysis/analysis_detail.md" in pkg
                                          else "缺") + " analysis/analysis_detail.md"),
        "no_false_ton_price_gap": _check(
            "吨价推算所需的同口径收入/销量未取全" not in md
            and "吨价推算所需的同口径收入/销量未取全" not in front,
            "正文与首屏都没有'吨价输入缺口'"),
    }
    if name == "洋河股份":
        checks["front_carries_volume_price_inventory"] = _check(
            all(k in front for k in ("-16.30%", "+3.93%", "+16.38%", "-8.40%",
                                     "推算口径")),
            "首屏含销量/吨价(推算身份)/库存/产量")
        checks["cash_direction_is_profit_led_buffer"] = _check(
            "由利润下降主导" in detail and "缓冲" in detail,
            "七段式：利润下降主导 + 营运资本缓冲")
        checks["structure_evidence_excludes_rd_microecology"] = _check(
            _structure_block(detail).find("微生态") < 0,
            "研发微生态段落不作为结构支持（结构判断条内没有该段）")
    else:
        checks["cash_direction_is_working_capital"] = _check(
            "主要由营运资本（占用与时点）构成" in front,
            "首屏：现金改善主要由营运资本构成")
        checks["direct_and_indirect_are_separate"] = _check(
            ("**直接法**" in front and "**间接法**" in front
             and "两行净贡献" in front and "其余经营活动收支" in front
             and "其中合并净利润项" not in md),
            "直接法两行净贡献与间接法各自列出、不相加；无'其中净利项'嵌套写法")
        checks["company_attribution_at_page_19"] = _check(
            ("发行人归因（原句）" in front and "第 19 页" in front),
            "首屏依据＝发行人归因（第 19 页）")
        checks["no_yanghe_specific_gap"] = _check(
            "中高档" not in (md + detail) and "酒类" not in (md + detail),
            "三一正文与详表都不含洋河专属的酒类缺口")
    return {"dir": str(spec["dir"].relative_to(ROOT)),
            "pdf_pages": layout.get("pages"),
            "layout": layout,
            "checks": checks,
            "passed": sum(1 for c in checks.values() if c["ok"]),
            "total": len(checks)}


def main() -> int:
    report = {"batch": "X0 交付收口（一次装配／方向／角色／切法／版面）", "cases": {}}
    all_ok = True
    for name, spec in CASES.items():
        entry = _case(name, spec)
        report["cases"][name] = entry
        all_ok = all_ok and entry["passed"] == entry["total"]
        print(f"{name}: {entry['passed']}/{entry['total']} —— PDF {entry['pdf_pages']} 页；"
              + "；".join(f"{k}={'OK' if v['ok'] else 'FAIL'}"
                          for k, v in entry["checks"].items()))
    report["summary"] = {"all_ok": bool(all_ok)}
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"证据：{OUT.relative_to(ROOT)}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
