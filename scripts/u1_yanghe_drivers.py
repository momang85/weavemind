# -*- coding: utf-8 -*-
"""U1 案例试算：洋河股份 2023→2024 经营驱动利润桥（真实披露数，含闭合校验）。

字段来源（缓存年报 `evals/a2_official_chain_20260929/002304/.../doc.json`）：

| 字段 | 披露位置 |
|---|---|
| 营业收入/营业成本/税金及附加/销售/管理/研发/财务费用/其他收益/投资收益/公允价值变动/信用与资产减值/资产处置/营业外收支/所得税/净利润/归母净利/少数股东损益 | 第十节 财务报告「3、合并利润表」（第76-77页） |
| 分产品、分地区 收入与成本、毛利率 | 第15页「（2）占公司营业收入或营业利润10%以上的…」 |
| 分产品 2023 收入绝对值 | 第14页「（1）主营业务分产品情况」 |
| 白酒销量（吨） | 第15页「（3）实物销售收入」 |
| 销售费用构成（广告促销费等） | 第16页「3、费用」 |

本脚本只做**算术与闭合校验**：公式与算子 `operating_drivers` 一致，
先用来核对真实数字，再把同一套公式搬进算子。可重复运行，输出证据 JSON。
"""
from __future__ import annotations

import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVIDENCE = os.path.join(ROOT, "docs", "evidence", "u1_yanghe_drivers.json")

# ---- 合并利润表（元）：2024 / 2023 ----
IS = {
    "revenue": (28_876_296_993.56, 33_126_277_551.51),
    "operating_cost": (7_751_218_356.66, 8_200_245_255.42),
    "taxes_and_surcharges": (4_826_086_952.64, 5_269_245_592.35),
    "selling_expense": (5_516_238_544.79, 5_386_953_700.62),
    "admin_expense": (1_924_730_302.35, 1_764_423_149.06),
    "rd_expense": (104_796_407.26, 284_753_881.33),
    "finance_expense": (-610_889_994.14, -754_525_568.63),
    "other_income": (59_667_934.13, 56_179_399.53),
    "investment_income": (146_415_168.80, 255_520_777.61),
    "fair_value_change": (-396_164_080.43, -37_082_477.77),
    "credit_impairment": (667_208.93, 881_383.32),
    "asset_impairment": (-11_203_156.73, -2_828_018.24),
    "asset_disposal_income": (-2_729_328.84, -5_282_977.32),
    "non_operating_income": (52_446_752.81, 39_176_788.83),
    "non_operating_expense": (70_140_310.99, 63_913_298.25),
    "income_tax_expense": (2_476_620_791.72, 3_197_064_562.60),
    "net_profit": (6_666_455_819.96, 10_020_768_556.47),
    "net_profit_parent": (6_673_388_602.12, 10_015_930_040.27),
    "minority_interest": (-6_932_782.16, 4_838_516.20),
}

# ---- 分段（10% 以上口径：2024 绝对值 + 同比；2023 成本按同比推算，标记 derived） ----
SEGMENTS = {
    "分产品:白酒": {"r1": 28_175_707_878.18, "c1": 7_281_082_736.44,
                 "r0": 32_389_581_931.71, "c_yoy": -0.0543,
                 "cut": "分产品", "note": "2023 成本按披露同比 −5.43% 推算"},
    "分产品:红酒": {"r1": 72_587_951.44, "c1": None, "r0": 99_854_764.34,
                 "c_yoy": None, "cut": "分产品", "note": "成本未在10%以上表披露"},
    "分地区:省内": {"r1": 12_748_484_435.48, "c1": 3_254_113_271.23,
                 "r0": 12_748_484_435.48 / (1 - 0.1143), "c_yoy": -0.0991,
                 "cut": "分地区", "note": "2023 收入/成本按披露同比推算"},
    "分地区:省外": {"r1": 15_499_811_394.14, "c1": 4_074_079_172.95,
                 "r0": 15_499_811_394.14 / (1 - 0.1435), "c_yoy": -0.0182,
                 "cut": "分地区", "note": "2023 收入/成本按披露同比推算"},
}
# 酒类合计（第14页 分行业）：用于校验两条切法各自覆盖的范围
LIQUOR = {"r1": 28_248_295_829.62, "c1": 7_328_192_444.18,
          "r0": 32_489_436_696.05, "c0": 7_761_633_378.60}
VOLUME = {"白酒": (139_076.05, 166_154.73)}          # 吨，2024 / 2023

# ΔB 的逐项分解：sign=+1 表示"该项目增加会增利"，如收入类；-1 表示费用/税金类
LINES = [
    ("taxes_and_surcharges", "税金及附加", -1),
    ("selling_expense", "销售费用", -1),
    ("admin_expense", "管理费用", -1),
    ("rd_expense", "研发费用", -1),
    ("finance_expense", "财务费用", -1),
    ("other_income", "其他收益", +1),
    ("investment_income", "投资收益", +1),
    ("fair_value_change", "公允价值变动收益", +1),
    ("credit_impairment", "信用减值损失", +1),
    ("asset_impairment", "资产减值损失", +1),
    ("asset_disposal_income", "资产处置收益", +1),
    ("non_operating_income", "营业外收入", +1),
    ("non_operating_expense", "营业外支出", -1),
    ("income_tax_expense", "所得税费用", -1),
    ("minority_interest", "少数股东损益", -1),
]

