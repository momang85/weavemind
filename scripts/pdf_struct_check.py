# -*- coding: utf-8 -*-
"""实机 PDF 结构核对（只读脚本，不入库）：页数 / 每页图数 / 表格对齐 / 引用图是否嵌入。

走**生产同一函数** `web_ui._task_pdf_bytes`（导出即写清单，与页面"下载PDF"一致），
再用 `report_pdf` 的结构判定读回结果——"正文引用了 6 张图，PDF 里就有 6 张"。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import report_pdf  # noqa: E402
import web_ui  # noqa: E402

TID = sys.argv[1] if len(sys.argv) > 1 else "ui-750185076a"
pdf = web_ui._task_pdf_bytes(TID)
print("pdf bytes", len(pdf))

from pypdf import PdfReader  # noqa: E402

reader = PdfReader(__import__("io").BytesIO(pdf))
pages = []
per_page_images = report_pdf.pdf_page_image_counts(pdf)
for i, page in enumerate(reader.pages, 1):
    chars = len((page.extract_text() or "").strip())
    imgs = per_page_images[i - 1] if i - 1 < len(per_page_images) else 0
    pages.append({"page": i, "chars": chars, "images": imgs})
print("page_count", len(pages))
print("images_total", sum(p["images"] for p in pages))
print("blank_pages", [p["page"] for p in pages if p["chars"] < 50 and not p["images"]])
print("pages", json.dumps(pages))

text = "".join((p.extract_text() or "") for p in reader.pages)
charts = json.loads(
    (Path(web_ui.task_workspace(TID)) / "report_structure.json").read_text(encoding="utf-8")
).get("charts") or []
print("charts_in_structure", len(charts))
img = report_pdf.pdf_image_report(pdf, expected=len(charts), text=text)
print("image_report", json.dumps({k: v for k, v in img.items()}, ensure_ascii=False)[:600])
grids = report_pdf.pdf_table_grids(pdf)
print("tables_checked", len(grids), "aligned_all", all(g.get("aligned") for g in grids))
