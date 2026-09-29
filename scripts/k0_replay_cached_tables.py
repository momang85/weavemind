#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""K0-b 缓存回放：用**已落盘的年报原文**重跑表格抽取，对比修前基线（不联网、不付费）。

为什么需要它：抽取器的每次窄修都可能"修一处、退一处"。夜验收要求"回放现有京蓝、
三一、洋河缓存，检查正确数保留与错误项拒绝"。这里读 `evals/a2_official_chain_20260929/`
里已存的 `doc.json`（正文解析结果，不重新下载），重跑 `annual_financial_tables`，
把 **text_hash / 事实数 / 观察数 / 拒绝原因 / 模型 validated 数** 与
`report.json` 的基线逐项对照。

用法：
    python scripts/k0_replay_cached_tables.py            # 全部缓存，打印对照表
    python scripts/k0_replay_cached_tables.py --expect   # 与基线不符时退出码 1
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters import annual_financial_tables as aft  # noqa: E402

CACHE = ROOT / "evals" / "a2_official_chain_20260929"

# 基线（`report.json` 里各公司的 facts/dataset 块；只列用于对照的字段）
BASE_FIELDS = ("text_hash", "facts", "tables", "rejected_total", "observations")


def _load_handle(slug_dir: Path):
    """缓存材料 → `(doc, meta)`；取 index.json 里第一条（本轮每家只有一份正文）。"""
    mats = slug_dir / "project" / "materials"
    docs = sorted(mats.glob("*/doc.json"))
    if not docs:
        return None, None
    doc = json.loads(docs[0].read_text(encoding="utf-8"))
    meta_p = docs[0].with_name("meta.json")
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    return doc, meta


def replay(slug_dir: Path, *, company: str, code: str, periods, as_of: str = ""):
    doc, meta = _load_handle(slug_dir)
    if doc is None:
        return None
    doc = dict(doc)
    doc["url"] = str((meta or {}).get("url") or doc.get("url") or "")
    ds, raw = aft.to_dataset(doc, company=company, company_code=code, periods=periods,
                             as_of=as_of or str((meta or {}).get("disclosure_date") or ""))
    import financial_analysis as fa
    question = (f"{company}（{code}）{'/'.join(str(p) for p in periods)} 年经营分析："
                "利润变化来自哪里、现金质量如何、营运资金占用怎样")
    plan = fa.compile_plan(question, ds, prefer=("profit_bridge",))
    validated = 0
    for item in plan.adopted:
        run = fa.run(item.model_id, ds, params=item.params, question=item.question)
        validated += 1 if run.status == fa.RunStatus.VALIDATED else 0
    return {
        "text_hash": raw.get("text_hash"),
        "facts": len(raw.get("facts") or ()),
        "tables": len(raw.get("tables") or ()),
        "rejected_total": len(raw.get("rejected") or ()),
        "rejected_reasons": dict(Counter(str(r.get("reason"))
                                         for r in raw.get("rejected") or ()).most_common()),
        "observations": ds.manifest.observations,
        "usable": ds.manifest.usable,
        "periods": list(ds.manifest.periods),
        "conflicts": list(ds.manifest.conflicts),
        "adopted": [a.model_id for a in plan.adopted],
        "validated": validated,
        "rejected_models": [r for r in plan.rejected],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", action="store_true",
                    help="与 report.json 基线不一致时以非零码退出")
    args = ap.parse_args()
    base = json.loads((CACHE / "report.json").read_text(encoding="utf-8"))
    by_code = {str(c.get("code") or ""): c for c in base.get("companies") or []}
    bad = 0
    for slug_dir in sorted(p for p in CACHE.iterdir() if p.is_dir()):
        slug = slug_dir.name
        code = ""
        for c in base.get("companies") or []:
            if str(c.get("code") or "").split(".")[0] in slug:
                code = str(c["code"])
        if slug.endswith("-corrected"):
            # 更正稿：基线在 report.json 的 restatement_demo 里，另有一份 text_hash
            demo = None
            for c in base.get("companies") or []:
                if c.get("restatement_demo"):
                    demo = c
            if not demo:
                print(f"[skip] {slug}：report.json 里没有对应基线")
                continue
            got = replay(slug_dir, company=demo["company"], code=demo["code"],
                         periods=demo.get("periods") or (2019, 2020))
            if got is None:
                print(f"[skip] {slug}：缓存材料缺失")
                continue
            print(f"[{slug}] text_hash={str(got['text_hash'])[:12]} facts={got['facts']} "
                  f"obs={got['observations']} validated={got['validated']}")
            continue
        if not code:
            print(f"[skip] {slug}：无法对应基线公司")
            continue
        company = str(by_code[code].get("company") or "")
        periods = by_code[code].get("periods") or (2024,)
        got = replay(slug_dir, company=company, code=code, periods=periods)
        if got is None:
            print(f"[skip] {slug}：缓存材料缺失")
            continue
        want = {
            "text_hash": by_code[code]["facts"]["text_hash"],
            "facts": by_code[code]["facts"]["facts"],
            "tables": by_code[code]["facts"]["tables"],
            "rejected_total": by_code[code]["facts"]["rejected_total"],
            "observations": by_code[code]["dataset"]["observations"],
        }
        diffs = [k for k in BASE_FIELDS if got.get(k) != want.get(k)]
        flag = "OK " if not diffs else "DIFF"
        print(f"[{flag}] {slug} {code}")
        for k in BASE_FIELDS:
            mark = "" if k not in diffs else "   <-- 与基线不同"
            print(f"       {k:16s} 基线={str(want.get(k))[:20]:22s}"
                  f" 现在={str(got.get(k))[:20]}{mark}")
        print(f"       rejected_reasons={got['rejected_reasons']}")
        print(f"       periods={got['periods']} conflicts={len(got['conflicts'])} "
              f"adopted={got['adopted']} validated={got['validated']}")
        if diffs:
            bad += 1
    print("\n==== 回放结论 ====")
    print(f"与基线不一致的公司数：{bad}")
    return 1 if (bad and args.expect) else 0


if __name__ == "__main__":
    raise SystemExit(main())
