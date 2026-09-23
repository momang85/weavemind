# -*- coding: utf-8 -*-
"""包内 PDF 的文字完整性与版面边界核对（离线、只读、不联网）。

退出条件（架构指令 §D）：渲染与文字边界都要检查——边界/引用/免责声明不得丢字，
正文不得有字符落到页面之外。这里逐字符量底边/顶边，并把交付 MD 的**每个标题**与
关键句拿去 PDF 文本里找；缺失逐条列出（不猜、不四舍五入成"通过"）。

用法：<bundled python> scripts/pdf_package_check_20260923.py <zip> <out.json>
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pypdfium2 as pdfium


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    zp = Path(sys.argv[1])
    out_json = Path(sys.argv[2])
    with zipfile.ZipFile(zp) as z:
        pdf_bytes = z.read("reports/report.pdf")
        md = z.read("reports/report.md").decode("utf-8")
    tmp_pdf = out_json.with_suffix(".pdf")
    tmp_pdf.write_bytes(pdf_bytes)

    doc = pdfium.PdfDocument(str(tmp_pdf))
    pages = []
    for i in range(len(doc)):
        page = doc[i]
        tp = page.get_textpage()
        text = tp.get_text_bounded()
        ys_bottom, ys_top = [], []
        for idx in range(tp.count_chars()):
            try:
                box = tp.get_charbox(idx)
            except Exception:                     # noqa: BLE001 - 单个字符读不到就跳过
                continue
            if box and len(box) == 4:
                ys_bottom.append(box[1])
                ys_top.append(box[3])
        pages.append({
            "page": i + 1, "chars": len(text),
            "min_bottom": round(min(ys_bottom, default=0.0), 1),
            "max_top": round(max(ys_top, default=0.0), 1),
            "height": round(float(page.get_height()), 1),
            "text": text,
        })
        tp.close()
        page.close()

    alltext = "\n".join(p["text"] for p in pages)

    def norm(s: str) -> str:
        # 排版层会把破折号统一成 ASCII 连字符（标题去重时做过规范化）：比较时视为等价，
        # 免得把"排版规范化"误报成"丢字"。
        t = "".join(str(s).split())
        for ch in ("–", "—", "－", "‑"):
            t = t.replace(ch, "-")
        return t

    all_n = norm(alltext)
    height = pages[0]["height"] if pages else 0.0
    oob = [(p["page"], p["min_bottom"], p["max_top"]) for p in pages
           if p["min_bottom"] < 0 or p["max_top"] > height + 1]
    headings = [ln.strip() for ln in md.split("\n") if ln.strip().startswith("#")]
    missing_headings = [h for h in headings if norm(h.lstrip("# ")) not in all_n]
    key_sentences = [
        "方可进入偿债能力评估环节",
        "不构成任何投资建议",
        "数据时效",
        "量价与结构（发行人披露）",
        "白酒吨价（推算）",
        "未取得该指标变化的解释",
        "已取得量价/结构数据",
        "洋河股份:2024年年度报告",
        "比率适用条件",
        "结论边界",
    ]
    missing_sentences = [s for s in key_sentences if norm(s) not in all_n]
    long_blocks = [ln.strip() for ln in md.split("\n") if ln.strip().startswith("- 资产负债率")]
    missing_blocks = [b[:24] for b in long_blocks if norm(b[:24]) not in all_n]

    res = {
        "pdf": str(tmp_pdf), "pages": len(pages), "page_height": height,
        "out_of_bounds": oob,
        "per_page": [{"page": p["page"], "chars": p["chars"],
                      "min_bottom": p["min_bottom"], "max_top": p["max_top"]}
                     for p in pages],
        "missing_headings": missing_headings,
        "missing_key_sentences": missing_sentences,
        "missing_long_blocks": missing_blocks,
        "md_chars": len(md), "pdf_text_chars": len(alltext),
    }
    out_json.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "per_page"},
                     ensure_ascii=False, indent=1)[:2500])
    print("per_page:", json.dumps(res["per_page"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
