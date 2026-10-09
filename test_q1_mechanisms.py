# -*- coding: utf-8 -*-
"""Q1-v1 纯模块的**针对性**单元测试（本批新增公式/单位/队列语义，不重跑既有套件）。

覆盖九条不能再放宽的反例（§17.2/§17.6）：

1. 五项输入缺一项 ⇒ E 不可算（missing_inputs），**绝不填 0**；
2. 分母收入 ≤ 0 / 缺失 ⇒ O/Y_op 不可用，不给负分母方向；
3. 同一 (指标, 年度) 两个不相容值 ⇒ 整格冲突，不择一；
4. 千元→元一次换算后再喂读数层 ⇒ 无二次缩放（unit=元 才可入）；
5. PIT：state != ok 的观察不进读数；
6. 零预测 / 零实际 ⇒ 弃权或单列，不进胜负分母；
7. exploration_majority 平票/退化 ⇒ 未知 + 弃权，不装作有信号；
8. 跨切分（决策在前、结果在后）⇒ crossing，两边都不算；
9. 归母基线缺期 ⇒ unavailable，不借 E 的符号凑数。
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from quant_research import mechanisms as M          # noqa: E402
from quant_research import evaluate_mechanisms as EV  # noqa: E402


def _obs(metric, year, value, *, entity="X", state="ok", unit="元", caliber="合并",
         period_type="", end=None, fact_id="", **kw):
    return {
        "entity_id": entity, "metric": metric, "value": value, "state": state,
        "unit": unit, "caliber": caliber, "period_type": period_type,
        "period": f"{year}年度", "period_end": end or f"{year}-12-31",
        "fact_id": fact_id or f"F-{metric}-{year}", **kw,
    }


def _complete_year(year, *, revenue=Decimal(200), gp=Decimal(150), sell=Decimal(10),
                   admin=Decimal(10), rd=Decimal(5), tax=Decimal(5),
                   net_profit=Decimal(100)):
    """五个 E 项 + 收入 + 归母基线，全表口径合规。"""
    return [
        _obs("revenue", year, revenue),
        _obs("gross_profit", year, gp, derived_from=["revenue", "operating_cost"],
             formula_version="q1_projection/1.0.0"),
        _obs("selling_expense", year, sell),
        _obs("admin_expense", year, admin),
        _obs("rd_expense", year, rd),
        _obs("taxes_and_surcharges", year, tax),
        _obs("net_profit", year, net_profit),
    ]


class MissingInputTests(unittest.TestCase):
    """纪律 1：缺项 ⇒ E 不可算，绝不填 0。"""

    def test_missing_rd_blocks_e(self):
        obs = _complete_year("2023") + _complete_year("2024")
        no_rd = [o for o in obs if not (o["metric"] == "rd_expense" and o["period"].startswith("2024"))]
        r = M.readings(no_rd, entity_id="X")
        self.assertEqual(r["rows"]["2024"]["e_state"], "missing_inputs")
        self.assertIsNone(r["rows"]["2024"]["e"])
        self.assertIn("rd_expense", r["rows"]["2024"]["missing_inputs"])
        # E_2024 缺项 ⇒ 2024 不成链（只 computed 年成链），2023 的 Y_op 转待数据
        self.assertNotIn("2024", [c["t"] for c in r["chains"]])
        chain23 = next(c for c in r["chains"] if c["t"] == "2023")
        self.assertIsNone(chain23["y_op"])
        self.assertEqual(chain23["y_state"], "pending")

    def test_zero_fill_is_rejected_at_index(self):
        """把缺项填 0 会改变 e_state 吗？不会——0 是值不是缺项；但作为反例：
        显式 0 与真实缺项在 readings 里必须可区分（components 有值 vs missing_inputs）。"""
        obs = _complete_year("2023") + _complete_year("2024")
        zeroed = [dict(o, value=0) if (o["metric"] == "rd_expense" and
                                       o["period"].startswith("2024")) else o for o in obs]
        r = M.readings(zeroed, entity_id="X")
        row = r["rows"]["2024"]
        self.assertEqual(row["e_state"], "computed")   # 0 是合法值 → 进计算
        self.assertIn("rd_expense", row["components"])  # 但在 components 里可见，审计可查


class DenominatorTests(unittest.TestCase):
    """纪律 2：分母收入 ≤ 0 / 缺失 ⇒ 不可算。"""

    def test_nonpositive_base_revenue_blocks_o(self):
        obs = _complete_year("2023", revenue=Decimal(0)) + _complete_year("2024")
        r = M.readings(obs, entity_id="X")
        chain = next(c for c in r["chains"] if c["t"] == "2024")
        self.assertIsNone(chain["o_t"])
        self.assertEqual(chain["state"], "o_unavailable")
        self.assertIn("上期收入", chain["reason"])

    def test_nonpositive_t_revenue_blocks_y(self):
        # revenue 不进 E 的五项，所以 E_2024 仍可算；t=2024 有下一年(2025)但自身收入为负 → Y_op 不可用
        obs = (_complete_year("2023") + _complete_year("2024", revenue=Decimal(-5))
               + _complete_year("2025"))
        r = M.readings(obs, entity_id="X")
        chain = next(c for c in r["chains"] if c["t"] == "2024")
        self.assertIsNone(chain["y_op"])
        self.assertEqual(chain["y_state"], "unavailable")


class ConflictTests(unittest.TestCase):
    """纪律 3：同格冲突 ⇒ 整格拒绝，不择一。"""

    def test_conflicting_revenue_kills_year(self):
        obs = _complete_year("2023") + _complete_year("2024") + [
            _obs("revenue", "2023", Decimal(999), fact_id="F-alt"),
        ]
        r = M.readings(obs, entity_id="X")
        self.assertEqual(len(r["conflicts"]), 1)
        self.assertEqual(r["conflicts"][0]["metric"], "revenue")
        self.assertEqual(r["rows"]["2023"]["e_state"], "computed")   # revenue 不在 E 项里
        chain = next(c for c in r["chains"] if c["t"] == "2024")
        # 2023 revenue 冲突 → 2024 的 O 分母没了
        self.assertIsNone(chain["o_t"])
        self.assertEqual(chain["state"], "o_unavailable")

    def test_conflicting_component_kills_e(self):
        obs = _complete_year("2023") + [
            _obs("rd_expense", "2023", Decimal(777), fact_id="F-alt"),
        ]
        r = M.readings(obs, entity_id="X")
        row = r["rows"]["2023"]
        self.assertEqual(row["e_state"], "conflict")
        self.assertIsNone(row["e"])
        self.assertIn("rd_expense", row["conflicting_inputs"])


class UnitScalingTests(unittest.TestCase):
    """纪律 4：千元→元在**索引前**换算一次；读数层只认 元，无二次缩放。"""

    def test_thousand_unit_rows_rejected_not_rescaled(self):
        """千元行（未换算就喂进来）必须被拒，既不 ×1000 也不当元用；缺项如实标 missing。"""
        base = _complete_year("2023") + _complete_year("2024")
        obs = [o for o in base if not (o["metric"] == "rd_expense"
                                       and o["period"].startswith("2024"))]
        obs.append(_obs("rd_expense", "2024", Decimal(538062), unit="千元"))  # 千元，未换算
        r = M.readings(obs, entity_id="X")
        self.assertEqual([x["reason"] for x in r["rejected"]], ["unit_not_yuan"])
        self.assertEqual(r["rows"]["2024"]["e_state"], "missing_inputs")
        self.assertIn("rd_expense", r["rows"]["2024"]["missing_inputs"])

    def test_prescaled_thousand_chain_is_x1(self):
        """三一路径：千元打印值 ×1000 后喂入（unit=元）→ O 量级与元级一致。"""
        thousand = [  # 原始千元
            _obs("revenue", "2023", Decimal(73221725)),
            _obs("gross_profit", "2023", Decimal(19170672), derived_from=["r", "c"]),
            _obs("selling_expense", "2023", Decimal(5101926)),
            _obs("admin_expense", "2023", Decimal(2650981)),
            _obs("rd_expense", "2023", Decimal(5864595)),
            _obs("taxes_and_surcharges", "2023", Decimal(423706)),
            _obs("net_profit", "2023", Decimal(4527451)),
            _obs("revenue", "2024", Decimal(77773391)),
            _obs("gross_profit", "2024", Decimal(20556432), derived_from=["r", "c"]),
            _obs("selling_expense", "2024", Decimal(5464214)),
            _obs("admin_expense", "2024", Decimal(2996449)),
            _obs("rd_expense", "2024", Decimal(5380621)),
            _obs("taxes_and_surcharges", "2024", Decimal(431159)),
            _obs("net_profit", "2024", Decimal(5975451)),
        ]
        scaled = [dict(o, value=Decimal(str(o["value"])) * 1000) for o in thousand]
        r_th = M.readings(thousand, entity_id="K")     # 忘换算：能算，但口径标注=元 → 错
        r_ok = M.readings(scaled, entity_id="K")       # 换算一次：正确
        # 比率是量纲不变的，但绝对值差 1000 倍——单位账本必须保证喂进来的已是元
        self.assertAlmostEqual(r_th["chains"][0]["o_t"], r_ok["chains"][0]["o_t"], places=12)
        self.assertEqual(r_ok["rows"]["2024"]["revenue"], 77773391000.0)
        self.assertEqual(r_th["rows"]["2024"]["revenue"], 77773391.0)


class PitAdmissionTests(unittest.TestCase):
    """纪律 5：state != ok 的行不进读数（PIT 排除走 state 位，不靠删字段）。"""

    def test_excluded_row_is_not_used_as_value(self):
        base = _complete_year("2023") + _complete_year("2024")
        # 2024 研发费用只有一条「截止后才可用」被排除的行：E 必须缺项，而不是拿 999 顶替
        obs = [o for o in base if not (o["metric"] == "rd_expense"
                                       and o["period"].startswith("2024"))]
        obs.append(_obs("rd_expense", "2024", Decimal(999), state="excluded_late"))
        r = M.readings(obs, entity_id="X")
        row = r["rows"]["2024"]
        self.assertEqual(row["e_state"], "missing_inputs")
        self.assertIsNone(row["e"])
        self.assertNotIn("rd_expense", row["components"])
        self.assertIn("rd_expense", row["missing_inputs"])


class AbstainTests(unittest.TestCase):
    """纪律 6：零预测/零实际不进胜负分母。"""

    def _eval_with(self, sign_o, sign_y):
        case = {"case_id": "X::t=2023", "entity_id": "X", "t": "2023", "next_year": "2024",
                "decision_at": "2024-04-27", "outcome_at": "2025-04-29",
                "sign_o": sign_o, "sign_y": sign_y, "y_state": "mature",
                "baseline_profit_sign": "正", "rule_available": True,
                "time_unknown": False}
        out = EV.evaluate([case], pool=["X"], split_at="2025-04-30")
        return out["tallies"]["rule"]

    def test_zero_prediction_abstains(self):
        t = self._eval_with("零", "负")
        self.assertEqual(t["abstain"], 1)
        self.assertEqual(t["scored"], 0)
        self.assertIsNone(t["error_rate"])

    def test_zero_actual_is_separate_bucket(self):
        t = self._eval_with("正", "零")
        self.assertEqual(t["zero_actual"], 1)
        self.assertEqual(t["scored"], 0)
        self.assertEqual(t["abstain"], 0)

    def test_hit_and_miss(self):
        self.assertEqual(self._eval_with("负", "负")["hit"], 1)
        t = self._eval_with("正", "负")
        self.assertEqual(t["miss"], 1)
        self.assertEqual(t["error_rate"], 1.0)


class MajorityTests(unittest.TestCase):
    """纪律 7：探索期多数方向平票/退化 ⇒ 弃权。"""

    def test_tie_abstains(self):
        cases = [{"case_id": f"E::t={y}", "sign_y": s, "y_state": "mature"}
                 for y, s in (("2022", "正"), ("2023", "负"))]
        m = EV.exploration_majority(cases)
        self.assertEqual(m["state"], "tie")
        self.assertEqual(m["sign"], "未知")

    def test_degenerate_single_sample(self):
        m = EV.exploration_majority([{"case_id": "E::t=1", "sign_y": "正", "y_state": "mature"}])
        self.assertEqual(m["state"], "fixed")
        self.assertTrue(m["degenerate"])
        self.assertIn("退化", m["note"])

    def test_majority_not_scored_on_exploration(self):
        cases = [
            {"case_id": "E::t=2022", "entity_id": "E", "t": "2022", "next_year": "2023",
             "decision_at": "2023-04-27", "outcome_at": "2024-04-27", "sign_o": "正",
             "sign_y": "正", "y_state": "mature", "baseline_profit_sign": "正",
             "rule_available": True, "time_unknown": False},
            {"case_id": "E::t=2023", "entity_id": "E", "t": "2023", "next_year": "2024",
             "decision_at": "2024-04-27", "outcome_at": "2025-04-29", "sign_o": "正",
             "sign_y": "正", "y_state": "mature", "baseline_profit_sign": "正",
             "rule_available": True, "time_unknown": False},
        ]
        out = EV.evaluate(cases, pool=["E"], split_at="2025-04-30")
        self.assertIn("E::t=2022", out["split"]["exploration"])
        rows = {c["case_id"]: c for c in out["case_rows"]}
        self.assertEqual(rows["E::t=2022"]["majority_baseline_state"], "not_scored_exploration")


class CrossingTests(unittest.TestCase):
    """纪律 8：决策在切分前、结果在切分后 ⇒ crossing，两边都不算。"""

    def test_crossing_bucket(self):
        case = {"case_id": "X::t=2024", "entity_id": "X", "t": "2024", "next_year": "2025",
                "decision_at": "2025-04-29", "outcome_at": "2026-04-28",
                "sign_o": "负", "sign_y": "负", "y_state": "mature",
                "baseline_profit_sign": "负", "rule_available": True, "time_unknown": False}
        out = EV.evaluate([case], pool=["X"], split_at="2025-04-30")
        self.assertIn("X::t=2024", out["split"]["crossing"])
        self.assertEqual(out["split"]["exploration"], [])
        self.assertEqual(out["split"]["holdout"], [])
        self.assertEqual(out["tallies"]["rule"]["cases"][0]["verdict"], "hit")  # 记分但不入探索/留出


class BaselineTests(unittest.TestCase):
    """纪律 9：归母基线缺期 ⇒ unavailable。"""

    def test_missing_prev_net_profit(self):
        obs2023 = [o for o in _complete_year("2023") if o["metric"] != "net_profit"]
        obs = obs2023 + _complete_year("2024")
        r = M.readings(obs, entity_id="X")
        chain = next(c for c in r["chains"] if c["t"] == "2023")
        self.assertEqual(chain["baseline_profit"]["state"], "unavailable")
        self.assertEqual(chain["baseline_profit"]["sign"], "未知")


class IncrementDirectionTests(unittest.TestCase):
    """B2 纠偏：_increment 必须**分方向**（优/同/差）、同样本比较、零留出不报优势。"""

    @staticmethod
    def _case(i, *, decision_at, outcome_at, sign_o, sign_y, baseline_profit_sign):
        return {"case_id": f"E{i}::t=2030", "entity_id": f"E{i}", "t": "2030",
                "next_year": "2031", "decision_at": decision_at,
                "outcome_at": outcome_at, "sign_o": sign_o, "sign_y": sign_y,
                "y_state": "mature", "baseline_profit_sign": baseline_profit_sign,
                "rule_available": True, "time_unknown": False}

    def _run(self, cases):
        pool = sorted({c["entity_id"] for c in cases})
        return EV.evaluate(cases, pool=pool, split_at="2025-04-30")["increment"]

    def test_rule_worse_is_disadvantage_not_advantage(self):
        # 规则错 5/5、基线错 0/5 ⇒ 必须"差"，绝不能说成更优/无证据。
        cases = [self._case(i, decision_at="2025-06-01", outcome_at="2026-06-01",
                            sign_o="正", sign_y="负", baseline_profit_sign="负")
                 for i in range(5)]
        inc = self._run(cases)
        self.assertEqual(inc["direction"], "差")
        self.assertEqual(inc["verdict"], "disadvantage")
        self.assertIn("更差", inc["reason"])
        self.assertIn("规则误判 5/5", inc["reason"])
        self.assertIn("归母基线误判 0/5", inc["reason"])

    def test_equal_misses_means_no_increment(self):
        cases = [self._case(i, decision_at="2025-06-01", outcome_at="2026-06-01",
                            sign_o="正" if i else "负", sign_y="正" if i else "负",
                            baseline_profit_sign="正" if i else "负")
                 for i in range(5)]
        inc = self._run(cases)
        self.assertEqual(inc["direction"], "同")
        self.assertEqual(inc["verdict"], "no_evidence")
        self.assertIn("没有增量", inc["reason"])
        self.assertIn("共同有效样本 5 例", inc["reason"])

    def test_better_on_exploration_but_zero_holdout_no_advantage(self):
        # 方向为优但全部案例都在切分前（探索期）⇒ 不报优势。
        cases = [self._case(i, decision_at="2024-06-01", outcome_at="2025-04-01",
                            sign_o="正", sign_y="负" if i == 4 else "正",
                            baseline_profit_sign="负")
                 for i in range(5)]
        inc = self._run(cases)
        self.assertEqual(inc["direction"], "优")
        self.assertEqual(inc["verdict"], "no_evidence")
        self.assertIn("留出为 0", inc["reason"])
        self.assertFalse(inc["three_way_comparable"])

    def test_crossing_does_not_substitute_for_holdout(self):
        # 跨切分案例再多也不算留出：留出=0 ⇒ 优势方向也不报优势。
        cases = [self._case(i, decision_at="2025-04-01", outcome_at="2026-06-01",
                            sign_o="负", sign_y="负", baseline_profit_sign="正")
                 for i in range(5)]
        inc = self._run(cases)
        self.assertEqual(inc["direction"], "优")
        self.assertEqual(inc["verdict"], "no_evidence")
        self.assertIn("留出为 0", inc["reason"])
        self.assertEqual(inc["per_bucket"]["crossing"]["rule_scored"], 5)
        self.assertEqual(inc["per_bucket"]["holdout"]["rule_scored"], 0)
        self.assertEqual(inc["per_bucket"]["exploration"]["rule_scored"], 0)

    def test_holdout_advantage_candidate(self):
        # 留出 5 例、规则 0 误判 vs 基线 5 误判、无重叠 ⇒ 才允许"优势候选"。
        cases = [self._case(i, decision_at="2025-06-01", outcome_at="2026-06-01",
                            sign_o="负", sign_y="负", baseline_profit_sign="正")
                 for i in range(5)]
        inc = self._run(cases)
        self.assertEqual(inc["direction"], "优")
        self.assertEqual(inc["verdict"], "directional")
        self.assertIn("优势候选", inc["reason"])
        self.assertEqual(inc["holdout_scored"], 5)

    def test_reason_reports_denominator_and_abstains(self):
        # 零预测是弃权，不进胜负分母；理由里必须写明分母与弃权数。
        cases = [self._case(0, decision_at="2025-06-01", outcome_at="2026-06-01",
                            sign_o="负", sign_y="负", baseline_profit_sign="负"),
                 self._case(1, decision_at="2025-06-01", outcome_at="2026-06-01",
                            sign_o="正", sign_y="负", baseline_profit_sign="正"),
                 self._case(2, decision_at="2025-06-01", outcome_at="2026-06-01",
                            sign_o="零", sign_y="负", baseline_profit_sign="负")]
        inc = self._run(cases)
        self.assertEqual(inc["common_effective_cases"], 2)
        self.assertEqual(inc["rule_abstain"], 1)
        self.assertIn("共同有效样本 2 例", inc["reason"])
        self.assertIn("规则另弃权 1 例", inc["reason"])
        self.assertEqual(inc["direction"], "同")


class FollowUpTests(unittest.TestCase):
    """follow_up：未成熟 ⇒ 待数据/未决且不写读数；已核验才给支持/削弱。"""

    def test_follow_up_pending_writes_no_reading(self):
        case = {"case_id": "X::t=2024", "y_state": "pending", "sign_o": "负",
                "next_year": "2025"}
        f = EV.follow_up(case)
        self.assertEqual(f["status"], "待数据")
        self.assertEqual(f["mechanism_verdict"], "未决")
        self.assertIsNone(f["actual_reading"])

    def test_follow_up_none_case(self):
        f = EV.follow_up(None)
        self.assertEqual(f["status"], "待数据")
        self.assertEqual(f["mechanism_verdict"], "未决")

    def test_follow_up_agreement(self):
        case = {"case_id": "X::t=2024", "y_state": "mature", "sign_o": "负",
                "sign_y": "负", "y_op": -0.19, "t": "2024", "next_year": "2025",
                "outcome_at": "2026-04-28"}
        f = EV.follow_up(case)
        self.assertEqual(f["status"], "已核验")
        self.assertEqual(f["mechanism_verdict"], "支持")
        self.assertEqual(f["verify_by"], "2026-04-28")

    def test_follow_up_opposite_is_undermine(self):
        case = {"case_id": "X::t=2023", "y_state": "mature", "sign_o": "正",
                "sign_y": "负", "y_op": -0.10, "t": "2023", "next_year": "2024"}
        self.assertEqual(EV.follow_up(case)["mechanism_verdict"], "削弱")


class CompositionTests(unittest.TestCase):
    """组成表恒等式：贡献和 = ΔE，残差为 0。"""

    def test_identity(self):
        obs = _complete_year("2023") + _complete_year("2024", gp=Decimal(120),
                                                       revenue=Decimal(190))
        r = M.readings(obs, entity_id="X")
        comp = M.composition(r, "2024")
        self.assertEqual(comp["state"], "ok")
        self.assertEqual(Decimal(str(comp["residual"])), 0)
        total = sum(Decimal(str(i["contribution"])) for i in comp["items"])
        self.assertEqual(total, Decimal(str(comp["delta_e"])))


if __name__ == "__main__":
    unittest.main()
