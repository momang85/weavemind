#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""K3 质量对照：**同一份资料**上"旧装配稿 vs 现装配稿"的对照读数（只读、零模型调用）。

判据按验收单的说法：有效解释更多 / 重复更少 / 缺口具体——不是只数模型数、图数、页数。

用法：
    python scripts/k3_quality_compare.py [task_id]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import delivery_pipeline as dp          # noqa: E402
import report_version as rv             # noqa: E402
import task_state                        # noqa: E402
import workspace                         # noqa: E402


def _prose(text: str) -> list[str]:
    out = []
    for ln in str(text or "").splitlines():
        s = ln.strip()
        if not s or s.startswith(("#", ">", "|", "!", "- ", "* ")):
            continue
        out.append(s)
    return out


def _metrics(text: str) -> dict:
    body = str(text or "")
    heads = re.findall(r"^(#{1,6}) (.+)$", body, re.M)
    levels = Counter(len(h[0]) for h in heads)
    titles = Counter(h[1].strip() for h in heads)
    prose = _prose(body)
    dup_lines = [ln for ln, n in Counter(prose).items() if n > 1 and len(ln) > 12]
    dup_titles = {t: n for t, n in titles.items() if n > 1}
    adopted = re.search(r"正文引用 \*\*(\d+)\*\* 条|采用来源 (\d+) 条", body)
    cited = re.findall(r"^(\d+)\. \[", body, re.M)
    return {
        "chars": len(body),
        "h1": levels.get(1, 0),
        "h2": levels.get(2, 0),
        "h3": levels.get(3, 0),
        "headings_total": len(heads),
        "duplicate_headings": dup_titles,
        "prose_lines": len(prose),
        "duplicate_prose_lines": len(dup_lines),
        "duplicate_prose_sample": dup_lines[:3],
        "source_line": (adopted.group(0) if adopted else ""),
        "reference_entries": len(cited),
        "gaps_mentioned": body.count("缺口") + body.count("未取得") + body.count("待核查"),
        "cards": body.count("## 分析卡") + body.count("### 分析卡"),
        "next_actions": re.findall(r"下一项验证动作：([^\n]{0,60})", body),
    }


def main() -> int:
    tid = sys.argv[1] if len(sys.argv) > 1 else "ui-603f626cbe"
    row = task_state.read_task(tid) or {}
    goal = str(row.get("goal") or "")
    old = str(row.get("report") or "")
    ws = workspace.task_workspace(tid)
    adopted = rv.VersionStore(ws, tid).adopted()
    model_body = str(getattr(adopted, "body", "") or "")
    new = dp.research_candidate_body(tid, goal, model_body, project="default") or ""
    out = {"task_id": tid, "adopted_identity": (adopted.identity_id() if adopted else ""),
           "old": _metrics(old), "new": _metrics(new)}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
