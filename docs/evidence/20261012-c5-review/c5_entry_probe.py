# -*- coding: utf-8 -*-
"""C5-1 探针：**无主体 options 只返登记主体**（用户闭环的入口修复）。

覆盖（离线、进程内、不消耗外部额度；另附一次**对 8091 实例的环回认证探测**）：
  A 无 entity 的 `options` → 200：返回登记主体清单＋研究元数据＋`entity_required:true`，
    **不**默认取第一个主体、**不**扫材料、**不**求值、**不**建候选工作区（探针桩记账）
  B 无 entity 的 `evaluate`/`save` → 400 `entity_required`（其余动作保留主体要求）
  C 未知研究 → 404；result_hash 与冻结登记不一致 → 409；未知 action → 400
  D 环回：8091 真实实例 POST 同一端点（无会话）→ 401（鉴权仍在，不因新入口放松）

用法：`python -X utf8 docs/evidence/20261012-c5-review/c5_entry_probe.py`
"""
from __future__ import annotations

import io
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import web_ui as W                                    # noqa: E402

results: list[dict] = []
fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail[:400]})
    if not ok:
        fails.append(f"{name}: {detail[:200]}")


class _Stub:
    """只实现 `_post_research_study_experiment` 用到的接口；`_json` 记录状态码与载荷。"""

    def __init__(self):
        self.status = None
        self.payload = None

    def _json(self, data, code=200, extra_headers=None):
        self.status, self.payload = code, data
        return self.payload, code

    def _client_ip(self) -> str:
        return "127.0.0.1"


# 探针桩：记账 task_workspace / prepare_company_research_experiment / audit_log 调用
_calls = {"ws": [], "prepare": [], "audit": []}
_real_ws = W.task_workspace
import financial_analysis.research_experiment as _rxmod   # noqa: E402
_real_prep = _rxmod.prepare_company_research_experiment


def _spy_ws(tid, project="default"):
    _calls["ws"].append(tid)
    return _real_ws(tid, project=project)


def _spy_prepare(*a, **k):
    _calls["prepare"].append(k)
    return _real_prep(*a, **k)


def _spy_audit(*a, **k):
    _calls["audit"].append(str(k.get("target") or ""))
    return None


W.task_workspace = _spy_ws
_rxmod.prepare_company_research_experiment = _spy_prepare
W.audit_log = _spy_audit

YANGHE = "0af20919e6245b888974ee17321f327f99580ab745687e80aca6dae6921e7f4f"
YH_HASH = "0f21c7a36f0e28fd24d2b79634036ec736ca7cba18f58040b7621a27242b1cf3"

# ---------------------------------------------------------------- A options 无主体
s = _Stub()
W._post_research_study_experiment(
    s, None, {"action": "options", "study_id": YANGHE, "result_hash": YH_HASH}, {"user": "probe"})
p = s.payload or {}
check("A1 options 无主体 → 200 且 schema/主体清单/必须选主体齐备",
      s.status == 200 and p.get("schema") == "weavemind.c3_experiment_options/1"
      and isinstance(p.get("entities"), list) and "002304.SZ" in (p.get("entities") or [])
      and p.get("entity_required") is True,
      f"status={s.status} entities={p.get('entities')}")
check("A2 只返元数据：study 带 feature_cutoff/研究状态，不带任何求值读数",
      isinstance(p.get("study"), dict)
      and str((p.get("study") or {}).get("feature_cutoff") or "") == "2025-04-30"
      and not any(k in p for k in ("experiment", "cards", "market_comparison", "readings")),
      f"study keys={sorted((p.get('study') or {}).keys())}")
check("A3 不建候选工作区／不求值／不扫材料（探针桩零调用）",
      not _calls["ws"] and not _calls["prepare"],
      f"ws_calls={_calls['ws']} prepare_calls={len(_calls['prepare'])}")
check("A4 审计如实记 entities（target=<sid16>/entities）",
      any(str(t).endswith("/entities") for t in _calls["audit"]),
      f"audit={_calls['audit'][-2:]}")

# ---------------------------------------------------------------- B 其余动作仍要主体
for act in ("evaluate", "save"):
    s = _Stub()
    W._post_research_study_experiment(
        s, None, {"action": act, "study_id": YANGHE, "result_hash": YH_HASH}, {"user": "probe"})
    check(f"B1 无主体的 {act} → 400 entity_required",
          s.status == 400 and (s.payload or {}).get("code") == "entity_required",
          f"status={s.status} payload={json.dumps(s.payload, ensure_ascii=False)[:120]}")

# ---------------------------------------------------------------- C 形态与一致性
s = _Stub()
W._post_research_study_experiment(
    s, None, {"action": "options", "study_id": "deadbeef" * 6, "result_hash": YH_HASH},
    {"user": "probe"})
check("C1 未知研究 → 404", s.status == 404, f"status={s.status}")

s = _Stub()
W._post_research_study_experiment(
    s, None, {"action": "options", "study_id": YANGHE, "result_hash": "0" * 64},
    {"user": "probe"})
check("C2 result_hash 与冻结登记不一致 → 409",
      s.status == 409 and (s.payload or {}).get("code") == "hash_mismatch",
      f"status={s.status}")

s = _Stub()
W._post_research_study_experiment(
    s, None, {"action": "frobnicate", "study_id": YANGHE, "result_hash": YH_HASH,
              "entity_id": "002304.SZ"}, {"user": "probe"})
check("C3 未知 action → 400", s.status == 400, f"status={s.status}")

# ---------------------------------------------------------------- D 环回认证探测
live = ""
try:
    req = urllib.request.Request(
        "http://127.0.0.1:8091/api/research-study/experiment",
        data=json.dumps({"action": "options", "study_id": YANGHE,
                         "result_hash": YH_HASH}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        live = f"status={r.status}"
except urllib.error.HTTPError as exc:
    live = f"status={exc.code}"
    check("D1 8091 实例无会话 POST → 401（鉴权未放松）", exc.code == 401,
          f"live={live}")
except Exception as exc:                          # noqa: BLE001
    live = f"unreachable: {str(exc)[:60]}"
    check("D1 8091 实例无会话 POST → 401（鉴权未放松）", False, f"live={live}")

# ---------------------------------------------------------------- 汇总
out = {"scope": "C5-1 探针：无主体 options 只返登记主体（入口闭环修复）",
       "live_8091": live,
       "checks": results, "total": len(results),
       "passed": sum(1 for r in results if r["ok"]), "fails": fails}
(OUT := Path(__file__).resolve().parent).joinpath("c5_entry_results.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{out['passed']}/{out['total']} PASS  live_8091={live}")
for f in fails:
    print("FAIL:", f)
sys.exit(0 if not fails else 1)
