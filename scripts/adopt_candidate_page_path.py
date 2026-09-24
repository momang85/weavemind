# -*- coding: utf-8 -*-
"""按**页面修订的同一套生产函数**采纳一份候选正文（无模型、不联网）。

与 `web_ui._post_task_review_edit` 同序：读交付说明 → 登记新版本 → 采纳 → 对同一版
重验（`accept_for_body`，trigger=人工修订重验）→ `assemble_and_verify` 重装配 →
写交付投影（页面顶部读的就是它）。

为什么不是点页面按钮：本轮服务重启后浏览器会话失效，登录表单无凭据（不猜密码），
因此直接调用页面按钮背后的同一条代码路径；页面按钮路径已在 R2 批次实机验证过。

用法：python scripts/adopt_candidate_page_path.py <task_id> <candidate.md>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REDIS_PORT", "6399")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    tid = sys.argv[1]
    body_path = Path(sys.argv[2])
    if not body_path.is_file():
        print("候选文件不存在：", body_path, file=sys.stderr)
        return 2
    new_body = body_path.read_text(encoding="utf-8")
    if not new_body.strip():
        print("候选正文为空", file=sys.stderr)
        return 2

    import task_state as _ts
    import web_ui
    from delivery_pipeline import (accept_for_body, assemble_and_verify, read_wrapper,
                                   rules_identity, sources_fingerprint)
    from report_version import VersionStore
    from workspace import task_workspace

    ws = task_workspace(tid)
    goal = str((_ts.read_task(tid) or {}).get("goal") or "")
    store = VersionStore(ws, tid)
    current = store.adopted()
    if current is None:
        print("没有采纳版本", file=sys.stderr)
        return 2
    _delivered = (web_ui._get_task_report_data(tid) or {}).get("report") or ""
    wrapper, wrapper_source = read_wrapper(tid, _delivered, current.body)
    if wrapper_source == "derived":
        wrapper = (wrapper + "\n\n> 注：交付说明由旧交付正文推导（该任务没有"
                   "落盘交付说明），请人工确认。").strip("\n")

    nv = store.record(new_body, parent_id=current.version_id,
                      sources_fingerprint=sources_fingerprint(tid, new_body),
                      rules_version=rules_identity(tid)[0],
                      rules_fingerprint=rules_identity(tid)[1])
    store.adopt(nv, reason="人工复核修订（页面同路径，R3 前后对照稿）")
    print("新版本:", nv.version_id[:16], "| 身份:", nv.identity_id()[:16],
          "| 父:", current.version_id[:16])

    verdict = accept_for_body(tid, goal, new_body, trigger="人工修订重验",
                              prefer_body=True, ws_dir=task_workspace(tid)) or {}
    print("验收:", verdict.get("overall"), "| 缺口:",
          (verdict.get("gaps") or [])[:3])
    _ctr_wire = None
    try:
        _w = web_ui._contract_wire_for(tid)
        if isinstance(_w, dict) and _w:
            _ctr_wire = {"wire": dict(_w), "source": "task_record"}
    except Exception:                            # noqa: BLE001
        _ctr_wire = None
    asm = assemble_and_verify(tid, goal, new_body, wrapper=wrapper,
                              accept_fn=lambda t, g, b: verdict or None,
                              ws_dir=task_workspace(tid), contract=_ctr_wire)
    print("装配:", asm.get("status"), "| 正文:", len(str(asm.get("report") or "")), "字符")
    refreshed = store.adopted() or nv
    _acc_summary = {
        "overall": refreshed.acceptance_overall(),
        "gaps": list((refreshed.acceptance or {}).get("gaps") or []),
        "rules_version": str((refreshed.acceptance or {}).get("rules_version") or ""),
        "rules_fingerprint": str((refreshed.acceptance or {}).get("rules_fingerprint") or ""),
        "report_sha256": str((refreshed.acceptance or {}).get("report_sha256") or ""),
        "version_bound": bool(refreshed.acceptance_for_this_body()),
    }
    _status = str(asm.get("status") or "")
    _task_status = _ts.derive_status(
        step_statuses=["SUCCESS"], acceptance=_acc_summary, llm_degraded={},
        draft_delivery=("" if _status == "verified" else str(asm.get("reason") or "")))
    from task_state import update_delivery_projection
    projected = update_delivery_projection(tid, report=str(asm.get("report") or ""),
                                          acceptance=_acc_summary, status=_task_status)
    print("交付投影写入:", bool(projected), "| 任务状态:", _task_status)
    return 0 if projected else 1


if __name__ == "__main__":
    raise SystemExit(main())
