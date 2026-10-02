# -*- coding: utf-8 -*-
"""把 d5af 两项验收的**只读复验**读数写成证据（不改原工作区）。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                  # noqa: BLE001
    pass

WS = Path(r"C:\Users\ding0\AppData\Local\Temp\agent_workspace\tasks\projects\default\ui-d5af8cae2f")
TARGET = "6145120084227eac85a2f779162415fb5be05029c7bb7a6f1a777feab980f368"
OUT = ROOT / "docs" / "evidence" / "20261002-d5af-acceptance-recheck.json"


def _body() -> str:
    raw = json.loads((WS / "report_versions.json").read_text(encoding="utf-8"))

    def walk(node):
        if isinstance(node, dict):
            yield node
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    for node in walk(raw):
        b = str(node.get("body") or "")
        if b and hashlib.sha256(b.encode("utf-8")).hexdigest() == TARGET:
            return b
    return ""


def main() -> int:
    import acceptance_checker as ac
    body = _body()
    sources = ac._collect_sources(str(WS))
    out = {
        "case": "ui-d5af8cae2f 两项验收失败的只读复验（选定候选正文冻结复算）",
        "task_id": "ui-d5af8cae2f",
        "candidate_body_sha256": TARGET,
        "candidate_body_chars": len(body),
        "read_only": True,
        "before_fix": {
            "entity_attribution": ("污染 3 处：占当期营业收入/占公司营业收入/消费税按照销售额"
                                   " = 10.0% 被判『网络源证据指向其他公司』"),
            "source_labeling": ("虚假标注 5 条：4 条 PDF 页码定位（第 75/76/77/20 页·合并利润表/"
                                "合并现金流量表·行「…」）＋1 条派生说明"
                                "「2024年 合并：营业收入 − 营业成本」"),
        },
        "after_fix": {},
        "source_channels": sorted(sources),
    }
    for name, fn in (("source_labeling", ac.check_source_labeling),
                     ("entity_attribution",
                      lambda r, s: ac.check_entity_attribution(
                          r, s, "研究洋河股份（002304.SZ）2023/2024 年报"))):
        res = fn(body, sources)
        out["after_fix"][name] = {
            "pass": bool(res.get("pass")), "details": str(res.get("details"))[:400],
            "mislabeled": list(res.get("mislabeled") or [])[:10],
            "contaminated": list(res.get("contaminated") or [])[:10],
        }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out["after_fix"], ensure_ascii=False, indent=1)[:800])
    print("证据：", OUT.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
