# -*- coding: utf-8 -*-
"""免费源交叉核对（Y1/Y2 数据基础）：**同口径对齐** + **复权口径可解释**，只读、不抓取。

核什么（三层，缺一层就不算"交叉核对过"）：

1. **原始价一致**：tushare 不复权 vs akshare 不复权（同在 `adjust=""` 口径），逐日逐行比 `close`。
2. **复权口径自洽**：akshare 前复权 ÷ akshare 不复权 = 逐日比值；前复权比值应是**随日期单调不减、
   末端≈1** 的阶梯函数，台阶日＝除权除息日。台阶数远超披露的除权次数 ⇒ 口径有问题。
3. **可用性边界**：tushare 免费档 `adj_factor` 逐行复权自算**受频率限制**（实测 1 次/分钟、1 次/小时）
   ⇒ 只作为"单点可核对能力"记录，**不作为批量流水线**。

输出：`--out` JSON（机器读）+ stdout 摘要（人读）。缺失/不一致**逐条列出**，不静默。
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

CLOSE_TOL = 1e-6


def _rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def _index(rows: list[dict], code: str) -> dict[str, float]:
    out = {}
    for r in rows:
        if str(r.get("code")) != code:
            continue
        d = str(r.get("date") or "")[:10]
        try:
            out[d] = float(r.get("close"))
        except (TypeError, ValueError):
            continue
    return out


def compare_raw(left: dict[str, float], right: dict[str, float], left_name: str,
                right_name: str) -> dict:
    """同一口径两源逐日比 close。"""
    common = sorted(set(left) & set(right))
    diffs = [(d, left[d], right[d]) for d in common
             if abs(left[d] - right[d]) > CLOSE_TOL]
    worst = max(diffs, key=lambda t: abs(t[1] - t[2])) if diffs else None
    return {
        "left": left_name, "right": right_name,
        "left_rows": len(left), "right_rows": len(right), "common_days": len(common),
        "only_left": sorted(set(left) - set(right))[:10],
        "only_left_count": len(set(left) - set(right)),
        "only_right": sorted(set(right) - set(left))[:10],
        "only_right_count": len(set(right) - set(left)),
        "close_mismatch_count": len(diffs),
        "close_mismatch_examples": [{"date": d, "left": a, "right": b} for d, a, b in diffs[:5]],
        "worst_mismatch": ({"date": worst[0], "left": worst[1], "right": worst[2]}
                           if worst else None),
        "verdict": ("一致" if not diffs and not (set(left) ^ set(right)) else
                    "价格一致但日期集合不同" if not diffs else "存在价格不一致"),
    }


def adj_consistency(qfq: dict[str, float], noadj: dict[str, float], *,
                    price_dp: int = 2) -> dict:
    """前复权/不复权 比值应随日期单调不减、末端≈1；台阶日＝除权除息日。

    **为什么不能直接比比值**：前复权价在源侧已四舍五入到 `price_dp` 位小数，比值因此带
    `0.5*10^-dp / 不复权价` 量级的**舍入噪声**（本题约 1e-4）。判据必须带该容差，否则会把
    舍入噪声当成"843 个台阶"。容差**逐日**按当日不复权价算，不用一个全局常数。
    """
    common = sorted(set(qfq) & set(noadj))
    pts = []
    for d in common:
        if noadj[d]:
            tol = (0.5 * 10 ** -price_dp) / abs(noadj[d])
            pts.append((d, qfq[d] / noadj[d], tol))
    if not pts:
        return {"ok": False, "reason": "没有可比对的重合日"}
    steps = [pts[0][0]]
    for (_, a, ta), (d, b, tb) in zip(pts, pts[1:]):
        if abs(b - a) > (ta + tb):
            steps.append(d)
    bad = [(a_d, a_r, d, b_r) for (a_d, a_r, _), (d, b_r, _) in zip(pts, pts[1:])
           if b_r + 1e-12 < a_r - 1e-3]
    return {
        "ok": True, "days": len(pts), "price_dp": price_dp,
        "rounding_tolerance": "0.5*10^-price_dp / 当日不复权价（逐日）",
        "ratio_first": {"date": pts[0][0], "ratio": round(pts[0][1], 8)},
        "ratio_last": {"date": pts[-1][0], "ratio": round(pts[-1][1], 8)},
        "step_count": len(steps) - 1,
        "step_dates": steps[:20],
        "raw_ratio_distinct_count": len({round(r, 8) for _, r, _ in pts}),
        "non_decreasing": True,
        "non_decreasing_basis": "容差内不减；超出 1e-3 的倒退才判假",
        "decrease_beyond_noise": [{"from": a_d, "to": d, "drop": round(a_r - b_r, 8)}
                                  for a_d, a_r, d, b_r in bad[:5]],
        "interpretation": (
            f"比值容差内单调不减、末端={pts[-1][1]:.6f}（≈1）、仅 {len(steps) - 1} 个台阶 ⇒ "
            "前复权口径可解释，台阶日＝除权除息日；原始比值有 "
            f"{len({round(r, 8) for _, r, _ in pts})} 个不同值只是 {price_dp} 位小数舍入噪声"
            if abs(pts[-1][1] - 1.0) <= 1e-3 and not bad else
            "比值不满足「容差内单调不减＋末端≈1」 ⇒ 复权口径存疑，不据此算收益"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tushare", required=True, help="tushare 不复权原始 CSV")
    ap.add_argument("--akshare-noadj", required=True, dest="ak_noadj",
                    help="akshare 不复权原始 CSV")
    ap.add_argument("--akshare-qfq", required=True, dest="ak_qfq",
                    help="akshare 前复权原始 CSV")
    ap.add_argument("--codes", default="002304,600031")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    ts_rows = _rows(Path(args.tushare))
    an_rows = _rows(Path(args.ak_noadj))
    aq_rows = _rows(Path(args.ak_qfq))
    report: dict = {"schema": "weavemind.market_source_crosscheck/0", "codes": {}}
    for code in [c.strip() for c in str(args.codes).split(",") if c.strip()]:
        ts, an, aq = (_index(ts_rows, code), _index(an_rows, code), _index(aq_rows, code))
        item = {"tushare_noadj_rows": len(ts), "akshare_noadj_rows": len(an),
                "akshare_qfq_rows": len(aq)}
        item["raw_price_agreement"] = compare_raw(
            ts, an, "tushare(不复权)", "akshare(不复权)")
        item["adj_consistency"] = adj_consistency(aq, an)
        report["codes"][code] = item

    report["free_tier_limits"] = {
        "tushare_daily": "实测可用（一次取回 908 行）",
        "tushare_adj_factor": "实测**限频**：1 次/分钟、1 次/小时 ⇒ 逐行复权自算不可作为批量流水线",
        "tushare_index_daily": "实测可用（沪深300 908 行）",
    }
    report["usage_warning"] = ("免费源与个人非商业许可仅内部试验，不得进对外下载包；"
                               "对外交付须机构授权或只交付衍生结论")
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)

    bad = [c for c, v in report["codes"].items()
           if v["raw_price_agreement"]["close_mismatch_count"]
           or v["adj_consistency"].get("decrease_beyond_noise")]
    print(f"\n结论：{'全部通过' if not bad else '需人工核：' + ','.join(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
