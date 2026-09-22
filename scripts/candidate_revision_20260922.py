# -*- coding: utf-8 -*-
"""批次C-1/D-3：对实机采纳正文做**新候选修订**（保留历史版本），再导出 PDF 逐页目视。

两处修订（反例来自 09-22 复核 §3.3 / §5.2）：
1. 固定费用缺证归因 → 改成"待核查的假设"措辞（不再写"主要来自"）；
2. 资产负债率下降的绝对额解释 → 按两期水平复算（L/A 两期 + 负债率变化），
   并明确"绝对额减幅大小不构成比率变化的直接原因"。

不改旧版本：走与人工修订同一条共享实现（记录新版本 → 采纳 → 重验 → 装配），
旧正文仍在版本库里。全程不调用模型。
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("REDIS_PORT", "6399")

TID = sys.argv[1] if len(sys.argv) > 1 else "ui-706c5ef4a5"
WS = Path(os.environ.get("WM_WS") or
          Path(os.environ["TEMP"]) / "agent_workspace" / "tasks" / "projects" / "default" / TID)
os.environ["WEAVEMIND_WORKSPACE_ROOT"] = str(WS.parent.parent.parent)

import db_paths  # noqa: E402
import task_state  # noqa: E402
import workspace as ws_mod  # noqa: E402

ws_mod.configure_workspace_root(str(WS.parent.parent.parent))
# 任务库路径**必须**用仓库里的解析结果（与运行中的服务同一份 agents.db）：
# 自造路径会读不到落库的研究契约，底稿会被按"无契约"重算成空底稿
task_state.DB_PATH = db_paths.resolve_db_path()
print("workspace:", ws_mod.task_workspace(TID), "| db:", task_state.DB_PATH)

from report_version import VersionStore  # noqa: E402

store = VersionStore(ws_mod.task_workspace(TID), TID)
cur = store.adopted()
assert cur is not None, "没有采纳版本"
body = cur.body
row = {}
try:
    row = task_state.read_task(TID) or {}
except Exception as exc:
    print("read_task failed:", exc)
goal = str(row.get("goal") or "")
print("adopted:", cur.version_id[:16], "body", len(body), "chars")

FIX_33_OLD = ("因此净利率降幅大于毛利率降幅，主要来自收入规模下降对固定性费用摊薄的削弱，"
              "而非毛利线以下项目的绝对恶化——该判断的进一步拆分**原因未在本次资料中体现**，"
              "需核查\"合并利润表\"期间费用明细小节。")
FIX_33_NEW = ("净利率降幅大于毛利率降幅的**具体原因，本次资料无法判定**："
              "本次没有期间费用（销售/管理/研发）与成本结构的科目明细，"
              "因此**不把差额归到任何一项费用或损益**。作为**待核查的假设**："
              "若收入下降而固定性费用未同步收缩，费用率上升会放大净利率降幅——"
              "该假设需核查\"合并利润表\"期间费用明细小节后才能成立。")

# §5.2：资产负债率＝负债/资产 → 负债是**分子**。上一版写成"分母降得更快"是反的
# （09-22 晚间复核 P1：两期比率算对了也不能豁免随后的错误解释）
FIX_52_OLD = ("（本例负债降幅约 11.78%、资产约 3.51%，分母降得更快，比率才下降）")
FIX_52_NEW = ("（资产负债率＝负债/资产：本例负债降幅约 11.78%、资产约 3.51%，"
              "**分子降得更快**，比率才下降）")

FIX_24_OLD = ("毛利润降幅（-38.01 亿元）小于营业收入降幅（-42.50 亿元），"
              "说明毛利端下滑幅度略小于收入端，毛利率的下降是两者共同作用的结果")
FIX_24_NEW = ("毛利率按两期水平复算：2023 年 249.26/331.26 = 75.25%，"
              "2024 年 211.25/288.76 = 73.16%，下降 2.09 个百分点。"
              "**绝对额的减幅大小不构成毛利率变化的直接原因**：比率取决于两者的"
              "相对变化（本例毛利润降幅约 15.25%、营业收入约 12.83%，分子降得更快，"
              "比率才下降）——绝对额上毛利润减得更少并不表示毛利端更稳")

# 09-22 晚间复核 P1：模型正文的图引用整体错位（chart_2 被说成"两年对比"、chart_5 被
# 说成"现金覆盖"……），按**清单里的真实 chart_id** 重映射；不删句子，只改指向。
_ALIAS_FIXES = (
    ("**关键数据一览表所对应的核心指标走势，如图 chart_1 所示。**",
     "**关键数据一览表所对应的核心指标两年规模对比，如图 chart_1 所示"
     "（core_scale：营业收入、归母净利润、经营活动现金流净额）。**"),
    ("**收入、利润、经营现金流三项核心指标的两年对比，如图 chart_2 所示；"
     "同比降幅对比，如图 chart_3 所示。**",
     "**收入、利润、经营现金流三项核心指标的两年对比，如图 chart_1 所示"
     "（core_scale）；同比降幅对比，如图 chart_2 所示（yoy_growth）。**"),
    ("**毛利率与归母净利率的两年对比，如图 chart_4 所示。**",
     "**毛利率与归母净利率的两年对比，如图 chart_3 所示（ratio_net_margin）。**"),
    ("**经营现金流净额与归母净利润的覆盖关系，如图 chart_5 所示。**",
     "**经营现金流净额与归母净利润的覆盖关系，如图 chart_4 所示"
     "（ratio_cashflow_coverage）。**"),
    ("**资产负债率与总资产、总负债的两年对比，如图 chart_6 所示。**",
     "**资产负债率与总资产、总负债的两年对比，如图 chart_5 所示"
     "（ratio_debt_ratio）。**"),
)

# 幂等：已修订过的正文不再重复替换（重跑本脚本只做重验与装配）
new_body = body
for old, new in ((FIX_24_OLD, FIX_24_NEW), (FIX_33_OLD, FIX_33_NEW),
                 (FIX_52_OLD, FIX_52_NEW)):
    pass
for old, new in ((FIX_24_OLD, FIX_24_NEW), (FIX_33_OLD, FIX_33_NEW),
                 (FIX_52_OLD, FIX_52_NEW)):
    if old in new_body:
        new_body = new_body.replace(old, new)
    elif new in new_body:
        print("已修订（幂等跳过）:", old[:24])
    else:
        print("!! 原句与修订句都未命中:", old[:40])
        raise SystemExit(2)
for _o, _n in _ALIAS_FIXES:
    if _o in new_body:
        new_body = new_body.replace(_o, _n)
    elif _n in new_body:
        print("图引用已重映射（幂等跳过）:", _o[:24])
    else:
        print("!! 图引用原句未命中:", _o[:40])
print("new body:", len(new_body), "chars")

from delivery_pipeline import (accept_for_body, assemble_and_verify, read_wrapper,  # noqa: E402
                              read_research_state, rules_identity,
                              sources_fingerprint)

_delivered = ""
try:
    from web_ui import _get_task_report_data
    _delivered = (_get_task_report_data(TID) or {}).get("report") or ""
except Exception as exc:
    print("delivered report read failed:", str(exc)[:120])
wrapper, wrapper_source = read_wrapper(TID, _delivered, cur.body)
print("wrapper source:", wrapper_source, len(wrapper))

from report_version import body_hash  # noqa: E402
if body_hash(new_body) == cur.version_id:
    nv = cur
    print("正文已是修订版，不再新建版本:", nv.version_id[:16])
else:
    nv = store.record(new_body, parent_id=cur.version_id,
                      sources_fingerprint=sources_fingerprint(TID, new_body),
                      rules_version=rules_identity(TID)[0],
                      rules_fingerprint=rules_identity(TID)[1])
    store.adopt(nv, reason="09-22 复核：两项金融反例的候选修订（保留历史版本）")
    print("new version:", nv.version_id[:16])

verdict = accept_for_body(TID, goal, new_body, trigger="候选修订重验",
                          prefer_body=True, ws_dir=ws_mod.task_workspace(TID)) or {}
print("acceptance:", verdict.get("overall"), "gaps:",
      json.dumps(verdict.get("gaps"), ensure_ascii=False)[:300])

_prev = read_research_state(TID, ws_dir=str(ws_mod.task_workspace(TID))) or {}
_w = (_prev.get("binding") or {}).get("contract")
_ctr = {"wire": dict(_w)} if isinstance(_w, dict) and _w else None
asm = assemble_and_verify(TID, goal, new_body, wrapper=wrapper,
                          accept_fn=lambda t, g, b: verdict or None,
                          ws_dir=ws_mod.task_workspace(TID), contract=_ctr)
print("delivery:", asm.get("status"), "|", str(asm.get("reason"))[:120])
# 与 web_ui 的人工修订路径**逐项一致**：装配后同步交付投影（页面/导出读的就是它），
# 否则导出端点仍按旧投影出 PDF（旧正文），"同版交付"就无从谈起
try:
    from task_state import derive_status, update_delivery_projection
    _acc = {
        "overall": nv.acceptance_overall(),
        "gaps": list((nv.acceptance or {}).get("gaps") or []),
        "rules_version": str((nv.acceptance or {}).get("rules_version") or ""),
        "rules_fingerprint": str((nv.acceptance or {}).get("rules_fingerprint") or ""),
        "report_sha256": str((nv.acceptance or {}).get("report_sha256") or ""),
        "version_bound": bool(nv.acceptance_for_this_body()),
    }
    _status = str(asm.get("status") or "")
    _ts_status = derive_status(
        step_statuses=["SUCCESS"], acceptance=_acc, llm_degraded={},
        draft_delivery=("" if _status == "verified" else str(asm.get("reason") or "")))
    projected = update_delivery_projection(
        TID, report=str(asm.get("report") or ""), acceptance=_acc,
        status=_ts_status)
    print("projection updated:", bool(projected), "| task status:", _ts_status)
except Exception as exc:                     # noqa: BLE001 - 投影失败必须可见
    print("!! 交付投影更新失败:", str(exc)[:160])

# **不要**在这里再 adopt 一次：装配内部已把"本次交付的那一版"记为当前版；
# 用装配前的 nv 覆盖会把版本指针指回旧正文（实测导致清单 aligned=False、状态待重验）
_after = store.adopted()
print("selected after assembly:", str(getattr(_after, "version_id", ""))[:16],
      "| identity:", str(getattr(_after, "identity_id", ""))[:16])

out = Path(os.environ.get("WM_OUT") or (Path(os.environ["TEMP"]) / "wm_candidate_out"))
out.mkdir(parents=True, exist_ok=True)
(out / "delivered.md").write_text(asm.get("report") or "", encoding="utf-8")
(out / "research_state.json").write_text(
    json.dumps(read_research_state(TID, ws_dir=str(ws_mod.task_workspace(TID))),
               ensure_ascii=False, indent=1), encoding="utf-8")
print("wrote:", out / "delivered.md")
