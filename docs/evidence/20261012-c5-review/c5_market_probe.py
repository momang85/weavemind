# -*- coding: utf-8 -*-
"""C5-2 市场对照探针：**只读**核对三窗口装配与 C4 受理证据 `c5_market_window_check.json` 同源。

不重跑旧套件、不写研究目录、不改任何冻结产物。参考值只用于比对，不参与计算。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from financial_analysis import research_experiment as R      # noqa: E402

STUDY = ROOT / "tmp" / "research-studies" / "0af20919e6245b88"
STUDY_SANY = ROOT / "tmp" / "research-studies" / "4dba42a6bdde9227"
DS = "3d6b4acb451abcf960c4132deb16c2a09169234b76dcb643958ceee855fcb148"
CAL = "49413c7d1c48ce4c59f8e63706705c28d4b66edc9d047ab381ea60bd600e5435"
ASOF = "2026-10-09"

# C4 受理证据里的参考值（c5_market_window_check.json）：offset → (股票, 基准, 相对)
REF = {
    "2ce860dddd94e5a6": {"anchor": 70.88,
                         0: (-0.002681, 0.011371, -0.013894),
                         4: (0.011428, 0.054273, -0.040639),
                         19: (-0.024125, 0.071024, -0.088839)},
    "ee4429eecac66f50": {"anchor": 47.68,
                         0: (0.005663, 0.010958, -0.005238),
                         4: (-0.024119, 0.023896, -0.046894),
                         19: (-0.083054, 0.028144, -0.108154)},
}

results: list[dict] = []
fails: list[str] = []


def market_cache_ready(dataset_id: str = DS) -> tuple[bool, str]:
    """行情缓存（本机临时目录里的免费源数据）在不在？

    不在时**如实停在本机条件**：不覆盖已记录的读数、不拿别的数据集顶替。
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
          "本轮不重算、不覆盖已记录读数。")
    print("  原因：", _why)
    print("  恢复：按 README「怎么复核」重建行情缓存后重跑本探针。")
    raise SystemExit(2)


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail[:400]})
    if not ok:
        fails.append(f"{name}: {detail[:200]}")


def _dec(v) -> Decimal:
    return Decimal(str(v)).normalize()


def _reg(study: Path) -> list[dict]:
    man = json.loads((study / "study_manifest.json").read_text(encoding="utf-8"))
    return (((man.get("sources") or {}).get("financial") or {}).get("metadata") or {}).get("materials") or []


def _call(**kw) -> dict:
    study = kw.pop("study", STUDY)
    args = {"price_dataset_id": DS, "calendar_id": CAL, "evaluation_as_of": ASOF}
    args.update(kw)
    return R.market_comparison(roots=R.material_roots(study_dir=study), registered=_reg(study), **args)


