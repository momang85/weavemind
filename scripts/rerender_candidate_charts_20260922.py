# -*- coding: utf-8 -*-
"""D-1/C-2：为候选修订版**重新生成图表规格并重渲染**（同源、确定性、离线）。

- 规格由 `chart_specs.financial_research_specs` 从底稿（rows + derived）重新算出：
  带稳定 `chart_id` 与绑定块，同比全为负时措辞是"降幅最小/降幅最大"；
- 用**当前** `chart_assembly.RENDER_CHART_SCRIPT` 渲染（它把 chart_id/binding 写进
  `chart_manifest.json`），因此正文引用与清单、图三者同源；
- 只写工作区内的图表文件，不改正文与版本库。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("REDIS_PORT", "6399")

TID = sys.argv[1] if len(sys.argv) > 1 else "ui-706c5ef4a5"
WS = Path(os.environ["TEMP"]) / "agent_workspace" / "tasks" / "projects" / "default" / TID
PROJ = WS / "project"

import chart_assembly  # noqa: E402
import chart_specs as cs  # noqa: E402
from facts import CORE_METRICS  # noqa: E402

# 行数据走**生产同一条**通道（`working_paper_export.chart_rows`：只保留契约期间、
# 含同比与比率、带 year 字段），不要自己从底稿 rows 拼——底稿行只有 `period`
# 字符串，自拼会让"两期对比图"因缺 year 被跳过（本脚本第一版即如此）
import working_paper_export as WPX  # noqa: E402
data = WPX.chart_rows(TID, "")
assert data.get("ok"), data
specs = cs.financial_research_specs(
    data.get("rows") or [], data.get("derived") or [],
    unit=str(data.get("unit") or ""), source=str(data.get("source_label") or ""),
    company="洋河股份", caliber="合并", periods=list(data.get("periods") or []),
    core_metrics=[m for m, _ in CORE_METRICS])
print("specs:", len(specs))
for s in specs:
    print(" -", s.get("chart_id"), "|", str(s.get("conclusion"))[:70])

# 语义图（若有）保留：只替换确定性财务图
try:
    prev = json.loads((PROJ / "chart_data.json").read_text(encoding="utf-8"))
except Exception:
    prev = {}
prev_specs = [s for s in (prev.get("charts") or [])
              if isinstance(s, dict) and not str(s.get("chart_id") or "").startswith(
                  ("core_scale", "yoy_growth", "ratio_"))]
merged = specs + [s for s in prev_specs if "归母净利率" not in str(s.get("title"))]
(PROJ / "chart_data.json").write_text(
    json.dumps({"charts": merged}, ensure_ascii=False, indent=1), encoding="utf-8")

# 用当前渲染脚本（含 chart_id/binding 回填）重渲染
script = chart_assembly.RENDER_CHART_SCRIPT.replace("__REPO_ROOT__", str(REPO))
(PROJ / "render_charts.py").write_text(script, encoding="utf-8")
r = subprocess.run([sys.executable, "render_charts.py"], cwd=str(PROJ),
                   capture_output=True, text=True, timeout=900)
print("render rc:", r.returncode)
print((r.stdout or "")[-1200:])
print((r.stderr or "")[-600:])
man = json.loads((PROJ / "chart_manifest.json").read_text(encoding="utf-8"))
for c in man.get("charts") or []:
    print(" *", c.get("file"), "|", c.get("chart_id"), "|", str(c.get("observation"))[:60])
