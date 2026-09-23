# -*- coding: utf-8 -*-
"""第一批 B：用**生产装配入口**重建候选（当前采纳正文 + 当前已准入资料为输入）。

不再依赖某个历史 hash 做裁剪：直接调生产链路
`report_brief.build_structure` → `render_brief_markdown` → `rewrite_report_links`
（与 `assemble_and_verify` 同序），因此候选与线上装配同源，逐问题支持关系、参考来源、
缺口都按**当前资料**重算。

只读 + 内存计算 + 写一个临时候选文件；不联网、不调用模型、不改采纳版本。

用法：python scripts/candidate_from_material_20260923.py [task_id]
产物：C:/Users/ding0/AppData/Local/Temp/candidate_from_material_20260923.md（+ .json）
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REDIS_PORT", "6399")
TASK = sys.argv[1] if len(sys.argv) > 1 else "ui-706c5ef4a5"
OUT_MD = Path(r"C:\Users\ding0\AppData\Local\Temp\candidate_from_material_20260923.md")
OUT_JSON = Path(r"C:\Users\ding0\AppData\Local\Temp\candidate_from_material_20260923.json")


def main() -> int:
    import task_state as ts
    import workspace as ws_mod
    import report_brief as rb
    import delivery_pipeline as dp
    from report_version import VersionStore

    ws = Path(ws_mod.task_workspace(TASK))
    goal = str((ts.read_task(TASK) or {}).get("goal") or "")
    store = VersionStore(ws, TASK)
    adopted = store.adopted()
    if adopted is None:
        print("没有采纳版本", file=sys.stderr)
        return 2
    body = str(adopted.body or "")
    print("当前采纳:", adopted.version_id[:16], len(body), "字符")

    # 渲染器要的是**模型稿形状**的输入：只带"分析"一节。当前采纳稿已是装配过的简报，
    # 直接喂进去会"简报套简报"（实测 18,174 字符、两份关键发现）。这里只抽出
    # 渲染器**不会**自己生成的两块（结论、数据时效）作为分析输入；关键发现/研究问题/
    # 业务背景/财务对照/图表/风险与核查/附录/参考来源都由结构重生成。
    def _block(text: str, heading: str, stops: tuple[str, ...] = ()) -> str:
        """取 heading 到**下一个二级标题**之间的内容（stops 只作额外兜底）。

        实机反例：按固定 stops 取"数据时效"，会把正文里紧随其后的整段『变化解释』
        一起带进分析输入，渲染出来就是**两份变化解释**（一份未缩短的旧文 + 一份代码
        新渲染）。这里统一在下一个 `## ` 处截断。
        """
        i = text.find(heading)
        if i < 0:
            return ""
        rest_start = i + len(heading)
        rest = text[rest_start:]
        m = re.search(r"\n##\s", rest)
        j = rest_start + (m.start() if m else len(rest))
        for s in stops:
            k = text.find(s, rest_start)
            if 0 <= k < j:
                j = k
        return text[i:j].strip("\n")

    conclusion = _block(body, "## 结论")
    timeliness = _block(body, "## 数据时效")
    title = next((l.strip()[2:].strip() for l in body.split("\n")
                  if l.strip().startswith("# ")), "公司研究简报")
    analysis_input = "\n\n".join(x for x in (conclusion, timeliness) if x)
    render_input = f"# {title}\n\n## 分析\n\n{analysis_input}\n"
    print("渲染输入（分析一节）:", len(render_input), "字符 | 结论", bool(conclusion),
          "| 数据时效", bool(timeliness))

    structure = rb.build_structure(TASK, goal, render_input, ws_dir=ws)
    if not structure:
        print("build_structure 返回空（不是研究任务或缺底稿）", file=sys.stderr)
        return 2
    brief = rb.render_brief_markdown(structure, render_input, task_id=TASK)
    brief = dp.rewrite_report_links(brief, TASK)
    new_sha = hashlib.sha256(brief.encode("utf-8")).hexdigest()
    print("候选:", new_sha[:16], len(brief), "字符")

    # 逐问题支持关系（生产口径）
    qs = structure.get("research_questions") or []
    print("研究问题支持关系：")
    for q in qs:
        sup = q.get("support") or {}
        print(f"  {q.get('metric')}: kind={sup.get('kind') or ('explanation' if sup.get('has_evidence') else 'none')}"
              f" | has_evidence={bool(sup.get('has_evidence'))}"
              f" | locator={str(sup.get('locator') or '')[:52]}")
    cits = structure.get("citations") or []
    print("采用来源:", [(c.get("n"), str(c.get("title"))[:28], str(c.get("type"))) for c in cits])
    ev = structure.get("evidence") or {}
    print("资料: located=", ev.get("located"), "| missing=", ev.get("missing_labels"),
          "| fp=", ev.get("fingerprint"))

    # 差异清单（相对当前采纳正文）
    old_lines = body.split("\n")
    new_lines = brief.split("\n")
    added = [l for l in difflib.unified_diff(old_lines, new_lines, lineterm="")
             if l.startswith("+") and not l.startswith("+++")]
    removed = [l for l in difflib.unified_diff(old_lines, new_lines, lineterm="")
               if l.startswith("-") and not l.startswith("---")]
    print(f"差异：新增 {len(added)} 行 / 删除 {len(removed)} 行")
    for l in added[:12]:
        print("  +", l[1:100])
    for l in removed[:8]:
        print("  -", l[1:100])
    # 关键检查：正文里出现"初步背景依据"与新来源
    checks = {
        "background_only": "初步背景依据" in brief,
        "revenue_background": bool(re.search(r"收入变化的量价与结构依据", brief)),
        "source_annual": any("年度报告" in str(c.get("title")) or "年报" in str(c.get("title"))
                             for c in cits),
        "profit_reason_gap": "利润变化的分解" in brief,
        "api_chunk_locator": "api_chunk" in brief,
    }
    print("检查:", checks)
    OUT_MD.write_text(brief, encoding="utf-8")
    OUT_JSON.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
    print("wrote", OUT_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