YI = 100_000_000.0


def yi(x: float) -> float:
    return round(x / YI, 4)


def gross_profit_bridge() -> dict:
    r1, r0 = IS["revenue"]
    c1, c0 = IS["operating_cost"]
    gp1, gp0 = r1 - c1, r0 - c0
    m1, m0 = gp1 / r1, gp0 / r0
    d_r, d_m = r1 - r0, m1 - m0
    scale = d_r * (m1 + m0) / 2.0
    margin = d_m * (r1 + r0) / 2.0
    return {
        "revenue": {"prev_yi": yi(r0), "cur_yi": yi(r1), "delta_yi": yi(d_r)},
        "operating_cost": {"prev_yi": yi(c0), "cur_yi": yi(c1), "delta_yi": yi(c1 - c0)},
        "gross_profit": {"prev_yi": yi(gp0), "cur_yi": yi(gp1), "delta_yi": yi(gp1 - gp0)},
        "gross_margin": {"prev": round(m0, 6), "cur": round(m1, 6),
                         "delta_pp": round(d_m * 100, 4)},
        "scale_effect_yi": yi(scale),
        "margin_effect_yi": yi(margin),
        "closure_diff_yuan": round(scale + margin - (gp1 - gp0), 6),
        "formula": "Δ毛利 = ΔR×(m1+m0)/2 + Δm×(R1+R0)/2（交互项均分）",
    }


def net_profit_bridge(gp: dict) -> dict:
    gp1 = gp["gross_profit"]["cur_yi"] * YI
    gp0 = gp["gross_profit"]["prev_yi"] * YI
    np1, np0 = IS["net_profit_parent"]
    b1, b0 = np1 - gp1, np0 - gp0
    items = []
    total = 0.0
    for key, label, sign in LINES:
        v1, v0 = IS[key]
        contrib = sign * (v1 - v0)
        total += contrib
        items.append({"metric": key, "label": label, "prev_yi": yi(v0),
                      "cur_yi": yi(v1), "contribution_yi": yi(contrib),
                      "sign": sign})
    items.sort(key=lambda x: -abs(x["contribution_yi"]))
    return {
        "net_profit_parent": {"prev_yi": yi(np0), "cur_yi": yi(np1),
                              "delta_yi": yi(np1 - np0)},
        "below_gross_line": {"prev_yi": yi(b0), "cur_yi": yi(b1),
                             "delta_yi": yi(b1 - b0)},
        "closure_diff_yuan": round((gp1 - gp0) + (b1 - b0) - (np1 - np0), 6),
        "line_items": items,
        "explained_yi": yi(total),
        "unexplained_residual_yi": yi((b1 - b0) - total),
        "formula": "Δ归母净利 = Δ毛利 + ΔB；B = 归母净利 − 毛利；ΔB = Σ sign×ΔX（未列项目留残差）",
    }


def segment_bridge() -> dict:
    out = {"formula": "同整体：ΔGP = ΔR×(m1+m0)/2 + Δm×(R1+R0)/2（分段各自算，不可跨切法相加）",
           "cuts": {}}
    for name, s in SEGMENTS.items():
        if s["c1"] is None or s["c_yoy"] is None:
            out["cuts"][name] = {"note": s["note"], "skipped": True}
            continue
        c0 = s["c1"] / (1.0 + s["c_yoy"])
        gp1, gp0 = s["r1"] - s["c1"], s["r0"] - c0
        m1, m0 = gp1 / s["r1"], gp0 / s["r0"]
        d_r, d_m = s["r1"] - s["r0"], m1 - m0
        out["cuts"][name] = {
            "cut": s["cut"], "note": s["note"],
            "revenue_prev_yi": yi(s["r0"]), "revenue_cur_yi": yi(s["r1"]),
            "gross_margin_prev": round(m0, 6), "gross_margin_cur": round(m1, 6),
            "gross_margin_delta_pp": round(d_m * 100, 4),
            "gross_profit_delta_yi": yi(gp1 - gp0),
            "scale_effect_yi": yi(d_r * (m1 + m0) / 2.0),
            "margin_effect_yi": yi(d_m * (s["r1"] + s["r0"]) / 2.0),
        }
    liq_gp1 = LIQUOR["r1"] - LIQUOR["c1"]
    liq_gp0 = LIQUOR["r0"] - LIQUOR["c0"]
    out["liquor_total"] = {"gross_profit_delta_yi": yi(liq_gp1 - liq_gp0),
                           "coverage": "两条切法各自覆盖酒类合计，互斥不可相加"}
    by_cut: dict[str, float] = {}
    for name, v in out["cuts"].items():
        if v.get("skipped"):
            continue
        by_cut.setdefault(v["cut"], 0.0)
        by_cut[v["cut"]] += v["gross_profit_delta_yi"]
    out["cut_totals_yi"] = {k: round(v, 4) for k, v in by_cut.items()}
    return out


