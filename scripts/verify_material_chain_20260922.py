# -*- coding: utf-8 -*-
"""第二小批（离线部分）：用已保存的**真实公告片段**闭合"取得→解析→定位→对应问题"。

只读产物 + 内存计算，不联网、不调用模型。

定位口径（09-23 更正）：东财公告文本 API 的 `page_index` 返回的是**接口片段**
（每段 5000 字符），不是 PDF 实体页——因此：
- 不构造 `page_offsets`（不写"第 N 页"），改用 `chunk_offsets`（片段号 + 字符区间）；
- 定位文本形如 `api_chunk 3 · 小节：…（字符 a-b）`；
- 原始响应字节未落盘：旧 raw hash 不可复验，改用已存正文的 `text_sha256` 核对。
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
chunks = [(int(p["api_chunk"]), str(p.get("text") or ""))
          for p in data["pages"] if p.get("text")]
print("片段数:", len(chunks), "| HTTP:", data["http_requests"],
      "| 披露日:", data["published_at"], "| 定位口径:", data.get("location_kind"))

import annual_report_pdf as pdf          # noqa: E402
import narrative_evidence as ne          # noqa: E402

# 正文 hash 核对（原始字节未留存 → 只核对已存正文，不冒充 raw 校验）
for p in data["pages"]:
    if not p.get("text"):
        continue
    got = hashlib.sha256(str(p["text"]).encode("utf-8")).hexdigest()
    assert got == p.get("text_sha256"), (p.get("api_chunk"), got)
    assert p.get("raw_retained") is False, p
print("正文 text_sha256 核对:", len(chunks), "段全部一致")

# 解析：片段文本 → 文档对象；**没有 PDF 页码映射**，只建接口片段偏移
doc = pdf.doc_from_pages("洋河股份:2024年年度报告", data["pages"][0]["url"],
                         [t for _c, t in chunks])
doc["published_at"] = data["published_at"]
offsets, pos, parts = [], 0, []
for ck, text in chunks:
    offsets.append((pos, ck))
    parts.append(text)
    pos += len(text) + 1
doc["text"] = "\n".join(parts)
doc["chunk_offsets"] = offsets
doc.pop("page_offsets", None)
print("解析: 正文", len(doc["text"]), "字符，接口片段偏移", offsets[:3], "...")

records = ne.extract_sections(doc, periods=[2023, 2024], company="洋河股份",
                              company_id="002304.SZ", as_of="2025-04-30")
located = [r for r in records if r.get("has_location")
           and str(r.get("admission") or "") in ("admitted", "comparison")]
print("定位记录:", len(located))
kinds = {}
for r in located:
    kinds.setdefault(str(r.get("kind") or ""), []).append(r)

# 定位种类核对：不得出现 PDF 页码形状，必须带 api_chunk
bad_loc = [r.get("locator") for r in located if "第 " in str(r.get("locator") or "")
           or "api_chunk" not in str(r.get("locator") or "")]
assert not bad_loc, f"定位里出现页码或缺少 api_chunk：{bad_loc[:3]}"
print("定位样例:", str(located[0].get("locator"))[:80] if located else "(无)")

# 对应问题：真实披露里"解释收入变化"的句子。
# 记录正文在 `snippet`（不是 `text`）；snippet 是**截断前缀**，会漏掉小节后半段的
# 句子——因此按记录的字符区间回原文取**完整小节正文**再找因果句。
import re  # noqa: E402
full_text = doc["text"]
rev_evidence = []
for r in located:
    cs, ce = int(r.get("char_start") or 0), int(r.get("char_end") or 0)
    body = full_text[cs:ce] if 0 <= cs < ce <= len(full_text) else ""
    if not body:
        body = str(r.get("snippet") or r.get("text") or "")
    for sent in re.split(r"[。；\n]", body):
        if "营业收入" in sent and ne.is_causal(sent):
            rev_evidence.append((r.get("chunk"), r.get("locator"), sent.strip()[:90]))
print("谈到收入的因果句:", len(rev_evidence))
for ck, loc, s in rev_evidence[:3]:
    print(f"  api_chunk {ck}: {s}")

# 原文全量（不限于已分类小节）：公告片段里到底有没有收入解释句。
# 09-23 更正：上次写 0 条，一部分是字段读错（text vs snippet），另一部分是
# "四、主营业务分析 > 1、概述"被分类器判为 None（kind=None → 不产出记录）。
# 两个口径分开报，不混成一个结论。
raw_causal = []
for ck, text in chunks:
    for sent in re.split(r"[。；\n]", text):
        if "营业收入" in sent and ne.is_causal(sent):
            raw_causal.append((ck, sent.strip()[:100]))
print("原文片段里的收入因果句（全量，含未分类小节）:", len(raw_causal))
for ck, s in raw_causal[:3]:
    print(f"  api_chunk {ck}: {s}")
secs = ne.split_sections(doc["text"], chunk_offsets=offsets)
unclassified = []
for s in secs:
    if not ne.classify(s["title"], s["body"], source=ne.source_type(doc["url"])):
        for sent in re.split(r"[。；\n]", s["body"]):
            if "营业收入" in sent and ne.is_causal(sent):
                unclassified.append((s.get("chunk"), s["path"], s["start"], s["end"],
                                     sent.strip()[:100]))
print("未分类小节里的收入因果句:", len(unclassified))
for ck, path, cs, ce, s in unclassified[:3]:
    print(f"  api_chunk {ck} 字符 {cs}-{ce}：{path}｜{s}")
missing = sorted({k for k in ne.KIND_LABELS.values() if k not in
                  {ne.KIND_LABELS.get(k2) for k2 in kinds}})
print("本批材料覆盖的类别:", sorted(kinds))
print("仍缺类别:", missing)
out = ROOT / "docs" / "evidence" / "_night_material_chain_20260922.json"
out.write_text(json.dumps({
    "art_code": data["art_code"], "published_at": data["published_at"],
    "location_kind": "api_chunk", "pdf_page_mapping": None,
    "http_requests": data["http_requests"], "elapsed_sec": data["elapsed_sec"],
    "chunks": [{"api_chunk": p["api_chunk"], "text_sha256": p["text_sha256"],
                "chars": p["text_chars"]} for p in data["pages"] if p.get("text")],
    "located_records": len(located), "kinds": sorted(kinds),
    "locator_sample": str(located[0].get("locator")) if located else "",
    "revenue_causal_sentences": len(rev_evidence),
    "revenue_causal_examples": [s for _c, _l, s in rev_evidence[:3]],
    "revenue_causal_raw": len(raw_causal),
    "revenue_causal_raw_examples": [s for _c, s in raw_causal[:3]],
    "revenue_causal_unclassified": len(unclassified),
    "revenue_causal_unclassified_examples": [
        {"api_chunk": ck, "path": path, "char_start": cs, "char_end": ce, "text": s}
        for ck, path, cs, ce, s in unclassified[:3]],
    "missing_kinds": missing,
}, ensure_ascii=False, indent=1), encoding="utf-8")
print("wrote", out)
