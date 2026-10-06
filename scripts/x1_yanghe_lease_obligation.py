# -*- coding: utf-8 -*-
"""洋河**租赁义务**读数（材料已在本地，0 成本、不联网、不调模型）。

背景（10-06 复核 §3）：风险卡此前用"短期借款/长期借款/应付债券**三项无金额**"推出
"无有息负债"。这是**窄范围标题代替整体结论**：同一份 2024 年报里租赁负债是**有金额的**
有息性质义务。本脚本只做"取数 + 给定位"，把租赁义务**单列**出来，不改报告链、不下偿债结论。

定位口径（与 `narrative_evidence`、`x1_yanghe_restricted_cash.py` 同规矩）：
**小节路径 + 字符区间**，再用 `page_offsets` 把字符位置换成页码。

三处互相独立的位置都要命中，且**期末合计必须闭合**：
  ① 合并资产负债表（非流动负债）租赁负债 期末 40,134,989.46；
  ② 附注 26「一年内到期的非流动负债」一年内到期的租赁负债 23,588,100.85；
  ③ 附注 28「租赁负债」应付租赁费 40,134,989.46；
  ④ 附注 54 筹资活动负债变动表「租赁负债（含一年内到期的非流动负债）」期末 63,723,090.31
     —— 公司**自己给的合计**，用来交叉验证 ①+②。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NUM = re.compile(r"-?[\d,]+\.\d{2}")
YI = 100000000.0


def _num(s: str):
    try:
        return float(str(s).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _page_of(page_offsets, pos: int):
    page = None
    for off, pno in page_offsets or []:
        if pos >= int(off):
            page = int(pno)
        else:
            break
    return page


def _window(text: str, marker: str, *, back: int = 40, span: int = 240, nth: int = 0) -> dict:
    """取标记附近的一段（默认第一处；`nth` 指定第几处），带字符区间与页码。"""
    pos = -1
    for _ in range(nth + 1):
        pos = text.find(marker, pos + 1)
        if pos < 0:
            return {"found": False, "reason": f"没找到标记：{marker}（第 {nth + 1} 处）"}
    start = max(0, pos - back)
    return {"found": True, "start": start, "end": pos + span,
            "line": " ".join(text[start:pos + span].split())[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--material-dir", required=True,
                    help="材料目录（含 doc.json），如 …/project/materials/f42e747c73850b59")
    ap.add_argument("--out", default="docs/evidence/y1_002304_lease_obligation_20261006.json")
    args = ap.parse_args()

    doc = json.loads((Path(args.material_dir) / "doc.json").read_text(encoding="utf-8"))
    text = str(doc.get("text") or "")
    offs = doc.get("page_offsets") or []
    out: dict = {"schema": "weavemind.lease_obligation_probe/0",
                 "material_id": doc.get("material_id"),
                 "disclosed_at": doc.get("disclosed_at"),
                 "subject": doc.get("subject"), "periods": doc.get("periods"),
                 "text_sha256": doc.get("text_sha256"),
                 "scope_note": ("三项借款/债券科目无金额 ≠ 无有息负债：租赁负债是有金额的"
                                "有息性质义务，本表把它单列；不据此给偿债结论"),
                 "readings": {}, "checks": {}, "gaps": []}

    def put(key: str, path: str, win: dict, *, pick: str = "first") -> dict:
        if not win.get("found"):
            out["gaps"].append({"what": key, "reason": win.get("reason")})
            return {}
        # PDF 抽文会把小数**断开**（`63,723,090.3 1`）：只把"小数点后一位 + 空格 + 数字"
        # 合起来，不做通用的空格去重（那样会把相邻两个数粘成一个，反而是编数字）。
        norm = re.sub(r"(?<=\.\d)\s+(?=\d)", "", win["line"])
        nums = [n for n in (_num(x) for x in NUM.findall(norm)) if n is not None]
        rec = {"section_path": path, "start": win["start"], "end": win["end"],
               "page": _page_of(offs, win["start"]), "numbers": nums,
               "pick": pick, "value": (nums[-1] if (nums and pick == "last")
                                       else (nums[0] if nums else None)),
               "line": win["line"],
               "normalization": "仅合并被空格断开的小数位（`\\.\\d` + 空格 + 数字）"}
        out["readings"][key] = rec
        return rec

    # ① 合并资产负债表（非流动负债）——租赁负债在"长期借款/应付债券（无金额）"之后
    put("balance_sheet_lease", "第十节 财务报告 > 合并资产负债表 > 非流动负债 > 租赁负债",
        _window(text, "租赁负债", nth=1))
    # ② 附注 26 一年内到期的非流动负债
    put("current_portion", "… > 七、合并财务报表项目注释 > 26、一年内到期的非流动负债",
        _window(text, "一年内到期的租赁负债"))
    # ③ 附注 28 租赁负债（应付租赁费）
    put("lease_note", "… > 七、合并财务报表项目注释 > 28、租赁负债",
        _window(text, "应付租赁费"))
    # ④ 附注 54 筹资活动负债变动表（公司自己给的期末合计 = 该行**最后一个**数）
    put("rollforward_total", "… > 54、现金流量表补充资料 > 筹资活动产生的各项负债变动情况",
        _window(text, "租赁负债（含", span=320), pick="last")

    def _val(key: str):
        return (out["readings"].get(key) or {}).get("value")

    cur, noncur, total = _val("current_portion"), _val("balance_sheet_lease"), \
        _val("rollforward_total")
    s = None if (cur is None or noncur is None) else round(cur + noncur, 2)
    out["checks"] = {
        "current_portion": cur, "non_current_portion": noncur,
        "sum_current_plus_non_current": s,
        "company_reported_total": total,
        "sum_equals_company_total": bool(s is not None and total is not None
                                          and abs(s - total) < 0.01),
        "total_yi": round((total or 0.0) / YI, 6),
        "total_wan": round((total or 0.0) / 10000.0, 2),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"material_id": out["material_id"], "checks": out["checks"],
                      "pages": {k: (v or {}).get("page") for k, v in out["readings"].items()},
                      "gaps": out["gaps"], "out": args.out}, ensure_ascii=False, indent=1))
    return 0 if out["checks"]["sum_equals_company_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
