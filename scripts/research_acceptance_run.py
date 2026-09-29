# -*- coding: utf-8 -*-
"""C 批的一次有界实机验收脚本：提交一条两年度单公司研究任务并逐项核对。

为什么用服务端 publish 而不是浏览器表单：本机 UI 需要登录会话，而运行侧没有可用
凭据（也不应复制会话 token）。本脚本调用的是 `POST /api/tasks` 在鉴权之后走的**同一个**
函数 `web_ui._publish_task`，并送出前端 `buildResearchGoal` 会生成的同一份
`research_request`（先过 `_sanitize_research_request` 白名单）。因此未覆盖的只有
"表单字段 → payload"这一层（由前端守卫与渲染检查覆盖）。

用法：
    python scripts/research_acceptance_run.py --submit          # 提交并等待
    python scripts/research_acceptance_run.py --verify <task_id> # 只核对已有任务
    python scripts/research_acceptance_run.py --revise <task_id> --append "..."
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COMPANY = "贵州茅台"
COMPANY_ID = "600519.SH"
MARKET = "cn"
CALIBER = "合并"
AS_OF = "2025-04-30"
YEARS = (2023, 2024)

# 与 frontend/src/lib/researchGoal.ts 的 buildResearchGoal 一致
GOAL = (
    f"研究{COMPANY} {YEARS[0]} 与 {YEARS[1]} 两个年度的营业收入、归母净利润、"
    f"经营活动现金流净额，{CALIBER}报表口径，数据截至 {AS_OF}。"
    "每个数字须能回溯到来源位置并可重算；缺证据的如实标缺口。"
)
FIELDS = {
    "company": COMPANY,
    "company_id": COMPANY_ID,
    "market": MARKET,
    "caliber": CALIBER,
    "as_of": AS_OF,
    "year_from": YEARS[0],
    "year_to": YEARS[1],
    "periods": list(YEARS),
}
REQUIRED = ("revenue", "net_profit", "operating_cashflow")


def submit() -> str:
    import web_ui
    fields = web_ui._sanitize_research_request(FIELDS)
    assert fields.get("company_id") == COMPANY_ID, fields
    assert fields.get("periods") == list(YEARS), fields
    out = web_ui._publish_task(goal=GOAL, project="default", auto_run=True,
                               report_confirm=False, user_id="admin",
                               research_request=fields)
    return str(out.get("task_id") or "")


def wait_terminal(tid: str, *, timeout: float = 1500.0) -> dict:
    import task_state
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        row = task_state.read_task(tid) or {}
        status = str(row.get("status") or "")
        if status != last:
            print(f"[{int(time.time() - t0):4d}s] status={status} phase={row.get('phase')}",
                  flush=True)
            last = status
        if status in ("SUCCESS", "SUCCESS_WITH_ISSUES", "FAILED", "CANCELLED"):
            return row
        time.sleep(5)
    row = task_state.read_task(tid) or {}
    row["_timeout"] = True
    return row


def _paper(tid: str) -> dict:
    import workspace
    p = workspace.task_project_dir(tid, "default") / "working_paper.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _financials(tid: str) -> dict:
    import workspace
    p = workspace.task_project_dir(tid, "default") / "financials.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def verify(tid: str, *, export: bool = False) -> dict:
    """逐项核对验收清单。

    export=False（默认）**只读**：读既有台账/包身份，不生成 PDF、不重写清单。
    export=True 走真实导出口径（会重写 export_manifest.json）——两者分开，
    避免验收动作本身改变了被验收对象（K2）。
    """
    import delivery_pipeline
    import task_state
    import workspace
    from report_version import VersionStore, body_hash

    row = task_state.read_task(tid) or {}
    ws = workspace.task_workspace(tid)
    store = VersionStore(ws, tid)
    adopted = store.adopted()
    paper = _paper(tid)
    fin = _financials(tid)
    deliveries = store.deliveries()
    last = deliveries[-1] if deliveries else {}
    state = delivery_pipeline.delivery_state(tid, str(row.get("report") or ""), ws_dir=ws)

    # ① 六项必需事实 + 来源位置
    rows = [r for r in (paper.get("rows") or [])
            if str(r.get("metric") or "") in REQUIRED
            and str(r.get("period") or "") in (f"{YEARS[0]}年", f"{YEARS[1]}年")]
    facts_ok = len(rows) >= len(REQUIRED) * 2
    with_source = [r for r in rows if r.get("source_url") or r.get("source")]
    # ② 同比（相邻年度、单位 %）
    yoy = [d for d in (paper.get("derived") or []) if str(d.get("unit") or "") == "%"]
    # ③ 缺口与问题
    gaps = [g for g in (paper.get("gaps") or []) if g.get("kind") == "fact"]
    # ④ 导出四件套 + manifest：**默认只读**（K2，2026-09-29 夜验收：本函数此前自称只读，
    #    却调用 PDF/MD 导出并重写清单——"验收动作本身改变了被验收对象"）。`--export` 时
    #    才走真实导出（按真实字节写清单），并如实标注本次运行改写过清单。
    exports: dict = {}
    if export:
        try:
            import web_ui
            pdf = web_ui._task_pdf_bytes(tid)             # 生成 PDF 并按真实字节写清单
            md_bytes, manifest = web_ui._task_markdown_export(tid)
            csv_path = workspace.task_project_dir(tid, "default") / "working_paper.csv"
            json_path = workspace.task_project_dir(tid, "default") / "working_paper.json"
            exports = {
                "mode": "export（本次运行重写了 export_manifest.json 与 PDF/MD 台账）",
                "manifest": {k: manifest.get(k) for k in
                             ("report_version_id", "status", "aligned", "draft",
                              "body_sha256", "final_content_matches")},
                "files": manifest.get("files"),
                "pdf_bytes": len(pdf),
                "markdown_bytes": len(md_bytes),
                "markdown_has_body": str(row.get("report") or "")[:60] in
                md_bytes.decode("utf-8", "ignore"),
                "csv_present": csv_path.exists() and csv_path.stat().st_size > 0,
                "json_present": json_path.exists() and json_path.stat().st_size > 0,
                "csv_bytes": csv_path.stat().st_size if csv_path.exists() else 0,
                "json_bytes": json_path.stat().st_size if json_path.exists() else 0,
            }
        except Exception as exc:
            exports = {"mode": "export", "error": str(exc)[:200]}
    else:
        # 只读口径：读**已存在**的清单与磁盘上的导出件，不生成、不改写
        try:
            _mf = ws / "export_manifest.json"
            manifest = json.loads(_mf.read_text(encoding="utf-8")) if _mf.exists() else {}
            _pdf = ws / "reports" / "report.pdf"
            _md = ws / "reports" / "report.md"
            csv_path = workspace.task_project_dir(tid, "default") / "working_paper.csv"
            json_path = workspace.task_project_dir(tid, "default") / "working_paper.json"
            exports = {
                "mode": "read-only（只读既有台账，未生成/未改写）",
                "manifest": {k: manifest.get(k) for k in
                             ("report_version_id", "status", "aligned", "draft",
                              "body_sha256", "final_content_matches")},
                "files": manifest.get("files"),
                "pdf_bytes": _pdf.stat().st_size if _pdf.exists() else 0,
                "markdown_bytes": _md.stat().st_size if _md.exists() else 0,
                "markdown_has_body": bool(_md.exists()),
                "csv_present": csv_path.exists() and csv_path.stat().st_size > 0,
                "json_present": json_path.exists() and json_path.stat().st_size > 0,
                "csv_bytes": csv_path.stat().st_size if csv_path.exists() else 0,
                "json_bytes": json_path.stat().st_size if json_path.exists() else 0,
            }
        except Exception as exc:
            exports = {"mode": "read-only", "error": str(exc)[:200]}
    # ④b 交付包身份（K2）：逐个包标 current/historical，页面/下载据此对齐采纳身份
    packages: dict = {}
    try:
        import delivery_pipeline as _dp
        packages = _dp.package_statuses(tid, ws_dir=ws)
    except Exception as exc:
        packages = {"error": str(exc)[:200]}
    # ⑤ LLM 调用形状（脱敏）
    calls = {}
    lc = ws / "llm_calls.jsonl"
    if lc.exists():
        events = [json.loads(x) for x in lc.read_text(encoding="utf-8").splitlines()
                  if x.strip()]
        calls = {"count": len(events),
                 "failed": sum(1 for e in events
                               if str(e.get("end_reason") or "") != "ok"),
                 "by_stage": {s: sum(1 for e in events if e.get("stage") == s)
                              for s in {str(e.get("stage") or "") for e in events}},
                 "total_elapsed_ms": sum(int(e.get("elapsed_ms") or 0) for e in events),
                 "max_input_chars": max((int(e.get("input_chars") or 0)
                                         for e in events), default=0)}
    md = dict(fin.get("metadata") or {})
    return {
        "task_id": tid,
        "verify_mode": "export" if export else "read-only",
        "status": row.get("status"),
        "phase": row.get("phase"),
        "delivery": {"status": state.get("status"), "draft": state.get("draft"),
                     "draft_reason": state.get("draft_reason"),
                     "hard_fail": state.get("hard_fail"),
                     "review_valid": state.get("review_valid"),
                     "aligned": state.get("aligned")},
        "version": {"identity_id": adopted.identity_id() if adopted else "",
                    "version_id": adopted.version_id if adopted else "",
                    "acceptance_overall": adopted.acceptance_overall() if adopted else "",
                    "acceptance_for_this_body": bool(adopted and adopted.acceptance_for_this_body())},
        "delivery_record": {"ok": last.get("ok"), "reason": last.get("reason"),
                            "delivered_sha256": str(last.get("delivered_sha256") or "")[:16],
                            "matches_body": bool(
                                last and adopted
                                and str(last.get("delivered_sha256") or "") == body_hash(
                                    str(row.get("report") or "")))},
        "facts": {"required_rows": len(rows), "six_ok": facts_ok,
                  "with_source_locator": len(with_source),
                  "sample": [{"metric": r.get("metric_label"), "period": r.get("period"),
                              "value": r.get("value"), "unit": r.get("unit"),
                              "source": str(r.get("source_url") or "")[:80]}
                             for r in rows[:6]],
                  "yoy_rows": len(yoy),
                  "yoy_sample": [{"metric": d.get("metric_label"),
                                  "period": d.get("period"),
                                  "value": d.get("value"), "unit": d.get("unit"),
                                  "from": d.get("derived_from")}
                                 for d in yoy[:3]],
                  "fact_gaps": len(gaps),
                  "problems": len(paper.get("problems") or []),
                  "paper_ok": paper.get("ok"),
                  "request": paper.get("request", {}).get("company"),
                  "request_periods": paper.get("request", {}).get("periods")},
        "eastmoney": {k: md.get(k) for k in
                      ("source", "company", "stock_code", "currency", "unit", "period",
                       "latest_report", "disclosure_date", "annual_count")},
        "contract_in_financials": fin.get("contract"),
        "exports": exports,
        "packages": packages,
        "llm_calls": calls,
        "review_state": json.loads((ws / "review_state.json").read_text(encoding="utf-8"))
        if (ws / "review_state.json").exists() else {},
    }


def revise(tid: str, *, find: str = "", replace: str = "") -> dict:
    """一次人工修订：走 `POST /api/task/<id>/review/edit` 的同一个函数。"""
    import web_ui

    class _H:
        def __init__(self):
            self.headers: dict = {}
            self.last: list = []

        def _client_ip(self):
            return "127.0.0.1"

        def _json(self, payload, code=200):
            self.last = [payload, code]
            return payload

    h = _H()
    body = {"find": find, "replace": replace} if find else {"body": replace}
    web_ui._post_task_review_edit(h, f"/api/task/{tid}/review/edit", body,
                                 {"user": "admin", "role": "admin"})
    payload, code = (h.last or [{}, 0])
    return {"http": code, "response": payload}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--submit", action="store_true")
    ap.add_argument("--verify")
    ap.add_argument("--revise")
    ap.add_argument("--find", default="")
    ap.add_argument("--append", default="")
    ap.add_argument("--wait", type=float, default=1500.0)
    ap.add_argument("--export", action="store_true",
                    help="核对时走真实导出（会重写 export_manifest.json）；默认只读")
    args = ap.parse_args()
    if args.submit:
        tid = submit()
        print("submitted:", tid, flush=True)
        row = wait_terminal(tid, timeout=args.wait)
        print("terminal:", row.get("status"), "timeout" if row.get("_timeout") else "")
        print(json.dumps(verify(tid, export=args.export), ensure_ascii=False, indent=1))
        return 0
    if args.revise:
        print(json.dumps(revise(args.revise, find=args.find, replace=args.append),
                         ensure_ascii=False, indent=1)[:2500])
        print(json.dumps(verify(args.revise), ensure_ascii=False, indent=1))
        return 0
    if args.verify:
        print(json.dumps(verify(args.verify, export=args.export), ensure_ascii=False, indent=1))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
