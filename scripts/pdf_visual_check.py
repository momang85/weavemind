# -*- coding: utf-8 -*-
"""PDF 逐页视觉证据：生成样例 → 渲染逐页 PNG → 写索引（供视觉门禁判定）。

为什么要单独一个脚本：几何/文本断言证明不了"看起来对不对"——页码有没有印出来、
跨页表头有没有重复、缺图有没有可见占位、中文与数字并排时字距/单位是否正常，
只有把真实 PDF 栅格化成图才看得见。渲染器用 **已安装** 的 pypdfium2
（本机在 Codex 运行时自带的 Python 3.12 里；不装依赖、不改生产环境）：

    PY="C:/Users/ding0/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"
    "$PY" scripts/pdf_visual_check.py --out .weavemind/pdf_visual

产物（默认写到运行目录，不入库）：
    <out>/<case>/page-N.png      逐页图，交给视觉门禁
    <out>/<case>/report.pdf      真实生成的 PDF
    <out>/index.json             案例清单 + 每页 PNG 路径 + PDF/PNG 的 sha256

样例覆盖（对应 S0 要求检查的项）：
    两页样例   —— 分页与页脚页码（第 N 页 / 共 M 页）
    长表格     —— 跨页表头重复、行线、表头文字是否落在色块内
    缺图       —— 缺图占位是否可见、是否带原路径
    中英文数字 —— CJK 与 ASCII/数字/单位混排（含草稿标识）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import report_pdf  # noqa: E402


def _two_page_case() -> tuple[str, str]:
    body = "\n\n".join(f"第 {i} 段经营情况说明。" + "内容" * 55 for i in range(1, 31))
    md = (
        "# 贵州茅台 2024 年度经营复核\n\n"
        "数据口径：合并报表，金额单位 亿元。\n\n" + body
    )
    return md, "贵州茅台 2024 年度经营复核"


def _long_table_case() -> tuple[str, str]:
    rows = ["| 公司 | 营业收入 | 归母净利润 | 经营现金流 | 来源 |",
            "|---|---|---|---|---|"]
    for i in range(1, 46):
        rows.append(
            f"| 公司{i} | {1000 + i} | {100 + i} | {200 + i} | 2024年报 P{i} |")
    md = "# 长表格分页\n\n下表 45 行，用于检查跨页表头是否重复。\n\n" + "\n".join(rows)
    return md, "长表格分页"


def _missing_image_case() -> tuple[str, str]:
    md = (
        "# 缺图占位\n\n"
        "![营收趋势图](missing_chart.png)\n\n"
        "上图引用的文件不存在，应当看到可见占位而不是一片空白。\n"
    )
    return md, "缺图占位"


def _mixed_text_case() -> tuple[str, str]:
    md = (
        "> **未验收草稿：该版正文没有取得对应它的验收通过**\n"
        "> 本次交付不得视为已通过，也不得直接用于对外发布或审批。\n\n"
        "# 中英文与数字混排\n\n"
        "2024 年营业收入 1741.00 亿元，同比 +15.7%；归母净利润 862.28 亿元，"
        "同比 +15.4%；经营活动现金流净额 924.90 亿元。\n\n"
        "对比口径：2023 年营业收入 1505.60 亿元（来源：2023 年报 P58）。\n\n"
        "英文与符号：ROE 34.2%, EPS 68.10 CNY, code 600519.SH (SSE)。\n"
    )
    return md, "中英文与数字混排"


CASES = {
    "two-page": _two_page_case,
    "long-table": _long_table_case,
    "missing-image": _missing_image_case,
    "mixed-text": _mixed_text_case,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / ".weavemind" / "pdf_visual"))
    ap.add_argument("--scale", type=float, default=2.0,
                    help="栅格化缩放（2.0 ≈ 144dpi，够看清字号与单位）")
    args = ap.parse_args()

    try:
        import pypdfium2 as pdfium
    except Exception as exc:               # 明确说出"缺渲染器"，不要静默跳过
        print(f"缺少渲染器 pypdfium2：{exc}\n"
              "请用自带该库的解释器运行（见本文件顶部注释）", file=sys.stderr)
        return 2

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    index: dict = {"renderer": f"pypdfium2 {getattr(pdfium, '__version__', '?')}",
                   "scale": args.scale, "cases": []}

    for name, builder in CASES.items():
        md, title = builder()
        pdf_bytes = report_pdf.markdown_to_pdf(md, title=title, workspace=None)
        case_dir = out_root / name
        case_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = case_dir / "report.pdf"
        pdf_path.write_bytes(pdf_bytes)

        doc = pdfium.PdfDocument(pdf_bytes)
        pages = []
        for idx in range(len(doc)):
            page = doc[idx]
            bitmap = page.render(scale=args.scale)
            png_path = case_dir / f"page-{idx + 1}.png"
            bitmap.to_pil().save(png_path)
            pages.append({"page": idx + 1, "png": str(png_path),
                          "png_sha256": _sha256(png_path)})
        doc.close()
        index["cases"].append({
            "case": name, "title": title, "pdf": str(pdf_path),
            "pdf_sha256": _sha256(pdf_path), "pages": pages,
        })
        print(f"[{name}] {len(pages)} 页 → {case_dir}")

    idx_path = out_root / "index.json"
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"索引：{idx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