def volume_price() -> dict:
    q1, q0 = VOLUME["白酒"]
    s = SEGMENTS["分产品:白酒"]
    p1, p0 = s["r1"] / q1, s["r0"] / q0
    d_q, d_p = q1 - q0, p1 - p0
    return {
        "product": "白酒",
        "volume_ton": {"prev": q0, "cur": q1, "delta_pct": round(q1 / q0 - 1, 4)},
        "implied_price_wan_per_ton": {"prev": round(p0 / 1e4, 4),
                                      "cur": round(p1 / 1e4, 4),
                                      "delta_pct": round(p1 / p0 - 1, 4)},
        "revenue_delta_yi": yi(s["r1"] - s["r0"]),
        "volume_effect_yi": yi(d_q * (p1 + p0) / 2.0),
        "price_effect_yi": yi(d_p * (q1 + q0) / 2.0),
        "caveat": "均价由『该产品收入/销量』算出，含产品结构与口径混合；"
                  "不得直接命名“提价效果”",
    }


def alternative_explanations() -> dict:
    """替代解释与反证：不改变金额分解，但会改变读者对“原因”的判断。"""
    np1, np0 = IS["net_profit_parent"]
    pbt1 = 9_143_076_611.68
    pbt0 = 13_217_833_119.07
    rate1, rate0 = IS["income_tax_expense"][0] / pbt1, IS["income_tax_expense"][1] / pbt0
    tax_at_prev_rate = pbt1 * rate0
    return {
        "effective_tax_rate": {"prev": round(rate0, 4), "cur": round(rate1, 4),
                               "delta_pp": round((rate1 - rate0) * 100, 4)},
        "tax_counterfactual": {
            "actual_yi": yi(IS["income_tax_expense"][0]),
            "at_prev_rate_yi": yi(tax_at_prev_rate),
            "rate_effect_yi": yi(IS["income_tax_expense"][0] - tax_at_prev_rate),
            "reading": "所得税减少是利润下滑的被动结果；实际税率反而上升，"
                       "税率因素多吃掉约 2.65 亿元",
        },
        "non_operating_swing": {
            "fair_value_change_delta_yi": yi(IS["fair_value_change"][0] - IS["fair_value_change"][1]),
            "investment_income_delta_yi": yi(IS["investment_income"][0] - IS["investment_income"][1]),
            "reading": "公允价值变动与投资收益属非经营/非经常因素，不得并入经营判断",
        },
        "structure_vs_price": "隐含均价上升可能来自产品结构（高价酒占比提高）而非提价；"
                              "需分产品收入或量价披露才能区分",
        "carry_over": f"归母净利同比 {round(np1 / np0 - 1, 4):.2%}；"
                      f"毛利同比 {round((IS['revenue'][0]-IS['operating_cost'][0]) / (IS['revenue'][1]-IS['operating_cost'][1]) - 1, 4):.2%}",
    }


def main() -> int:
    gp = gross_profit_bridge()
    npb = net_profit_bridge(gp)
    seg = segment_bridge()
    vp = volume_price()
    alt = alternative_explanations()
    report = {
        "case": "洋河股份 002304 2023→2024 经营驱动利润桥",
        "source": {
            "material": "evals/a2_official_chain_20260929/002304/project/materials/"
                        "f42e747c73850b59/doc.json",
            "title": "江苏洋河酒厂股份有限公司 2024 年年度报告",
            "periods": [2023, 2024],
            "tables": ["合并利润表(76-77页)", "主营业务分产品/分地区(15页)",
                       "实物销售(15页)", "费用(16页)", "营业成本构成(15-16页)"],
        },
        "gross_profit_bridge": gp,
        "net_profit_bridge": npb,
        "segment_bridge": seg,
        "volume_price": vp,
        "alternative_explanations": alt,
        "headline": (
            f"归母净利 {npb['net_profit_parent']['delta_yi']:+.2f} 亿元 = "
            f"毛利 {gp['gross_profit']['delta_yi']:+.2f}（收入规模 "
            f"{gp['scale_effect_yi']:+.2f} / 毛利率 {gp['margin_effect_yi']:+.2f}）"
            f" + 毛利线以下净额 {npb['below_gross_line']['delta_yi']:+.2f} 亿元"
        ),
    }
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    with open(EVIDENCE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(report["headline"])
    print(f"毛利闭合差 = {gp['closure_diff_yuan']} 元；"
          f"净利闭合差 = {npb['closure_diff_yuan']} 元；"
          f"未解释残差 = {npb['unexplained_residual_yi']} 亿元")
    print("三项最大减利项：", [(i["label"], i["contribution_yi"])
                          for i in npb["line_items"][:3]])
    print("白酒量价：", vp["volume_ton"]["delta_pct"], vp["implied_price_wan_per_ton"])
    print(f"证据：{os.path.relpath(EVIDENCE, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
