# -*- coding: utf-8 -*-
"""第二批（资料准入）：把已保存的公告文本经**正常资料准入链**送进任务。

只做两件事，全部离线、无模型、不联网：
1. 把 `evals/real/yanghe_ar2024_pages_20260922.json` 的六段接口片段合并成**一个文档**
   写进任务工作区的 `project/fetch_snapshot.json`（按 URL 幂等合并，不动其它条目），
   并带上 `chunk_offsets`——定位只能是 `api_chunk K（字符 a-b）`，没有 PDF 页码映射；
2. 调 `narrative_evidence.build`（产品里的同一条链）产出 `narrative_evidence.json`，
   打印已准入/已定位记录、定位样例，并核对：
   - 收入解释句进入 `change_explanation` 记录（只支持收入问题）；
   - 利润/现金没有对应的原因表述（保持缺口，不扩散）。

用法：python scripts/admit_saved_material_20260923.py [task_id]
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REDIS_PORT", "6399")

ART = ROOT / "evals" / "real" / "yanghe_ar2024_pages_20260922.json"
TASK = sys.argv[1] if len(sys.argv) > 1 else "ui-706c5ef4a5"
COMPANY, CODE, AS_OF = "洋河股份", "002304.SZ", "2025-04-30"
PERIODS = [2023, 2024]


def main() -> int:
    import workspace as ws_mod
    ws = Path(ws_mod.task_workspace(TASK))
    proj = ws / "project"
    proj.mkdir(parents=True, exist_ok=True)
    data = json.loads(ART.read_text(encoding="utf-8"))
    chunks = [(int(p["api_chunk"]), str(p.get("text") or ""))
              for p in data["pages"] if p.get("text")]
    if not chunks:
        print("产物里没有可用片段", file=sys.stderr)
        return 2
    parts, offsets, pos = [], [], 0
    # 项4：合并走 `narrative_evidence.merge_chunks`——片段号跳号处插入显式缺口标记，
    # 合并文本不再假装连续（此前片段 5 的尾巴直接接片段 10 的开头，摘录与文本 hash
    # 都会被当成"一段连续原文"）。
    import narrative_evidence as ne
    merged_text, offsets, gaps = ne.merge_chunks(sorted(chunks, key=lambda x: x[0]))
    url = str(data["pages"][0]["url"])
    doc_item = {
        "title": str(data.get("notice_title_meta") or "洋河股份:2024年年度报告"),
        "url": url,
        "text": merged_text,
        # 接口片段偏移（不是 PDF 页码）：定位写 api_chunk
        "chunk_offsets": offsets,
        # 片段跳号处 = 合并文本的缺口（跨缺口摘录必须标明"非连续原文"）
        "chunk_gaps": gaps,
        "chunk_size": int(data.get("chunk_size") or 5000),
        "location_kind": "api_chunk",
        "art_code": data.get("art_code"),
        "published_at": str(data.get("published_at") or "unknown"),
        "fetched_at": str(data.get("fetched_at") or ""),
        "re_admitted_from": "evals/real/yanghe_ar2024_pages_20260922.json（离线重入，无新 HTTP）",
    }
    print("缺口:", gaps)
    snap_path = proj / "fetch_snapshot.json"
    try:
        items = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.exists() else []
    except Exception:
        items = []
    items = [it for it in (items if isinstance(items, list) else [])
             if isinstance(it, dict) and str(it.get("url") or "") != url]
    items.append(doc_item)
    snap_path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"快照写入：{snap_path}（{len(items)} 条，含公告片段 {len(chunks)} 段 / "
          f"{len(merged_text)} 字符）")

    import narrative_evidence as ne
    payload = ne.build(TASK, periods=PERIODS, company=COMPANY, company_id=CODE,
                       as_of=AS_OF, ws_dir=ws)
    recs = payload.get("records") or []
    located = [r for r in recs if r.get("has_location")
               and str(r.get("admission") or "") in ("admitted", "comparison")]
    kinds: dict[str, list] = {}
    for r in located:
        kinds.setdefault(str(r.get("kind") or ""), []).append(r)
    print(f"记录 {len(recs)} 条，已准入/已定位 {len(located)} 条，"
          f"类别 {sorted(kinds)}，missing={payload.get('missing_kinds')}")
    for kind, rows in sorted(kinds.items()):
        print(f"  {kind}: {len(rows)} 条；样例 {str(rows[0].get('locator'))[:80]}")

    # 只支持对应问题：收入解释在 change_explanation；利润/现金不得有原因表述
    change_rows = kinds.get(ne.KIND_CHANGE) or []
    rev, prof, cash = [], [], []
    for r in change_rows:
        body = str(r.get("snippet") or "")
        for sent in re.split(r"[。；\n]", body):
            if "营业收入" in sent and ne.is_causal(sent):
                rev.append((r.get("locator"), sent.strip()[:100]))
            if "净利润" in sent and ne.is_causal(sent):
                prof.append((r.get("locator"), sent.strip()[:100]))
            if "现金流" in sent and ne.is_causal(sent):
                cash.append((r.get("locator"), sent.strip()[:100]))
    print("收入解释句:", len(rev))
    for loc, s in rev[:2]:
        print(f"  {str(loc)[:70]}｜{s}")
    print("利润原因句:", len(prof), "| 现金原因句:", len(cash), "（应保持缺口/无原因）")
    out = ROOT / "docs" / "evidence" / "_batch2_material_admission_20260923.json"
    out.write_text(json.dumps({
        "task": TASK, "art_code": data.get("art_code"),
        "location_kind": "api_chunk", "pdf_page_mapping": None,
        "chunks": [{"api_chunk": ck, "chars": len(t)} for ck, t in chunks],
        "records": len(recs), "located": len(located), "kinds": sorted(kinds),
        "locator_samples": {k: str(v[0].get("locator")) for k, v in sorted(kinds.items())},
        "revenue_causal": len(rev), "profit_causal": len(prof), "cash_causal": len(cash),
        "missing_kinds": payload.get("missing_kinds"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