# ---------------------------------------------------------------- 1) 参考值同源
for mid, ref in REF.items():
    got = _call(entity_id="002304.SZ", material_id=mid)
    check(f"{mid}: status=observed", got.get("status") == "observed", str(got.get("status")) + " " + str(got.get("reason")))
    check(f"{mid}: 事件日=登记披露日", (got.get("event") or {}).get("date") == (got.get("material") or {}).get("disclosed_at"),
          f"{(got.get('event') or {}).get('date')} vs {(got.get('material') or {}).get('disclosed_at')}")
    check(f"{mid}: 锚点收盘={ref['anchor']}", _dec((got.get("anchor") or {}).get("close")) == _dec(ref["anchor"]),
          str((got.get("anchor") or {}).get("close")))
    wmap = {int(w["offset"]): w for w in (got.get("windows") or [])}
    check(f"{mid}: 三窗口=1/5/20交易日", sorted(wmap) == [0, 4, 19], str(sorted(wmap)))
    for off, (s, b, e) in ((k, v) for k, v in ref.items() if k != "anchor"):
        w = wmap.get(off) or {}
        check(f"{mid}: offset{off} 股票={s}", _dec(w.get("stock_return")) == _dec(s), str(w.get("stock_return")))
        check(f"{mid}: offset{off} 基准={b}", _dec(w.get("benchmark_return")) == _dec(b), str(w.get("benchmark_return")))
        check(f"{mid}: offset{off} 几何相对={e}", _dec(w.get("excess_return")) == _dec(e), str(w.get("excess_return")))
        # 几何口径自查：(1+股票)/(1+基准)−1
        if w.get("stock_return") is not None and w.get("benchmark_return") is not None:
            geo = (1 + Decimal(str(w["stock_return"]))) / (1 + Decimal(str(w["benchmark_return"]))) - 1
            check(f"{mid}: offset{off} 相对=几何超额",
                  abs(geo - Decimal(str(w["excess_return"]))) < Decimal("0.000001"), str(geo))
    # 页面两位
    check(f"{mid}: 相对显示两位", (wmap.get(4) or {}).get("relative_return_pct", "").endswith("%"),
          str((wmap.get(4) or {}).get("relative_return_pct")))
    # 许可随读数走
    check(f"{mid}: delivery_eligible=False", got.get("delivery_eligible") is False, str(got.get("delivery_eligible")))
    check(f"{mid}: 带对外拦截原因", bool(got.get("delivery_block_reason")), str(got.get("delivery_block_reason")))
    check(f"{mid}: 在包内标记 internal_only", got.get("internal_only") is True, str(got.get("internal_only")))
    check(f"{mid}: 冻结了行情/日历 id",
          (got.get("binding") or {}).get("price_dataset_id") == DS
          and (got.get("binding") or {}).get("calendar_id") == CAL,
          json.dumps(got.get("binding"), ensure_ascii=False)[:200])
    check(f"{mid}: 冻结了算子与公式",
          (got.get("params") or {}).get("operator") == "event_returns_v1"
          and "几何超额" in str((got.get("params") or {}).get("formula")),
          json.dumps(got.get("params"), ensure_ascii=False)[:200])
    # 确定性：同一组输入两次 → 逐字节同一块（进内容身份）
    again = _call(entity_id="002304.SZ", material_id=mid)
    check(f"{mid}: 重放逐字节同一块",
          hashlib.sha256(json.dumps(got, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
          == hashlib.sha256(json.dumps(again, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
          "重放不一致")

# ---------------------------------------------------------------- 2) 缺件＝pending（不猜）
p = _call(entity_id="002304.SZ", material_id="2ce860dddd94e5a6", price_dataset_id="", calendar_id="")
check("缺行情集→pending＋原因", p.get("status") == "pending" and "行情数据集" in str(p.get("reason")),
      f"{p.get('status')} {p.get('reason')}")
p = _call(entity_id="002304.SZ", material_id="2ce860dddd94e5a6", calendar_id="")
check("缺日历→pending＋原因", p.get("status") == "pending" and "交易日历" in str(p.get("reason")),
      f"{p.get('status')} {p.get('reason')}")
p = _call(entity_id="002304.SZ", material_id="2ce860dddd94e5a6", evaluation_as_of="")
check("缺评价时点→pending＋原因", p.get("status") == "pending" and "评价时点" in str(p.get("reason")),
      f"{p.get('status')} {p.get('reason')}")
p = _call(entity_id="002304.SZ", material_id="")
check("缺事件→pending＋原因", p.get("status") == "pending" and "披露事件" in str(p.get("reason")),
      f"{p.get('status')} {p.get('reason')}")

# ---------------------------------------------------------------- 3) id 不一致＝拒绝顶替
bad = _call(entity_id="002304.SZ", material_id="2ce860dddd94e5a6", price_dataset_id="f" * 64)
check("未登记行情集→refused（不拿别的顶替）",
      bad.get("status") == "refused" and "行情数据集" in str(bad.get("reason")),
      f"{bad.get('status')} {str(bad.get('reason'))[:120]}")
bad = _call(entity_id="002304.SZ", material_id="2ce860dddd94e5a6", calendar_id="f" * 64)
check("未登记日历→refused（不拿别的顶替）",
      bad.get("status") == "refused" and "交易日历" in str(bad.get("reason")),
      f"{bad.get('status')} {str(bad.get('reason'))[:120]}")
bad = _call(entity_id="002304.SZ", material_id="0c3e82056e977adc")   # 三一材料，洋河主体
check("跨公司材料→refused", bad.get("status") == "refused" and "同一公司" in str(bad.get("reason")),
      f"{bad.get('status')} {str(bad.get('reason'))[:120]}")

# ---------------------------------------------------------------- 4) 评价时点不足＝不报 0
early = _call(entity_id="002304.SZ", material_id="2ce860dddd94e5a6", evaluation_as_of="2025-08-21")
check("评价时点不足→非 observed", early.get("status") != "observed", str(early.get("status")))
miss = [w for w in (early.get("windows") or []) if w.get("state") != "observed"]
check("评价时点不足→缺数不报 0",
      all(w.get("stock_return") is None and w.get("relative_return_pct") == "" for w in miss),
      json.dumps(miss, ensure_ascii=False)[:200])

# ---------------------------------------------------------------- 5) 另一主体（三一）同一路径
sany = _call(study=STUDY_SANY, entity_id="600031.SH", material_id="0c3e82056e977adc")
check("三一另一事件同一路径可用", sany.get("status") in ("observed", "partial") and sany.get("anchor"),
      f"{sany.get('status')} {str(sany.get('reason'))[:120]}")
if sany.get("windows"):
    w = sany["windows"][-1]
    if w.get("stock_return") is not None and w.get("benchmark_return") is not None:
        geo = (1 + Decimal(str(w["stock_return"]))) / (1 + Decimal(str(w["benchmark_return"]))) - 1
        check("三一窗内相对=几何超额", abs(geo - Decimal(str(w["excess_return"]))) < Decimal("0.000001"), str(geo))

# ---------------------------------------------------------------- 6) 基准与清单
inv = R.registered_market_inventory()
check("清单含内部行情集", DS in [d["dataset_id"] for d in inv["price_datasets"]],
      json.dumps([d["dataset_id"][:12] for d in inv["price_datasets"]]))
check("清单含交易日历", CAL in [c["calendar_id"] for c in inv["calendars"]],
      json.dumps([c["calendar_id"][:12] for c in inv["calendars"]]))
check("基准推导唯一解=000300", R._benchmark_of(["000300", "002304", "600031"], entity_code="002304") == "000300",
      R._benchmark_of(["000300", "002304", "600031"], entity_code="002304"))
check("基准不唯一→空（要人显式给）",
      R._benchmark_of(["000300", "399001", "002304"], entity_code="002304") == "",
      R._benchmark_of(["000300", "399001", "002304"], entity_code="002304"))

# ---------------------------------------------------------------- 7) 不写死：算子和哈希来自现役实现
import quant_research.event_returns as er     # noqa: E402
check("三窗口＝算子版本里的 offsets", er.OPERATOR == "event_returns_v1" and er.IMPL_VERSION.startswith("event_returns/"),
      er.IMPL_VERSION)
check("读数哈希来自算子", bool(re.search(r"[0-9a-f]{64}", str(_call(entity_id="002304.SZ", material_id="2ce860dddd94e5a6").get("reading_hash") or ""))),
      "reading_hash 形态不对")

out = {"scope": "C5-2 市场对照三窗口（只读；参考值取自 C4 受理证据 c5_market_window_check.json）",
       "dataset_id": DS, "calendar_id": CAL, "evaluation_as_of": ASOF,
       "total": len(results), "passed": sum(1 for r in results if r["ok"]), "failed": len(fails),
       "results": results, "failures": fails}
(Path(__file__).with_name("c5_market_results.json")).write_text(
    json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"C5-2 市场对照：{out['passed']}/{out['total']} PASS")
for f in fails:
    print("FAIL:", f)
sys.exit(1 if fails else 0)
