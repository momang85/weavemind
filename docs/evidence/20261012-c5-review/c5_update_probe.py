# -*- coding: utf-8 -*-
"""C5-3／C5-2 端到端只读探针：**沿冻结原判更新**＋**三市场窗并列**＋保存重开。

覆盖（全部离线、确定性、不重跑旧套件／不付费、不碰研究目录以外的任何东西）：
  A 冻结基线更新：`baseline_experiment_id` → 较早一侧读数/判据**照抄**冻结实验（不现场重建）
  B 原判据同版：新实验每张卡的 `criterion_sha256` 与基线逐项相同
  C 市场对照三窗口：洋河年报事件的股票/基准/几何相对＝C4 受理证据参考值（生产不写死）
  D 并列经营判断＋下一观察（用户闭环要的一页）
  E 确定性：同输入两次装配 → 同一个 experiment_id／content_sha256
  F 保存重开：`experiments/<id>.json` 读回同一份，身份自校验 True，事件/行情/条件仍在
  G 历史重建明示：不给 baseline 时 `baseline_kind=historical_rebuild`（不冒充原假说）
  H 许可门：免费源读数 `delivery_eligible=False`＋`internal_only`＋阻断原因

用法：`python -X utf8 docs/evidence/20261012-c5-review/c5_update_probe.py`
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import hashlib

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from financial_analysis import research_experiment as R      # noqa: E402

OUT = Path(__file__).resolve().parent
STUDY = ROOT / "tmp" / "research-studies" / "0af20919e6245b88"   # 洋河（C4 已受理基线）
BASELINE = "245bcd5e56ef7c5eafaf11e2"                            # C4 冻结实验（不重写）
BASE_JSON = STUDY / "experiments" / f"{BASELINE}.json"
ENTITY = "002304.SZ"
RUN = "370dba92a40e36a3acdfee14644085602e088e625db536b8111f2da9ac2329b4"
NEW_MATERIAL = "ee4429eecac66f50"                                # 2025 年报（2026-04-28）
OLD_MATERIAL = "f42e747c73850b59"                                # 2024 年报（2025-04-29）
DS = "3d6b4acb451abcf960c4132deb16c2a09169234b76dcb643958ceee855fcb148"
CAL = "49413c7d1c48ce4c59f8e63706705c28d4b66edc9d047ab381ea60bd600e5435"
ASOF = "2026-10-09"
# C4 期间的候选工作区（续页 cash run 所在；进程内 task_workspace 同一位置）
WORKSPACE = Path(os.environ["TEMP"]) \
    / "agent_workspace" / "tasks" / "projects" / "default" \
    / "study-report-0af20919e6245b88-002304.SZ"
# C4 受理证据参考值（c5_market_window_check.json）：offset → (股票, 基准, 相对)
REF_ANNUAL = {"anchor": 47.68,
              0: (0.005663, 0.010958, -0.005238),
              4: (-0.024119, 0.023896, -0.046894),
              19: (-0.083054, 0.028144, -0.108154)}

results: list[dict] = []
fails: list[str] = []


def market_cache_ready(dataset_id: str = DS) -> tuple[bool, str]:
    """行情缓存（本机临时目录里的免费源数据）在不在？

    不在时**如实停在本机条件**：不重算、不覆盖已记录读数、不拿别的数据集顶替。
    """
    try:
        import adapters.market_history as MH
        pay = MH.load(dataset_id)
        rows = (pay.get("rows") if isinstance(pay, dict) else None) or []
        # load() 是 fail-soft 的：数据目录缺失时返回 {"unavailable":…, "rows":[]}，不是异常
        return (bool(rows) and not (pay.get("unavailable") if isinstance(pay, dict) else False),
                "" if rows else str((pay or {}).get("reason") or "load() 无行"))
    except Exception as exc:                          # noqa: BLE001
        return False, str(exc)[:200]


_ready, _why = market_cache_ready()
if not _ready:
    print("环境缺件：本机已登记行情集的数据目录不在（缓存被外部清理？）——"
          "本轮不重算、不落盘、不覆盖已记录读数。")
    print("  原因：", _why)
    print("  恢复：按 README「怎么复核」重建行情缓存后重跑本探针。")
    raise SystemExit(2)


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail[:400]})
    if not ok:
        fails.append(f"{name}: {detail[:200]}")


def _d(v) -> Decimal:
    return Decimal(str(v)).normalize()


def _prepare(**kw) -> dict:
    args = {
        "study_dir": STUDY, "entity_id": ENTITY, "run_id": RUN,
        "view": "current", "metric": "operating_payable_increase",
        "fractions": ("0", "0.5", "1"),
        "old_material_id": OLD_MATERIAL, "new_material_id": NEW_MATERIAL,
        "workspace_dir": WORKSPACE,
    }
    args.update(kw)
    return R.prepare_company_research_experiment(**args)


def _market() -> dict:
    return {"material_id": NEW_MATERIAL, "price_dataset_id": DS, "calendar_id": CAL,
            "evaluation_as_of": ASOF}


base_doc = json.loads(BASE_JSON.read_text(encoding="utf-8"))
BASE_BYTES_SHA256 = hashlib.sha256(BASE_JSON.read_bytes()).hexdigest()
base_ident = base_doc.get("identity") or {}
base_page = (base_doc.get("disclosure_update") or {}).get("page") or {}

# ---------------------------------------------------------------- A 冻结基线更新
art = _prepare(baseline_experiment_id=BASELINE, market=_market())
bo = art.get("based_on") or {}
check("A1 baseline_kind=frozen_experiment", bo.get("baseline_kind") == "frozen_experiment",
      f"got {bo.get('baseline_kind')!r}")
bx = bo.get("baseline_experiment") or {}
check("A2 记下原版内容身份（与冻结 JSON 同值）",
      str(bx.get("content_sha256") or "") == str(base_ident.get("content_sha256") or ""),
      f"{str(bx.get('content_sha256'))[:16]} vs {str(base_ident.get('content_sha256'))[:16]}")
check("A3 记下原版判据哈希", str(bx.get("criteria_sha256") or "")
      == str(base_ident.get("criteria_sha256") or ""),
      f"{str(bx.get('criteria_sha256'))[:16]}")
page = (art.get("disclosure_update") or {}).get("page") or {}
ver = page.get("version") or {}
check("A4 更新页标沿冻结原判（historical_rebuild=False）",
      ver.get("historical_rebuild") is False
      and str(ver.get("baseline_experiment_id") or "") == BASELINE,
      f"historical_rebuild={ver.get('historical_rebuild')} bid={ver.get('baseline_experiment_id')}")
same_earlier = ((page.get("readings") or {}).get("earlier") or {}) == ((base_page.get("readings") or {}).get("earlier") or {})
check("A5 较早一侧读数**照抄冻结实验**（不现场重建）", same_earlier,
      "readings.earlier 逐键相同" if same_earlier else "与冻结页不同 ⇒ 又现场重建了")
check("A6 较早一侧来源标注 experiments 冻结读数",
      "冻结" in json.dumps(page.get("hypotheses") or [{}], ensure_ascii=False)[:2000]
      or any("冻结" in str(r.get("earlier_side_source") or "") for r in (page.get("hypotheses") or [])),
      "earlier_side_source")
check("A7 旧材料沿用基线登记（不另选）",
      str(bo.get("old_material_id") or "") == OLD_MATERIAL,
      f"old_material_id={bo.get('old_material_id')}")
check("A8 旧截止核过：新材料时点晚于旧截止",
      str(ver.get("new_cutoff") or "") == "2026-04-28"
      and str(ver.get("old_cutoff") or "") == "2025-04-30",
      f"{ver.get('old_cutoff')} → {ver.get('new_cutoff')}")

# ---------------------------------------------------------------- B 原判据同版
base_cards = {str(c.get("judgment_id") or ""): c for c in (base_doc.get("cards") or [])}
new_cards = {str(c.get("judgment_id") or ""): c for c in (art.get("cards") or [])}
same_crit, diffs = True, []
for jid, bc in base_cards.items():
    nc = new_cards.get(jid) or {}
    b_cs = str((bc.get("criterion") or {}).get("criterion_sha256") or bc.get("criterion_sha256") or "")
    n_cs = str((nc.get("criterion") or {}).get("criterion_sha256") or nc.get("criterion_sha256") or "")
    if b_cs != n_cs:
        same_crit = False
        diffs.append(f"{jid}: {b_cs[:12]}→{n_cs[:12]}")
check("B1 每张卡的判据哈希与冻结基线相同（原判据同版）", same_crit, "；".join(diffs) or "5/5 相同")
check("B2 卡片数不减（冻结卡原样带过来）", len(new_cards) >= len(base_cards),
      f"{len(base_cards)} → {len(new_cards)}")
check("B3 状态按新窗口重判（本期读数只改方向）",
      all(("status" in (new_cards.get(j) or {})) for j in base_cards),
      json.dumps({j: (new_cards.get(j) or {}).get("status") for j in base_cards}, ensure_ascii=False))
check("B4 判据里不再有未执行的「ΔOCF 由正转负」文案",
      "ΔOCF 由正转负" not in json.dumps(art, ensure_ascii=False),
      "C5-3 删除项")
inv = next((r for r in (page.get("hypotheses") or [])
            if r.get("judgment_id") == "finished_goods_inventory_build"), {})
inv_crit = inv.get("criteria") or {}
check("B5 库存：单窗口削弱与两窗口持续确认**分开**（不把一次下降写成消化）",
      "单窗口" in str(inv_crit.get("weaken_single") or "")
      and "两窗口确认未满足" in str(inv.get("satisfied") or "")
      and "两个" in str(inv_crit.get("weaken_confirmed") or ""),
      f"satisfied={inv.get('satisfied')}｜single={str(inv_crit.get('weaken_single'))[:60]}")
from financial_analysis import continuation as CONT                      # noqa: E402
_st, _rs = CONT._decide(CONT._jd.J_INVENTORY,
                        {"period_kind": "annual", "inventory_yoy": 0.16},
                        {"period_kind": "half", "inventory_yoy": -0.34},
                        tol=CONT.TOLERANCE_PP, same_kind=False)
check("B6 单窗口转负的措辞＝本窗口不再累积＋待持续确认（不再写『消化出现』）",
      _st == "削弱" and "不再累积" in _rs and "消化出现" not in _rs,
      _rs[:160])

# ---------------------------------------------------------------- C 三窗口
mc = art.get("market_comparison") or {}
check("C1 市场对照 status=observed", str(mc.get("status") or "") == "observed",
      f"status={mc.get('status')} reason={str(mc.get('reason') or '')[:120]}")
check("C2 事件＝所选登记材料（同主体披露日）",
      str((mc.get("event") or {}).get("date") or "") == "2026-04-28"
      and str((mc.get("material") or {}).get("material_id") or "") == NEW_MATERIAL,
      json.dumps(mc.get("event") or {}, ensure_ascii=False)[:160])
check("C3 锚点＝披露日（或前一交易日）收盘＝参考 47.68",
      _d((mc.get("anchor") or {}).get("close")) == _d(REF_ANNUAL["anchor"]),
      f"anchor={mc.get('anchor')}")
wins = {int(w.get("offset")): w for w in (mc.get("windows") or [])}
ok_w, why_w = True, []
for off, ref in REF_ANNUAL.items():
    if off == "anchor":
        continue
    s, b, x = ref
    w = wins.get(off) or {}
    got_raw = (str(_d(w.get("stock_return"))), str(_d(w.get("benchmark_return"))),
               str(_d(w.get("excess_return"))))
    exp_raw = tuple(str(_d(v)) for v in (s, b, x))
    if got_raw != exp_raw:
        ok_w = False
        why_w.append(f"{off}: {got_raw} ≠ {exp_raw}")
check("C4 三窗口逐值＝C4 受理参考（1/5/20 交易日＝offsets 0/4/19）", ok_w,
      "；".join(why_w) or "三窗口 × 股票/基准/相对 原值全部相同")
check("C4b 同值按 2 位百分数呈现（页面口径）",
      all(str(wins.get(o, {}).get("stock_return_pct")) == f"{'+' if v[0] >= 0 else ''}{_d(v[0]) * 100:.2f}%"
          for o, v in REF_ANNUAL.items() if o != "anchor"),
      json.dumps({str(o): wins.get(o, {}).get("stock_return_pct")
                  for o in (0, 4, 19)}, ensure_ascii=False))
check("C5 几何相对自查 (1+s)/(1+b)−1（算子在 6 位小数上呈现，自核 1e-6 内）",
      all(abs(_d(w.get("excess_return"))
              - ((1 + _d(w.get("stock_return"))) / (1 + _d(w.get("benchmark_return"))) - 1))
          < Decimal("1e-6")
          for w in (mc.get("windows") or []) if w.get("excess_return") is not None),
      "逐窗口（1e-6 内：差 ≤4.2e-7 全来自 6 位小数呈现）")
check("C6 窗口状态 observed（评价时点够）", str(mc.get("window_state") or "") == "observed",
      str(mc.get("window_state")))
check("C7 绑定记下所选数据集/日历 id 与算子版本",
      str((mc.get("binding") or {}).get("price_dataset_id") or "") == DS
      and str((mc.get("binding") or {}).get("calendar_id") or "") == CAL
      and str((mc.get("params") or {}).get("impl_version") or "").startswith("event_returns/"),
      json.dumps(mc.get("params") or {}, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- D 并列
ops = mc.get("alongside_operations") or []
check("D1 并列经营支持/削弱（与机制卡同源）",
      len(ops) >= len(base_cards)
      and all(str(o.get("status") or "") in ("加强", "削弱", "无法判断") for o in ops),
      json.dumps(ops, ensure_ascii=False)[:240])
check("D2 下一观察随行（用户闭环要的一页）",
      bool(str(mc.get("next_observation") or "")),
      str(mc.get("next_observation") or "")[:120])
md = R.experiment_summary_markdown(art)
check("D3 一页摘要含三窗口与并列判断",
      "市场对照" in md and "相对" in md and "并列经营判断" in md and "沿冻结原判" in md,
      md[:200].replace("\n", " / "))

# ---------------------------------------------------------------- E 确定性
art2 = _prepare(baseline_experiment_id=BASELINE, market=_market())
i1, i2 = R.experiment_identity(art), R.experiment_identity(art2)
check("E1 同输入两次装配 → 同一 experiment_id", i1.get("experiment_id") == i2.get("experiment_id"),
      f"{i1.get('experiment_id')}")
check("E2 内容哈希逐字节同一（无时钟时间）",
      str(i1.get("content_sha256")) == str(i2.get("content_sha256")),
      str(i1.get("content_sha256"))[:20])
check("E3 机器验收/研究状态/人工复核三态原样",
      (art.get("research_state") or {}).get("experiment_state") == "not_applicable"
      and (art.get("research_state") or {}).get("human_review_label") == "未复核",
      json.dumps(art.get("research_state") or {}, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- H 许可门
check("H1 免费源读数：delivery_eligible=False", mc.get("delivery_eligible") is False,
      f"delivery_eligible={mc.get('delivery_eligible')}")
check("H2 internal_only 与阻断原因随行",
      mc.get("internal_only") is True and bool(str(mc.get("delivery_block_reason") or "")),
      str(mc.get("delivery_block_reason") or "")[:160])
check("H3 实验对外交付位未被本段改成 True",
      (art.get("research_state") or {}).get("external_delivery_allowed") is False,
      str((art.get("research_state") or {}).get("external_delivery_allowed")))

# ---------------------------------------------------------------- G 历史重建明示
art_h = _prepare()          # 不给 baseline
boh = art_h.get("based_on") or {}
check("G1 无原版 → baseline_kind=historical_rebuild",
      boh.get("baseline_kind") == "historical_rebuild", str(boh.get("baseline_kind")))
check("G2 无原版 → 更新页仍明示历史重建（不称原假说）",
      ((art_h.get("disclosure_update") or {}).get("page") or {}).get("version", {})
      .get("historical_rebuild") is True
      and "历史重建" in R.experiment_summary_markdown(art_h),
      "page.version.historical_rebuild=True ＋ 摘要标注")
check("G3 两条路都不写回原研究（identity.note 仍声明另存）",
      "另存" in str((art.get("identity") or {}).get("note") or ""),
      str((art.get("identity") or {}).get("note") or "")[:80])

# ---------------------------------------------------------------- F 保存重开（真落盘）
saved = R.save_experiment(art, study_dir=STUDY)
eid = str(saved.get("experiment_id") or "")
reopened = R.load_experiment(study_dir=STUDY, experiment_id=eid)
check("F1 保存得到 24 位实验 id 且未覆盖基线",
      len(eid) == 24 and eid != BASELINE and (STUDY / "experiments" / f"{eid}.json").is_file(),
      f"{eid}")
check("F2 重开身份自校验 True",
      (reopened.get("identity") or {}).get("content_sha256_matches") is True,
      str((reopened.get("identity") or {}).get("content_sha256_matches")))
re_page = (reopened.get("disclosure_update") or {}).get("page") or {}
check("F3 重开保留事件/行情/条件",
      str(((reopened.get("market_comparison") or {}).get("event") or {}).get("date") or "") == "2026-04-28"
      and bool((reopened.get("market_comparison") or {}).get("windows"))
      and ((re_page.get("readings") or {}).get("earlier") or {}) == ((base_page.get("readings") or {}).get("earlier") or {}),
      "event/windows/readings.earlier 均在")
check("F4 保存幂等（同内容不重写；基线 JSON 字节哈希本轮前后一致）",
      hashlib.sha256(BASE_JSON.read_bytes()).hexdigest() == BASE_BYTES_SHA256,
      f"{BASE_BYTES_SHA256[:16]}…")

# 基线 MSE：另核冻结基线未被这次保存触碰
base_reopen = R.load_experiment(study_dir=STUDY, experiment_id=BASELINE)
check("F5 冻结基线本轮未被改写（读回身份自校验 True、字节哈希不变）",
      (base_reopen.get("identity") or {}).get("content_sha256_matches") is True
      and hashlib.sha256(BASE_JSON.read_bytes()).hexdigest() == BASE_BYTES_SHA256,
      str((base_reopen.get("identity") or {}).get("experiment_id")))

out = {"scope": "C5-3/C5-2 端到端只读探针：沿冻结原判更新＋三市场窗＋保存重开",
       "study": str(STUDY.name), "baseline_experiment_id": BASELINE,
       "new_experiment_id": eid, "new_content_sha256": str(i1.get("content_sha256") or ""),
       "checks": results, "total": len(results),
       "passed": sum(1 for r in results if r["ok"]), "fails": fails}
(OUT / "c5_update_results.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{out['passed']}/{out['total']} PASS  new_experiment_id={eid}")
for f in fails:
    print("FAIL:", f)
sys.exit(0 if not fails else 1)
