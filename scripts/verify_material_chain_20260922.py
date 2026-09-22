# -*- coding: utf-8 -*-
"""第二小批（离线部分）：用刚取到的**真实原文页**闭合"取得→解析→定位→对应问题"。

只读产物 + 内存计算，不联网、不调用模型。
"""
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REDIS_PORT", "6399")

ART = ROOT / "evals" / "real" / "yanghe_ar2024_pages_20260922.json"
data = json.loads(ART.read_text(encoding="utf-8"))
pages = [(int(p["page"]), str(p.get("text") or "")) for p in data["pages"] if p.get("text")]
print("页数:", len(pages), "| HTTP:", data["http_requests"], "| 披露日:", data["published_at"])

import annual_report_pdf as pdf          # noqa: E402
import narrative_evidence as ne          # noqa: E402

# 取得（真实页码 + 原始字节 hash）→ 解析（页文本 → 带页码偏移的文档对象）
for p in data["pages"]:
    assert p.get("raw_sha256"), p
doc = pdf.doc_from_pages("洋河股份:2024年年度报告", data["pages"][0]["url"],
                         [t for _pg, t in pages])
doc["published_at"] = data["published_at"]
# 真实页码：page_offsets 是按**抓取顺序**建立的，这里按真实页码重建
offsets, pos, parts = [], 0, []
for pg, text in pages:
    offsets.append((pos, pg))
    parts.append(text)
    pos += len(text) + 1
doc["text"] = "\n".join(parts)
doc["page_offsets"] = offsets
print("解析: 正文", len(doc["text"]), "字符，页码偏移", offsets[:3], "...")

records = ne.extract_sections(doc, periods=[2023, 2024], company="洋河股份",
                              company_id="002304.SZ", as_of="2025-04-30")
located = [r for r in records if r.get("has_location")
           and str(r.get("admission") or "") in ("admitted", "comparison")]
print("定位记录:", len(located))
kinds = {}
for r in located:
    kinds.setdefault(str(r.get("kind") or ""), []).append(r)

# 对应问题：真实披露里"解释收入变化"的句子（带页码定位）
import re  # noqa: E402
rev_evidence = []
for r in located:
    for sent in re.split(r"[。；\n]", str(r.get("text") or "")):
        if "营业收入" in sent and ne.is_causal(sent):
            rev_evidence.append((r.get("page"), sent.strip()[:90]))
print("谈到收入的因果句:", len(rev_evidence))
for pg, s in rev_evidence[:3]:
    print(f"  第{pg}页: {s}")
missing = sorted({k for k in ne.KIND_LABELS.values() if k not in
                  {ne.KIND_LABELS.get(k2) for k2 in kinds}})
print("本批材料覆盖的类别:", sorted(kinds))
print("仍缺类别:", missing)
out = ROOT / "docs" / "evidence" / "_night_material_chain_20260922.json"
out.write_text(json.dumps({
    "art_code": data["art_code"], "published_at": data["published_at"],
    "http_requests": data["http_requests"], "elapsed_sec": data["elapsed_sec"],
    "pages": [{"page": p["page"], "raw_sha256": p["raw_sha256"],
               "chars": p["text_chars"]} for p in data["pages"]],
    "located_records": len(located), "kinds": sorted(kinds),
    "revenue_causal_sentences": len(rev_evidence),
    "missing_kinds": missing,
}, ensure_ascii=False, indent=1), encoding="utf-8")
print("wrote", out)
