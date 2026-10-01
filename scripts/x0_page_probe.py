# -*- coding: utf-8 -*-
"""X0 版面探针：读一份报告 PDF，量**总页数**与关键内容首次出现的页码。

为什么单独一个探针：阶段X §3 的可见结果是"前两页可读到三项主判断；正文 4–6 页"，
这只能从**渲染后的 PDF**量，不能从 markdown 字符数推断。本脚本只读不写交付物。

用法：python scripts/x0_page_probe.py <report.pdf> [关键词 ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

DEFAULT_KEYS = ("三项主判断", "研究判断", "销量", "吨价", "库存量", "营运资本",
                "缓冲", "销售收现", "采购付现", "直接法", "间接法",
                "附：研究判断明细", "参考来源")


def probe(pdf: Path, keys=DEFAULT_KEYS) -> dict:
    import fitz
    with fitz.open(str(pdf)) as doc:
        pages = [p.get_text() for p in doc]
    out: dict = {"file": pdf.name, "pages": len(pages), "first_page_of": {}}
    for k in keys:
        hit = next((i + 1 for i, t in enumerate(pages) if k in t), None)
        if hit:
            out["first_page_of"][k] = hit
    return out


def main() -> int:
    pdf = Path(sys.argv[1])
    rep = probe(pdf, tuple(sys.argv[2:]) or DEFAULT_KEYS)
    print(f"PDF {rep['file']}：{rep['pages']} 页")
    for k, p in rep["first_page_of"].items():
        print(f"  {k}：第 {p} 页")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
