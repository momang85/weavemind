# -*- coding: utf-8 -*-
"""D2 样本冻结：从实机工作区抽出**新洋河原始草稿**的可复核片段（离线、只读）。

为什么单独一个跑法：D2 要求"冻结新洋河原始草稿、含表格加解释的样本，断言有效解读、
引用和限制语句保留且不重复"。原始草稿有 4600+ 字，直接进仓库太大；这里按小节抽出
"表格 + 表后解读"的那几块（保留原文与字符区间），并记下来源任务/版本/正文 hash，
便于离线复验与跨修订比对。

用法：python scripts/d2_freeze_samples.py [task_id]
产物：evals/brief/d2_analysis_samples.json
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("WM_CASE_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

import workspace as ws_mod                 # noqa: E402

OUT = Path(os.environ.get("WM_CASE_OUT") or (ROOT / "evals" / "brief"))
DEFAULT_TASK = "ui-31305a2b28"
# 关注的版本：反思候选稿（进入交付的那一版的分析来源）与重做稿
WANTED = {"da62d9e4": "反思候选稿（交付稿的分析来源）",
          "eb4efbcb": "重做稿（反思单步重做的产出）"}
MAX_CHARS = 7000


def _sections_of(text: str) -> list[tuple[str, str]]:
    """按标题切块 → [(标题行, 块正文)]；首块标题为空串。"""
    blocks: list[tuple[str, list[str]]] = [("", [])]
    for line in str(text or "").splitlines():
        if line.strip().startswith("#"):
            blocks.append((line.strip(), []))
        else:
            blocks[-1][1].append(line)
    return [(t, "\n".join(ls).strip()) for t, ls in blocks]


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    tid = (argv[0] if argv else DEFAULT_TASK).strip() or DEFAULT_TASK
    ws = ws_mod.task_workspace(tid)
    rv = ws / "report_versions.json"
    if not rv.exists():
        print(f"找不到 {rv}（任务工作区不在本机？）", file=sys.stderr)
        return 2
    data = json.loads(rv.read_text(encoding="utf-8"))
    samples = []
    for v in (data.get("versions") or {}).values():
        vid = str(v.get("version_id") or "")
        if vid[:8] not in WANTED:
            continue
        body = str(v.get("body") or "")
        secs = _sections_of(body)
        # 抽"含表格的块 + 紧随其后的解读块"：保留原文与区间
        picked: list[dict] = []
        total = 0
        for i, (title, text) in enumerate(secs):
            has_table = any(ln.strip().startswith("|") for ln in text.splitlines())
            nxt = secs[i + 1] if i + 1 < len(secs) else ("", "")
            next_is_prose = bool(nxt[1]) and not any(
                ln.strip().startswith("|") for ln in nxt[1].splitlines())
            if not (has_table or (next_is_prose and i > 0)):
                continue
            chunk = (f"{title}\n{text}\n" if title else text) + (
                f"\n{nxt[0]}\n{nxt[1]}\n" if next_is_prose else "")
            if total + len(chunk) > MAX_CHARS:
                continue
            total += len(chunk)
            picked.append({"title": title or "(开头)", "chars": len(chunk), "text": chunk})
        if not picked:
            continue
        excerpt = "\n".join(p["text"] for p in picked)
        samples.append({
            "id": f"yanghe_draft_{vid[:8]}",
            "note": WANTED[vid[:8]],
            "version_id": vid,
            "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            "excerpt_chars": len(excerpt),
            "blocks": picked,
            "body": excerpt,
        })
    payload = {"source": {"task_id": tid, "note": "洋河 2024 实机草稿的冻结片段（只读抽取）"},
               "samples": samples}
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "d2_analysis_samples.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {out}：{len(samples)} 个样本，"
          f"{sum(s['excerpt_chars'] for s in samples)} 字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
