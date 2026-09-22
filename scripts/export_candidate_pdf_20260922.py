# -*- coding: utf-8 -*-
"""批次D-3：导出候选修订版的**实际 PDF**，核对清单 hash，渲染逐页 PNG 供目视。

走与 `/report.pdf` 端点同一条路径（`_task_pdf_bytes` → `markdown_to_pdf` +
`_write_export_manifest`），因此"清单里的 pdf hash"就是这份 PDF 的字节 hash。
"""
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("REDIS_PORT", "6399")

TID = sys.argv[1] if len(sys.argv) > 1 else "ui-706c5ef4a5"
OUT = Path(os.environ.get("WM_OUT") or (Path(os.environ["TEMP"]) / "wm_candidate_out"))
OUT.mkdir(parents=True, exist_ok=True)

import db_paths  # noqa: E402
import task_state  # noqa: E402
import workspace as ws_mod  # noqa: E402

WS_ROOT = Path(os.environ["TEMP"]) / "agent_workspace" / "tasks"
ws_mod.configure_workspace_root(str(WS_ROOT))
task_state.DB_PATH = db_paths.resolve_db_path()

import web_ui  # noqa: E402

pdf = web_ui._task_pdf_bytes(TID)
(OUT / "report.pdf").write_bytes(pdf)
man = json.loads((ws_mod.task_workspace(TID) / "export_manifest.json")
                 .read_text(encoding="utf-8"))
files = man.get("files") or {}
md_sha = str((files.get("markdown") or {}).get("sha256") or "")
pdf_sha = str((files.get("pdf") or {}).get("sha256") or "")
print("pdf bytes:", len(pdf), "sha256:", hashlib.sha256(pdf).hexdigest()[:16])
print("manifest pdf sha256:", pdf_sha[:16], "match:",
      pdf_sha == hashlib.sha256(pdf).hexdigest())
print("manifest markdown sha256:", md_sha[:16])
print("manifest status:", man.get("status"), "| draft:", man.get("draft"),
      "| report_version_id:", str(man.get("report_version_id"))[:16])
print("manifest research_state:", json.dumps(
    {k: v for k, v in ((man.get("research_state") or {})).items()
     if k in ("state", "stale", "mandatory_supported", "mandatory_total")},
    ensure_ascii=False))
print("manifest binding:", json.dumps(
    ((man.get("research_state") or {}).get("binding") or {}), ensure_ascii=False)[:220])
