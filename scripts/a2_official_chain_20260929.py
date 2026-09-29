# -*- coding: utf-8 -*-
"""A2 真实链：**官方公告发现 → 正常导入 → 事实 → 适用模型**（三家一次跑完，读数进证据）。

这条链要证明的是"资料是可替换的"：不再依赖用户手工递 PDF，也不依赖全文搜索摘要——
按参数契约问官方公告接口 → 拿到原件 URL → 走**既有的**补材料入口与准入判据 →
用 A0 的表格抽取器把原文变成带定位/身份/口径的事实 → 冻结数据集 → 跑注册模型。

三个角色（缺一不可，且**不接受与拒绝都算行为证据**）：

- 三一重工 600031.SH：**第二家公司**，上交所，2024 年报经官方发现取得；
- 洋河股份 002304.SZ：**另一个不同布局的原件**（冻结样本那家公司，这次同样走发现链，
  用来检验"换个排版还认不认得出"）；
- 京蓝科技 000711.SZ：**反例**——2020 年报的更正版是 **2025 年**才发布的，
  它的存在恰恰证明"原版与更正版必须并存"，并演示截至日（as_of）判据如何拒绝它。

预算：每家 1 次公告查询（orgId 映射进程内缓存只取一次）+ 1 次原件下载，全部走
`net_policy` 既有通道；确定性链路（抽取/冻结/模型）零 LLM 调用。

用法：python scripts/a2_official_chain_20260929.py [--only 600031]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import material_intake as mi                                            # noqa: E402
from adapters import cninfo, disclosure_ingest as di                    # noqa: E402
from adapters import annual_financial_tables as aft                     # noqa: E402

OUT_DIR = ROOT / "evals" / "a2_official_chain_20260929"

COMPANIES = (
    {"slug": "600031", "company": "三一重工", "code": "600031.SH",
     "periods": (2023, 2024), "role": "第二家公司（上交所）"},
    {"slug": "002304", "company": "洋河股份", "code": "002304.SZ",
     "periods": (2023, 2024), "role": "不同布局原件（冻结样本公司，改走官方发现）"},
    {"slug": "000711", "company": "京蓝科技", "code": "000711.SZ",
     "periods": (2019, 2020), "role": "反例：重述/亏损（更正稿 2025 年才发布）"},
)


def _verdict(res: dict) -> dict:
    """准入结论里的 `verdict`（admit 的成功路径把它嵌在返回里）。"""
    got = res.get("verdict")
    return got if isinstance(got, dict) else {}


def _pick_body(candidates):
    """候选顺序交给统一策略（中文正文原版优先、同级最早披露优先），逐个试、逐个留痕。

    实机教训：洋河 2024 年报的关键词查询同时返回中文正文与**英文版**（2025-06-03），
    只看"披露日最新"会挑到英文版，然后被正文判据拒绝。拒绝是对的，挑选策略错了。
    """
    ordered = di.prefer_body_candidates(candidates)
    return ordered[0] if ordered else None


def discover_one(spec):
    got = di.discover(spec["company"], spec["code"], periods=spec["periods"])
    out = {"role": spec["role"], "company": spec["company"], "code": spec["code"],
           "periods": list(spec["periods"]),
           "discovery_status": got["status"], "reason_code": got.get("reason_code") or "",
           "reason": got.get("reason") or "", "contract": got.get("contract") or "",
           "params": got.get("params") or {}, "probe": got.get("probe") or {},
           "candidates": [{k: c.get(k) for k in
                           ("url", "title", "period", "version", "is_summary", "language",
                            "disclosed_at", "size_kb", "why")}
                          for c in got.get("candidates") or ()],
           "steps": [], "ok": False}
    return got, out


def import_and_admit(spec, candidate, out):
    """正常入口：材料落盘（`store`）→ 准入（`admit`，取件走 net_policy）。"""
    ws = OUT_DIR / spec["slug"]
    task_id = f"a2-{spec['slug']}-{spec['periods'][-1]}"
    stored = mi.store(task_id=task_id, channel=mi.CHANNEL_LINK, url=candidate["url"],
                      title=candidate["title"], period=str(candidate.get("period") or ""),
                      doc_type="年度报告", provenance=di.PROVENANCE_DISCOVERY,
                      note=f"A2 官方发现（契约 {cninfo.PARAM_CONTRACT}）", ws_dir=ws)
    out["steps"].append({"step": "store", "ok": bool(stored.get("ok")),
                         "material_id": stored.get("material_id"),
                         "duplicate": bool(stored.get("duplicate")),
                         "error": stored.get("error") or ""})
    if not stored.get("ok"):
        return None
    mid = str(stored["material_id"])
    as_of = str(candidate.get("disclosed_at") or "")
    res = mi.admit(task_id=task_id, mid=mid, company=spec["company"],
                   company_code=spec["code"], periods=spec["periods"], as_of=as_of,
                   doc_type="年度报告", ws_dir=ws)
    out["steps"].append({"step": "admit", "ok": bool(res.get("ok")),
                         "status": res.get("status"), "as_of": as_of,
                         "reason": res.get("reason") or "",
                         "detail": str(res.get("detail") or "")[:200],
                         "provenance": (_verdict(res).get("provenance") or ""),
                         "provenance_label": (_verdict(res).get("provenance_label") or ""),
                         "source_class": ((_verdict(res).get("doc") or {}).get("source_class")
                                          or ""),
                         "text_hash": _verdict(res).get("hash", ""),
                         "cutoff": _verdict(res).get("cutoff", {}),
                         "metric_states": res.get("metric_states") or {}})
    if not res.get("ok"):
        return None
    doc = mi.load_doc(task_id, mid, ws_dir=ws)
    return {"task_id": task_id, "mid": mid, "ws": str(ws), "doc": doc,
            "meta": mi.load(task_id, mid, ws_dir=ws)}


def facts_and_models(spec, handle, out, *, restatement: str = ""):
    """原文 → 事实（带定位/口径/身份）→ 冻结数据集 → 注册模型（零 LLM）。"""
    import financial_analysis as fa

    doc = dict(handle["doc"] or {})
    meta = handle["meta"] or {}
    doc["url"] = str(meta.get("url") or doc.get("url") or "")
    ds, raw = aft.to_dataset(doc, company=spec["company"], company_code=spec["code"],
                             periods=spec["periods"],
                             as_of=str(meta.get("disclosure_date") or ""),
                             restatement=restatement)
    reasons = Counter(str(r.get("reason")) for r in raw.get("rejected") or ())
    out["facts"] = {
        "ok": bool(raw.get("ok")), "entity_state": raw.get("entity_state"),
        "text_hash": raw.get("text_hash"), "lines": raw.get("lines"),
        "facts": len(raw.get("facts") or ()), "tables": len(raw.get("tables") or ()),
        "rejected_total": len(raw.get("rejected") or ()),
        "rejected_reasons": dict(reasons.most_common()),
        "sample": [{k: f.get(k) for k in ("metric", "period", "value", "unit",
                                          "caliber", "locator", "fact_id")}
                   for f in (raw.get("facts") or ())[:6]],
    }
    out["dataset"] = {
        "hash": ds.dataset_hash, "entity": ds.manifest.entity,
        "entity_id": ds.manifest.entity_id, "periods": list(ds.manifest.periods),
        "observations": ds.manifest.observations, "usable": ds.manifest.usable,
        "gaps": list(ds.manifest.gaps), "conflicts": list(ds.manifest.conflicts),
    }
    question = (f"{spec['company']}（{spec['code']}）"
                f"{'/'.join(str(p) for p in spec['periods'])} 年经营分析："
                "利润变化来自哪里、现金质量如何、营运资金占用怎样")
    plan = fa.compile_plan(question, ds, prefer=("profit_bridge",))
    adopted_ids = {a.model_id for a in plan.adopted}
    runs = []
    for item in plan.adopted:
        run = fa.run(item.model_id, ds, params=item.params, question=item.question)
        ok = bool((run.validation or {}).get("ok"))
        runs.append({"model_id": run.model_id, "adopted": True, "status": run.status,
                     "reason": run.reason, "validation_ok": ok,
                     "outputs": [{"metric": o.metric, "value": o.value, "unit": o.unit,
                                  "period": o.output_period} for o in run.outputs]})
    for item in (plan.rejected or ()):
        rid = str(item.get("model_id") or "") if isinstance(item, dict) else str(item)
        why = str(item.get("reason") or "") if isinstance(item, dict) else ""
        runs.append({"model_id": rid, "adopted": False, "status": "rejected",
                     "reason": why + (f"（缺 {item.get('missing')}）"
                                      if isinstance(item, dict) and item.get("missing")
                                      else ""),
                     "validation_ok": False, "outputs": []})
    out["models"] = {"adopted": sorted(adopted_ids), "runs": runs,
                     "validated": sum(1 for r in runs if r["status"] == fa.RunStatus.VALIDATED)}
    out["question"] = question
    out["ok"] = bool(raw.get("ok")) and any(r["status"] == fa.RunStatus.VALIDATED
                                            for r in runs)
    return ds, raw


def jinglan_restatement_demo(spec, got, out):
    """反例的正题：更正稿在 2021 年**还不存在**，两个截至日必须给出相反结论。

    这里必须显式用**更正版**（2025-09-05 披露）做演示：链路的默认候选是"最早的原版"
    （2021-04-27），拿它做重述演示等于换了个题目。两个版本都在 drill 工作区里落盘，
    互不覆盖——这正是"原版与更正版并存"的落地形态。
    """
    rows = []
    corrected = [c for c in (got.get("candidates") or ())
                 if c.get("version") == "corrected" and not c.get("is_summary")]
    if not corrected:
        out["restatement_demo"] = []
        out["as_of_window"] = {}
        return
    ws = OUT_DIR / f"{spec['slug']}-corrected"
    task_id = f"a2-{spec['slug']}-{spec['periods'][-1]}-corrected"
    cand = corrected[0]
    stored = mi.store(task_id=task_id, channel=mi.CHANNEL_LINK, url=cand["url"],
                      title=cand["title"], period=str(cand.get("period") or ""),
                      doc_type="年度报告", provenance=di.PROVENANCE_DISCOVERY,
                      note=f"A2 更正版演示（{cand['disclosed_at']}）", ws_dir=ws)
    out["restatement_material"] = {
        "url": cand["url"], "title": cand["title"], "version": cand["version"],
        "disclosed_at": cand["disclosed_at"],
        "material_id": stored.get("material_id"), "ok": bool(stored.get("ok")),
    }
    if not stored.get("ok"):
        out["restatement_demo"] = []
        return
    # 取件一次、建档一次，然后只改**截至日**做两次判定（这是纯判据问题，不牵扯存储层；
    # 走 admit 的话第一次被拒就不会落盘，第二次演示会变成"空文档"）
    try:
        got_raw = mi._fetch_link(cand["url"])
        doc, _parse = mi.build_doc(raw=got_raw["raw"], kind="pdf", url=cand["url"],
                                   title=cand["title"])
        doc = dict(doc or {}, url=cand["url"])
    except Exception as exc:                                    # noqa: BLE001
        out["restatement_demo"] = [{"error": f"{type(exc).__name__}: {str(exc)[:160]}"}]
        return
    rows = []
    for as_of in ("2021-12-31", "2025-12-31"):
        verdict = di.ingest(dict(doc, url=cand["url"]), company=spec["company"],
                            company_code=spec["code"], periods=spec["periods"],
                            as_of=as_of, provenance=di.PROVENANCE_DISCOVERY)
        rows.append({"as_of": as_of, "status": verdict.get("status"),
                     "reason": verdict.get("reason") or "",
                     "detail": str(verdict.get("detail") or "")[:200],
                     "cutoff": verdict.get("cutoff") or {}})
    out["restatement_demo"] = rows
    # 当时已知的那一版（原版，2021-04-27 披露）
    early = di.discover(spec["company"], spec["code"], periods=(2020,),
                        until="2021-12-31")
    out["as_of_window"] = {
        "until": "2021-12-31", "seDate": (early.get("params") or {}).get("seDate"),
        "candidates": [(c.get("period"), c.get("version"), c.get("disclosed_at"),
                        c.get("url")) for c in early.get("candidates") or ()],
    }


def run_company(spec):
    print(f"\n===== {spec['company']} {spec['code']} {spec['role']} =====", flush=True)
    got, out = discover_one(spec)
    print(f"发现：{out['discovery_status']} 契约={out['contract']} "
          f"候选={len(out['candidates'])} 原因={out['reason_code'] or '-'}", flush=True)
    ordered = di.prefer_body_candidates(got.get("candidates") or ())
    if not ordered:
        out["error"] = "没有可用候选"
        return out
    handle = None
    for idx, cand in enumerate(ordered, 1):
        print(f"[{idx}/{len(ordered)}] 试：{cand['period']} {cand['version']} "
              f"{'摘要' if cand['is_summary'] else '正文'} "
              f"lang={cand.get('language')} {cand['disclosed_at']} "
              f"{cand['size_kb']}KB\n      {cand['url']}", flush=True)
        attempted = import_and_admit(spec, cand, out)
        if attempted:
            handle = attempted
            break
        last = out["steps"][-1]
        last["candidate_rejected"] = True
        if str(last.get("status")) == mi.STATE_FETCH_FAILED:
            # **取件失败 ≠ 这份材料不合格**：网络/截止问题与"材料不适合研究"是两回事。
            # 重试一次（同一条有界通道），仍失败就停下并如实报缺口——
            # 不许悄悄降级去用**摘要**（摘要也进得来，但它撑不起模型，等于把缺口藏起来）。
            print(f"      取件失败：{str(last.get('reason'))[:100]} → 重试一次", flush=True)
            attempted = import_and_admit(spec, cand, out)
            out["steps"][-1]["candidate_rejected"] = True
            out["steps"][-1]["retried"] = True
            if attempted:
                handle = attempted
                break
            out["error"] = ("取件在不改变通道的前提下仍未成功：本轮不做摘要降级，"
                            "留作可重试的缺口（材料候选见上）")
            print("      仍然失败 → 停止该公司的链路（不降级到摘要）", flush=True)
            return out
        print(f"      被拒：{last.get('reason') or '见 steps'} "
              f"（{str(last.get('detail'))[:80]}）", flush=True)
    if not handle:
        print("全部候选都没通过准入——拒绝也是行为证据（见 steps）", flush=True)
        return out
    print(f"准入：{out['steps'][-1]['status']} 来源={out['steps'][-1]['provenance_label']} "
          f"正文hash={str(out['steps'][-1]['text_hash'])[:12]}", flush=True)
    facts_and_models(spec, handle, out)
    print(f"事实：{out['facts']['facts']} 条 / 拒 {out['facts']['rejected_total']} "
          f"（{out['facts']['rejected_reasons']}）", flush=True)
    print(f"数据集：{out['dataset']['observations']} 观察 periods="
          f"{out['dataset']['periods']} gaps={out['dataset']['gaps']}", flush=True)
    for r in out["models"]["runs"]:
        outs = "；".join(f"{o['metric']}={o['value']}{o['unit']}({o['period']})"
                         for o in r["outputs"]) or "—"
        print(f"  模型 {r['model_id']:<18} {r['status']:<10} "
              f"{'通过' if r['validation_ok'] else '未通过'} {outs}", flush=True)
        if r["reason"]:
            print(f"      理由：{str(r['reason'])[:120]}", flush=True)
    if spec["slug"] == "000711":
        jinglan_restatement_demo(spec, got, out)
        print("重述演示：", json.dumps(out["restatement_demo"], ensure_ascii=False)[:400],
              flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑某一家（slug）")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "contract": cninfo.PARAM_CONTRACT, "companies": []}
    cninfo.reset_cache()
    for spec in COMPANIES:
        if args.only and spec["slug"] != args.only:
            continue
        try:
            report["companies"].append(run_company(spec))
        except Exception as exc:                                # noqa: BLE001
            import traceback
            report["companies"].append({"slug": spec["slug"], "error":
                                        f"{type(exc).__name__}: {str(exc)[:200]}",
                                        "traceback": traceback.format_exc()[-1200:]})
            print(f"!! {spec['slug']} 失败：{type(exc).__name__}: {str(exc)[:200]}",
                  flush=True)
    report["elapsed_s"] = round(time.time() - started, 1)
    (OUT_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n==== 完成：{report['elapsed_s']}s，报告 {OUT_DIR / 'report.json'} ====",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
