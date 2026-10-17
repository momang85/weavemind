# -*- coding: utf-8 -*-
"""B3 批次：两张 2024 经营机制卡 + 每家一张 3×3「利润目标＋天数冲击占用」网格 + 洋河 2025 独立后续页。

只用既有四份缓存（tmp/q1-real-inputs/，B1 已投影），不开 Wind、不重抓、不重投影。
全部走正常研究入口（quant_research.study.run_study/write_study/verify_study/replay_study）；
网格由 quant_research.joint_grid 逐格调用**既有**算子 scenario_sensitivity/cash_pressure 产出，
本脚本只做**研究输入（spec）组装**：复用 B1 冻结 spec 的 financial 描述符，覆盖 B3 字段。

与 B2 的差异（B3 指令）：
- 主因句更正：ΔE 与毛利贡献不再混写（ΔE≠毛利贡献；逐项以 2025-04-30 截止的冻结分解为准）；
- spec.financial 增加显式 joint_grid 块（g/m 网格 + 天数冲击 + 研究者目标注记；B 缺省未设）；
- 卡来源：spec 不带 sources 时继承所选 Observation 的原件出处（quant_research.study 衔接点A）。

用法：
    C:\\Python314\\python.exe scripts/q1_b3_study.py                # 组 spec + 打印概要
    C:\\Python314\\python.exe scripts/q1_b3_study.py --run          # 正常入口 run+write
    ... --verify <study_dir>                                       # 冻结产物字节校验
    ... --replay <study_dir>                                       # 同冻结输入重算
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from quant_research import study  # noqa: E402

INPUT_DIR = REPO / "tmp" / "q1-real-inputs"
OUTPUT_DIR = REPO / "tmp" / "q1-b3-studies"

# 已核对的 B3 研究输入（主因句按冻结分解更正，逐项贡献与 ΔE 恒等；无共享模板、不硬编码进代码库）
Q1_BLOCK = {
    "target_year": "2024",
    "split_at": "2025-04-30",
    "provenance_kind": "历史重建",
    "provenance_note": "两张卡生成时 2024 同期与后续材料均已公开；是历史重建不是事前预测",
    "window_note": "同一公司的多个年度窗口共享年度，重叠，不是独立样本",
    "companies": {
        "002304.SZ": {
            "company_primary_cause": (
                "洋河 2023→2024 年 E 减少 34.67 亿元，主因毛利贡献 −38.01 亿元"
                "（毛利率与销量结构同时承压）；税金及附加减少贡献 +4.43 亿元部分对冲，"
                "费用项（销售/管理/研发）合计 −1.09 亿元；O_2024=−10.47%。"),
            "alternatives": [
                "毛利下滑可来自产品结构/渠道让利/促销费用归类变化，不必然是需求走弱",
                "费用率变化可能含一次性计提或口径重分类",
                "税金及附加受消费税与缴付节奏影响，不随收入等比变化"],
            "next_check": [
                "2025 同期年报同口径 E 分解，检验毛利贡献是否延续为负",
                "现金流量表补充资料：经营现金 vs 合并净利润差额与营运资本三行",
                "若为真实材料：下一期行情事后反应观察（缺合格行情只停价格域）"],
            "target_note": "2025 目标按收入/毛利率双门槛情景测算，属设定情景非管理层承诺",
            # §18.10:825 目标期新实际（独立续页）：2025 归母 22.060589 亿 vs 研究者 T 100.159300 亿。
            # period 是唯一硬编码——具体读数由 study 层从冻结评估读数取（评价截止门控）。
            "target_followup": {
                "period": "2025",
                "metric_label": "归母净利润（合并口径）",
                "nature": ("研究者设定目标核查：不是管理层指引，也不构成业绩承诺；"
                           "新读数不回填旧卡"),
            },
        },
        "600031.SH": {
            "company_primary_cause": (
                "三一 2023→2024 年 E 增加 11.55 亿元，主因毛利贡献 +13.86 亿元；"
                "研发费用减少贡献 +4.84 亿元，被销售/管理费用增长（合计 −7.08 亿元）"
                "部分抵消；O_2024=+1.58%。"),
            "alternatives": [
                "毛利改善可来自出口结构/一次性质保金变动，不必然是周期反转",
                "研发费用下降可能是资本化或费用归类调整，不等于投入收缩",
                "Q2 当期达标不代表全年趋势，受发货与确认节奏影响"],
            "next_check": [
                "2025 同期年报同口径 E 分解（材料未到 → 保持 pending，不写读数）",
                "现金流量表补充资料：经营现金 vs 合并净利润差额与营运资本三行",
                "若为真实材料：下一期行情事后反应观察（缺合格行情只停价格域）"],
            "target_note": "三一 Q2 毛利率缺口 −1.86pp、营收 −7.04%：属条件下的可承受空间，非目标达成",
            # 同一入口声明核查：三一 2025 年报材料未到 → 预期保持待数据、不写实际读数。
            "target_followup": {
                "period": "2025",
                "metric_label": "归母净利润（合并口径）",
                "nature": ("研究者设定目标核查：不是管理层指引，也不构成业绩承诺；"
                           "新读数不回填旧卡"),
            },
        },
    },
}

# 3×3「利润目标＋天数冲击占用」网格（规划 §18.10）：g/m 是研究者假设；天数冲击 +10/+10 可切换 0；
# B 缺省未设预算（利润状态照算、不自动给可行绿灯）；演示预算 12 亿/35 亿走定向测试与同版改参流程。
JOINT_GRID_BLOCK = {
    "collection_days_delta": 10,
    "inventory_days_delta": 10,
    "grids": {
        "002304.SZ": {
            "growth_values": [0.0, 0.10, 0.20],
            "margin_deltas": [0.0, 0.02, 0.04],
            "budget": None,
            "goal_note": ("研究者设定：恢复 2023 年归母净利润旧水平（T=冻结读数 2023 年 "
                          "100.159300 亿元）——不是管理层指引，不构成业绩承诺"),
        },
        "600031.SH": {
            "growth_values": [-0.10, -0.05, 0.0],
            "margin_deltas": [-0.02, 0.0, 0.02],
            "budget": None,
            "goal_note": ("研究者设定：保持 2023 年归母净利润明确底线（T=冻结读数 2023 年 "
                          "45.274510 亿元）——不是管理层指引，不构成业绩承诺"),
        },
    },
}

B3_TITLE = ("B3：洋河/三一 2024 经营机制卡＋利润目标×天数冲击 3×3 网格"
            "（官方缓存年报；特征截止 2025-04-30）")


def build_spec() -> dict:
    """复用 B1 冻结 spec（financial 描述符原样），只覆盖 B3 字段。"""
    spec = json.loads((INPUT_DIR / "study.json").read_text(encoding="utf-8"))
    spec["name"] = B3_TITLE
    spec["feature_cutoff"] = "2025-04-30"
    spec["evaluation_as_of"] = "2026-10-09"
    spec["q1"] = Q1_BLOCK
    spec["financial"]["joint_grid"] = JOINT_GRID_BLOCK
    return spec


def _print_grids(result: dict) -> None:
    for code in sorted(result.get("financial") or {}):
        grid = ((result.get("financial") or {}).get(code) or {}).get("joint_grid")
        if not grid:
            continue
        t = grid.get("target") or {}
        print(f"grid {code}: base={grid.get('base_period')} "
              f"T={float(t.get('value') or 0) / 1e8:,.4f} 亿（{t.get('period')}）"
              f" cells={len(grid.get('cells') or [])}")
        for cell in grid.get("cells") or []:
            if cell.get("P") is None:
                print(f"   g={cell['g']:+.0%} m={cell['m']:+.0%}  不可算：{cell.get('reason')}")
                continue
            print("   g={:+.0%} m={:+.0%}  P={:>13.4f} 亿  P−T={:>13.4f} 亿  C={:>12.4f} 亿  {}"
                  .format(cell["g"], cell["m"], cell["P"] / 1e8,
                          (cell.get("P_minus_T") or 0.0) / 1e8,
                          (cell.get("C") or 0.0) / 1e8, cell.get("profit_state")))


def main() -> int:
    ap = argparse.ArgumentParser(description="B3 Q1 study launcher（正常研究入口）")
    ap.add_argument("--run", action="store_true", help="run_study + write_study（隔离目录）")
    ap.add_argument("--verify", metavar="STUDY_DIR")
    ap.add_argument("--replay", metavar="STUDY_DIR")
    args = ap.parse_args()
    spec = build_spec()
    if args.run:
        result = study.run_study(spec, input_dir=INPUT_DIR)
        out = study.write_study(result, output_dir=OUTPUT_DIR)
        q1 = result.get("q1") or {}
        print("study_dir =", out)
        print("study_id =", result["study_id"][:16], "…")
        print("result_hash =", result["result_hash"][:16], "…")
        print("q1 target_year =", q1.get("target_year"), "split =", q1.get("split_at"))
        print("q1 cards =", ", ".join(sorted((q1.get("cards") or {}))))
        for c, f in sorted((q1.get("followups") or {}).items()):
            print(f"q1 followup {c} = {f.get('status')}（{f.get('mechanism_verdict')}）")
            tf = f.get("target_followup")
            if isinstance(tf, dict):
                if tf.get("status") == "已记录":
                    act = tf.get("actual") or {}
                    print(f"q1 target {c} = {tf.get('verdict')}"
                          f"（实际 {float(act.get('value') or 0) / 1e8:,.6f} 亿 vs T "
                          f"{float((tf.get('target') or {}).get('value') or 0) / 1e8:,.6f} 亿，"
                          f"披露 {act.get('disclosed_at') or '—'}）")
                else:
                    print(f"q1 target {c} = {tf.get('status')}——{tf.get('pending_reason')}")
        ev = (q1.get("evaluation") or {})
        print("increment verdict =", (ev.get("increment") or {}).get("verdict"))
        _print_grids(result)
        return 0
    if args.verify:
        rep = study.verify_study(Path(args.verify))
        print(json.dumps({"ok": rep["ok"], "checked": rep["checked"],
                          "failed": rep["failed"]}, ensure_ascii=False, indent=1))
        return 0 if rep["ok"] else 1
    if args.replay:
        rep = study.replay_study(Path(args.replay))
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep.get("ok") else 1
    print(json.dumps({"feature_cutoff": spec["feature_cutoff"],
                      "evaluation_as_of": spec["evaluation_as_of"],
                      "q1_target_year": spec["q1"]["target_year"],
                      "q1_split_at": spec["q1"]["split_at"],
                      "companies": sorted(spec["q1"]["companies"]),
                      "joint_grid": {c: {"g": g["growth_values"], "m": g["margin_deltas"],
                                         "budget": g["budget"]}
                                     for c, g in JOINT_GRID_BLOCK["grids"].items()},
                      "financial_file": spec["financial"]["file"],
                      "input_dir": str(INPUT_DIR)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
