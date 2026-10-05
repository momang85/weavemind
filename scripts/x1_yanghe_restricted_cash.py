# -*- coding: utf-8 -*-
"""闭环一处**已知缺口**：洋河受限资金/债务读数（材料已在本地，0 成本、不联网、不调模型）。

背景：`y1_002304_risk_change_20261005.md` 的"债务/受限资金"当时记为**缺口**。实际上 2024 年年度报告
第十节 §54 有专节 `（3）不属于现金及现金等价物的货币资金`，把受限部分逐项列了出来——缺口是**定位没做**，
不是材料没有。本脚本只做"取数 + 给定位"，**不改报告链、不在正文里下结论**。

定位口径（与 `narrative_evidence` 同规矩）：**小节路径 + 字符区间**，再用 `page_offsets`
把字符位置换成页码（网页/PDF 没有稳定页码，字符区间是诚实替代）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NUM = re.compile(r"-?[\d,]+\.\d{2}")


def _num(s: str):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _page_of(page_offsets, pos: int) -> int | None:
    page = None
    for off, pno in page_offsets or []:
        if pos >= int(off):
            page = int(pno)
        else:
            break
    return page


def _section(text: str, start_marker: str, end_markers=(), *, span: int = 1600) -> dict:
    """从一个起点标记截一段（到最近的结束标记或 span 上限），带字符区间与页码。"""
    i = text.find(start_marker)
    if i < 0:
        return {"found": False, "reason": f"没找到标记：{start_marker}"}
    end = i + span
    for m in end_markers:
        j = text.find(m, i + len(start_marker))
        if 0 <= j < end:
            end = j
    return {"found": True, "start": i, "end": end, "text": text[i:end],
            "path": f"{start_marker} …"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--material-dir", required=True,
                    help="材料目录（含 doc.json），如 …/project/materials/f42e747c73850b59")
    ap.add_argument("--out", default="docs/evidence/y1_002304_restricted_cash_20261005.json")
    args = ap.parse_args()

    d = Path(args.material_dir)
    doc = json.loads((d / "doc.json").read_text(encoding="utf-8"))
    text = str(doc.get("text") or "")
    offs = doc.get("page_offsets") or []
    out: dict = {"schema": "weavemind.restricted_cash_probe/0",
                 "material_id": doc.get("material_id"),
                 "disclosed_at": doc.get("disclosed_at"),
                 "date_precision": doc.get("date_precision"),
                 "date_basis": doc.get("date_basis"),
                 "subject": doc.get("subject"), "periods": doc.get("periods"),
                 "text_sha256": doc.get("text_sha256"), "readings": {}, "gaps": []}

    # ① 受限资金：§54（3）不属于现金及现金等价物的货币资金
    # 标记取**不含编号**的特征串：PDF 抽出的正文在编号与标题之间带空格（`（3） 不属于…`），
    # 用带编号的串会静默找不到——这正是"定位没做"会被误当成"材料没有"的地方。
    sec = _section(text, "不属于现金及现金等价物的货币资金",
                   ("其他说明", "外币货币性项目"))
    if not sec.get("found"):
        out["gaps"].append({"what": "受限资金", "reason": sec.get("reason")})
    else:
        nums = [_num(x) for x in NUM.findall(sec["text"])]
        nums = [n for n in nums if n is not None]
        out["readings"]["restricted_cash"] = {
            "section_path": "第十节 财务报告 > 七、合并财务报表项目注释 > 54、现金流量表补充资料 "
                            ">（3）不属于现金及现金等价物的货币资金",
            "start": sec["start"], "end": sec["end"],
            "page": _page_of(offs, sec["start"]),
            "numbers": nums,
            "line": " ".join(sec["text"].split())[:600],
        }
    # ② 现金及现金等价物构成：§54（2）
    sec2 = _section(text, "现金和现金等价物的构成", ("不属于现金及现金等价物",))
    if sec2.get("found"):
        out["readings"]["cash_equivalents"] = {
            "section_path": "… > 54、现金流量表补充资料 >（2）现金和现金等价物的构成",
            "start": sec2["start"], "end": sec2["end"], "page": _page_of(offs, sec2["start"]),
            "numbers": [n for n in (_num(x) for x in NUM.findall(sec2["text"])) if n is not None],
            "line": " ".join(sec2["text"].split())[:400]}
    # ③ 债务：短期/长期借款与应付债券是否存在（有则给定位，无则如实记缺口）
    for name, key in (("短期借款", "short_term_borrowing"), ("长期借款", "long_term_borrowing"),
                      ("应付债券", "bonds_payable"), ("所有权或使用权受到限制的资产",
                                                "restricted_assets")):
        j = text.find(name)
        out["readings"].setdefault("debt_probe", {})[key] = (
            {"found": True, "start": j, "page": _page_of(offs, j),
             "line": " ".join(text[max(0, j - 60):j + 200].split())[:260]}
            if j >= 0 else {"found": False, "note": f"正文未出现「{name}」"})

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    r = out["readings"].get("restricted_cash")
    print(json.dumps({"material_id": out["material_id"], "disclosed_at": out["disclosed_at"],
                      "restricted_cash_found": bool(r), "restricted_page": (r or {}).get("page"),
                      "restricted_numbers": (r or {}).get("numbers"),
                      "debt_probe": {k: v.get("found") for k, v in
                                     out["readings"].get("debt_probe", {}).items()},
                      "gaps": out["gaps"], "out": args.out},
                     ensure_ascii=False, indent=1))
    return 0 if r else 1


if __name__ == "__main__":
    raise SystemExit(main())
