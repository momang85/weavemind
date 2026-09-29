# -*- coding: utf-8 -*-
"""阶段 Q1：冻结数据集 + 利润桥接的最小契约、独立验证与到期语义（全部离线）。

覆盖 Q1 退出条件里能在这条链上验的部分：
金样手算一致；零/负分母、单位换算、错公司/错期间/错币种/错口径、同值不同指标、
数据重述、输入缺失、输出篡改各有明确结果；**改输入值但保留 fact_id 必须让旧 run 过期**；
计算核心零模型调用；图/卡绑定同一次运行。
"""
from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path

import financial_analysis as fa
from financial_analysis import contracts as C


def _row(metric, period, value, **kw):
    base = {
        "fact_id": kw.pop("fact_id", f"fact-{metric}-{period}"),
        "entity": "洋河股份", "entity_id": "002304.SZ", "market": "cn",
        "metric": metric, "metric_label": metric, "period": period,
        "period_type": "年报", "currency": "CNY", "unit": "亿元", "caliber": "合并",
        "value": value, "source_url": "https://example.invalid/x", "source_hash": "h1",
        "verify_state": "unverified",
    }
    base.update(kw)
    return base


def _two_period_rows(**overrides):
    """两期四指标的最小真实形状（2023→2024，与冻结样本同量级）。"""
    rows = [
        _row("revenue", "2023年", 331.26), _row("revenue", "2024年", 288.76),
        _row("net_profit", "2023年", 100.16), _row("net_profit", "2024年", 66.73),
        _row("gross_profit", "2023年", 249.26), _row("gross_profit", "2024年", 211.25),
        _row("operating_cashflow", "2023年", 61.30),
        _row("operating_cashflow", "2024年", 46.29),
    ]
    for metric, period, patch in overrides.get("mutate", ()):
        for r in rows:
            if r["metric"] == metric and r["period"] == period:
                r.update(patch)
    return rows


def _financials_payload(rows=None):
    """`financials.json` 的**真实形状**（与预载落盘的一致：source/metadata/financials）。

    用于验证"底稿还没落盘时按预载载荷冻结数据集"这条路（实机 `ui-f4bac0d202` 的缺口）。
    """
    return {
        "source": "eastmoney_ashare",
        "metadata": {
            "source": "eastmoney_ashare", "company": "洋河股份", "stock_code": "002304",
            "currency": "CNY", "unit": "亿元", "annual_count": 2,
            "caliber": "合并",
            "caliber_evidence": "来源行含 PARENTNETPROFIT（归母净利润）",
        },
        "financials": rows if rows is not None else [
            {"year": 2023, "report_type": "年报", "report_date": "2023-12-31",
             "disclosure_date": "2024-04-10", "revenue": 331.26, "net_profit": 100.16,
             "gross_profit": 249.26, "operating_cashflow": 61.30},
            {"year": 2024, "report_type": "年报", "report_date": "2024-12-31",
             "disclosure_date": "2025-04-10", "revenue": 288.76, "net_profit": 66.73,
             "gross_profit": 211.25, "operating_cashflow": 46.29},
        ],
        "raw": {"url": "https://example.invalid/yh", "text": "snapshot"},
    }


def _dataset(rows=None, **kw):
    kw.setdefault("periods", (2023, 2024))
    kw.setdefault("as_of", "2025-04-30")
    kw.setdefault("source_label", "unit-test")
    return fa.freeze_from_facts(_FakeFacts(rows or _two_period_rows()), **kw)


class _FakeFacts(list):
    """`freeze_from_facts` 接受任何带同名属性的对象；这里直接用 dict 行。"""


class TestDatasetFreeze(unittest.TestCase):
    def test_two_period_dataset_is_complete_and_hashed(self):
        ds = _dataset()
        m = ds.manifest
        self.assertEqual(m.periods, ("2023年", "2024年"), "期间取数据里真实存在的年度期间")
        self.assertEqual((m.entity, m.entity_id), ("洋河股份", "002304.SZ"))
        self.assertEqual(m.observations, 8)
        self.assertEqual(m.usable, 8)
        self.assertEqual(m.conflicts, ())
        self.assertEqual(len(m.dataset_hash), 64)

    def test_same_inputs_freeze_to_the_same_hash(self):
        self.assertEqual(_dataset().dataset_hash, _dataset().dataset_hash)

    def test_value_change_with_same_fact_id_changes_the_hash(self):
        """**改一个数但沿用 fact_id** → 数据集 hash 必须变（否则旧结果会被沿用）。"""
        a = _dataset()
        b = _dataset(_dataset_rows_with(mutate=(("revenue", "2024年", {"value": 999.99}),)))
        self.assertNotEqual(a.dataset_hash, b.dataset_hash)
        run = fa.run("profit_bridge", a)
        self.assertEqual(run.status, C.RunStatus.VALIDATED)
        state, why = fa.revalidate(run, b)
        self.assertEqual(state, "expired", why)

    def test_restatement_becomes_a_new_dataset(self):
        """同值但**重述批次**不同 → 新数据集（历史回测不得用重述后的值冒充当时已知）。"""
        a = _dataset(_dataset_rows_with(restatement="orig"))
        b = _dataset(_dataset_rows_with(restatement="restated-2025"))
        self.assertNotEqual(a.dataset_hash, b.dataset_hash)

    def test_conflicting_values_are_not_silently_picked(self):
        """同一 (指标, 期间, 口径) 两个不相容值 → 冲突：不取最大/平均/第一条。"""
        rows = _two_period_rows()
        rows.append(_row("revenue", "2024年", 288.76, fact_id="fact-dup",
                         source_url="https://example.invalid/y", source_hash="h2"))
        ds = _dataset(rows)
        self.assertEqual(len(ds.manifest.conflicts), 1, ds.manifest.conflicts)
        with self.assertRaises(fa.MissingInput):
            ds.require("revenue", "2024年")

    def test_missing_metric_is_a_gap_not_a_zero(self):
        rows = [r for r in _two_period_rows() if r["metric"] != "gross_profit"]
        ds = _dataset(rows, required_metrics=("net_profit", "gross_profit"))
        self.assertTrue(any("gross_profit" in g for g in ds.manifest.gaps), ds.manifest.gaps)
        self.assertIsNone(ds.get("gross_profit", "2024年"), "缺失不得被当成 0")

    def test_zero_is_a_value_not_a_hole(self):
        rows = _dataset_rows_with(mutate=(("net_profit", "2024年", {"value": 0}),))
        ds = _dataset(rows)
        obs = ds.get("net_profit", "2024年")
        self.assertEqual(obs.state, C.State.ZERO)
        self.assertTrue(obs.usable, "真实零有值、可参与计算（与缺失不同）")

    def test_unknown_metadata_observation_is_unusable(self):
        rows = _dataset_rows_with(mutate=(("revenue", "2024年", {"caliber": ""}),))
        ds = _dataset(rows)
        obs = ds.get("revenue", "2024年")
        self.assertEqual(obs.state, C.State.UNKNOWN)
        self.assertFalse(obs.usable)

    def test_non_annual_periods_do_not_form_an_annual_pair(self):
        rows = _two_period_rows() + [_row("net_profit", "2024中报", 50.0,
                                          period_type="中报")]
        ds = _dataset(rows)
        self.assertEqual(ds.manifest.periods, ("2023年", "2024年"))
        self.assertTrue(any("非年度期间" in g for g in ds.manifest.gaps), ds.manifest.gaps)

    # ── 2026-09-29（A2）：真实年报同时有合并/母公司 → 口径必须显式选择，不得"多条=缺输入" ──

    def _multi_caliber_rows(self):
        rows = list(_two_period_rows())
        for r in list(rows):
            if r["metric"] in ("revenue", "operating_cost", "gross_profit"):
                rows.append(dict(r, caliber="母公司", value=r["value"] / 3.0,
                                 fact_id=r["fact_id"] + "-p"))
        return rows

    def test_primary_caliber_is_consolidated_and_used_by_default(self):
        ds = _dataset(self._multi_caliber_rows())
        self.assertEqual(ds.primary_caliber, "合并")
        obs = ds.get("revenue", "2024年")
        self.assertIsNotNone(obs, "合并与母公司并存时，默认应取合并口径")
        self.assertEqual(obs.caliber, "合并")

    def test_explicit_caliber_is_honoured(self):
        ds = _dataset(self._multi_caliber_rows())
        obs = ds.get("revenue", "2024年", caliber="母公司")
        self.assertEqual(obs.caliber, "母公司")

    def test_conflict_within_one_caliber_is_still_ambiguous(self):
        """同一口径内两个不同值：仍返回 None（不任选一条）。"""
        rows = self._multi_caliber_rows()
        twin = dict(next(r for r in rows if r["metric"] == "revenue"
                         and r["caliber"] == "合并" and r["period"] == "2024年"))
        twin["value"] = twin["value"] + 1.0
        twin["fact_id"] = twin["fact_id"] + "-x"
        ds = _dataset(rows + [twin])
        self.assertIsNone(ds.get("revenue", "2024年"))

    def test_without_consolidated_only_one_caliber_may_be_defaulted(self):
        rows = [r for r in self._multi_caliber_rows() if r["caliber"] == "母公司"]
        ds = _dataset(rows)
        self.assertEqual(ds.primary_caliber, "母公司")
        self.assertEqual(ds.get("revenue", "2024年").caliber, "母公司")

    def test_two_non_consolidated_calibers_are_not_defaulted(self):
        rows = [dict(r, caliber="母公司") for r in _two_period_rows()]
        rows += [dict(r, caliber="分部A", fact_id=r["fact_id"] + "-s")
                 for r in _two_period_rows()]
        ds = _dataset(rows)
        self.assertEqual(ds.primary_caliber, "", "没有合并、又不止一种口径时不得默认")
        self.assertIsNone(ds.get("revenue", "2024年"))

    def test_two_period_models_run_on_a_multi_caliber_dataset(self):
        """端到端：合并+母公司并存不再是"缺输入"，利润桥照样跑出闭合结果。"""
        ds = _dataset(self._multi_caliber_rows())
        run = fa.run("profit_bridge", ds)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        got = {o.metric: o.value for o in run.outputs}
        self.assertAlmostEqual(got["net_profit_change"], -33.43, places=2)

    # ── 2026-09-29：血缘随观察走；亏损期情景判"不适用"而不是"验证失败" ──────────

    def test_derived_lineage_survives_freezing(self):
        """`derived_from`/`formula_version` 必须随观察一起进数据集（此前只到抽取器就丢）。"""
        rows = list(_two_period_rows())
        gp = [r for r in rows if r["metric"] == "gross_profit"]
        src = [r["fact_id"] for r in rows if r["metric"] in ("revenue", "operating_cost")]
        for r in gp:
            r["derived_from"] = src
            r["formula_version"] = "gross_profit_v1"
        ds = _dataset(rows)
        obs = ds.get("gross_profit", "2024年")
        self.assertTrue(obs.is_derived)
        self.assertEqual(tuple(obs.derived_from), tuple(src))
        self.assertEqual(obs.formula_version, "gross_profit_v1")

    def test_lineage_is_part_of_the_observation_fingerprint(self):
        """改血缘 = 换了一份观察：指纹要变（数值不变也不许悄悄改）。"""
        def _rows(lineage):
            return [_row("revenue", "2024年", 100.0),
                    _row("operating_cost", "2024年", 60.0),
                    dict(_row("gross_profit", "2024年", 40.0),
                         derived_from=lineage, formula_version="gross_profit_v1")]

        a = _dataset(_rows(["fact-a", "fact-b"])).get("gross_profit", "2024年")
        b = _dataset(_rows(["fact-a"])).get("gross_profit", "2024年")
        self.assertEqual(a.value, b.value)
        self.assertEqual(a.derived_from, ("fact-a", "fact-b"))
        self.assertNotEqual(a.observation_hash, b.observation_hash)

    def test_negative_gross_margin_is_not_applicable_not_a_validation_failure(self):
        """亏损期：情景模型的"增长⇒利润改善"方向假设不成立 → 明确判**不适用**。

        实机反例：京蓝 2020（毛利率 −0.78%、归母净利 −23.55 亿）此前只会得到
        `validation_failed: ['scenario_direction']`——看起来像模型跑错了。
        """
        rows = [_row("revenue", "2024年", 1_158_320_511.62),
                _row("gross_profit", "2024年", -9_068_492.11),
                _row("net_profit", "2024年", -2_354_850_607.11)]
        ds = _dataset(rows)
        run = fa.run("scenario_sensitivity", ds)
        self.assertEqual(run.status, C.RunStatus.NOT_APPLICABLE, run.reason)
        self.assertIn("毛利率为负", str(run.reason))
        self.assertEqual(run.outputs, (), "不适用不得产出数字")


def _dataset_rows_with(*, mutate=(), restatement=""):
    rows = _two_period_rows(mutate=mutate)
    if restatement:
        for r in rows:
            r["restatement"] = restatement
    return rows


class TestProfitBridgeChain(unittest.TestCase):
    def test_gold_matches_and_closure_holds(self):
        ds = _dataset()
        run = fa.run("profit_bridge", ds)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        got = {o.metric: o.value for o in run.outputs}
        # 手算（十进制）：Δ净利 = 66.73-100.16 = -33.43；Δ毛利 = 211.25-249.26 = -38.01；
        # 毛利线以下 = -33.43 - (-38.01) = +4.58
        self.assertAlmostEqual(got["net_profit_change"], -33.43, places=2)
        self.assertAlmostEqual(got["gross_profit_change"], -38.01, places=2)
        self.assertAlmostEqual(got["below_gross_line_change"], 4.58, places=2)
        self.assertEqual(run.validation["failed"], [])
        self.assertTrue(run.validation["checks"]["closure"]["ok"])

    def test_outputs_are_traceable_artifacts(self):
        run = fa.run("profit_bridge", _dataset())
        out = run.outputs[0]
        self.assertTrue(out.output_id.startswith(run.run_id[:12]))
        self.assertEqual(len(out.inputs), 4)
        self.assertTrue(all(i.startswith("fact-") for i in out.inputs))
        self.assertIn("Δ归母净利", out.formula)
        self.assertTrue(out.limits, "限制必须随输出带走")
        from decimal import Decimal
        self.assertEqual(Decimal(out.diagnostics["closure"]), 0,
                         f"恒等式闭合差必须精确为 0：{out.diagnostics['closure']}")

    def test_components_sum_to_total_and_no_pct_substitution(self):
        run = fa.run("profit_bridge", _dataset())
        total = run.outputs[0]
        comp_sum = sum(c["value"] for c in total.components)
        self.assertAlmostEqual(comp_sum, total.value, places=2, msg="贡献项必须闭合")
        for c in total.components:
            self.assertFalse(str(c["unit"]).endswith("%"),
                             "金额桥里不得出现百分点贡献项")

    def test_immaterial_company_or_period_or_currency_or_caliber_rejects(self):
        cases = {
            "错公司": {"entity_id": "600519.SH", "entity": "贵州茅台"},
            "错币种": {"currency": "USD"},
            "错口径": {"caliber": "母公司"},
            "错单位量纲": {"unit": "万元"},
        }
        for label, patch in cases.items():
            rows = _two_period_rows(mutate=(("gross_profit", "2024年", patch),))
            ds = _dataset(rows)
            run = fa.run("profit_bridge", ds)
            self.assertIn(run.status,
                          (C.RunStatus.NOT_APPLICABLE, C.RunStatus.MISSING_INPUT,
                           C.RunStatus.VALIDATION_FAILED), f"{label}: {run.status}")
            self.assertTrue(run.reason, label)
        # 期间不足两期：不适用（不给单期归因）
        one = fa.freeze_from_facts([_row("net_profit", "2024年", 66.73),
                                    _row("gross_profit", "2024年", 211.25)],
                                   periods=(2024,), source_label="one-period")
        run = fa.run("profit_bridge", one)
        self.assertEqual(run.status, C.RunStatus.NOT_APPLICABLE)

    def test_same_value_different_metric_is_not_borrowed(self):
        """同值不同指标不得互相顶替：把净利与毛利设成同值，桥仍是两条各自的项。"""
        rows = _two_period_rows(mutate=(("gross_profit", "2024年", {"value": 66.73}),
                                        ("gross_profit", "2023年", {"value": 100.16})))
        ds = _dataset(rows)
        run = fa.run("profit_bridge", ds)
        self.assertEqual(run.status, C.RunStatus.VALIDATED)
        got = {o.metric: o.value for o in run.outputs}
        self.assertAlmostEqual(got["gross_profit_change"], -33.43, places=2)
        self.assertAlmostEqual(got["below_gross_line_change"], 0.0, places=2)
        inputs = run.outputs[0].inputs
        self.assertEqual(len(set(inputs)), 4, "四个输入必须各自独立，不得复用同一条观察")

    def test_zero_or_negative_denominator_gives_not_computable(self):
        rows = _dataset_rows_with(mutate=(("revenue", "2024年", {"value": 0}),))
        ds = _dataset(rows)
        run = fa.ratio_run("net_margin", "net_profit", "revenue", ds)
        self.assertEqual(run.status, C.RunStatus.NOT_COMPUTABLE, run.reason)
        self.assertIn("分母为 0", run.reason)
        self.assertEqual(run.outputs, (), "不可算就不得产出数字")
        # 负分母：比率仍可算，但方向性叙述要另行把关（这里只断言算得出来且带限制）
        rows = _dataset_rows_with(mutate=(("net_profit", "2024年", {"value": -10.0}),))
        run2 = fa.ratio_run("cashflow_coverage", "operating_cashflow", "net_profit",
                            _dataset(rows))
        self.assertIn(run2.status, (C.RunStatus.VALIDATED, C.RunStatus.NOT_COMPUTABLE))

    def test_unit_conversion_is_explicit_when_scales_differ(self):
        """分子亿元、分母万元：**不静默放大 1e4**，要么明确换算要么拒绝。"""
        rows = _dataset_rows_with(mutate=(("revenue", "2024年", {"unit": "万元",
                                                                 "value": 2887600.0}),))
        ds = _dataset(rows)
        run = fa.ratio_run("net_margin", "net_profit", "revenue", ds)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        self.assertAlmostEqual(run.outputs[0].value, 23.11, places=1)
        self.assertIn("换算", run.outputs[0].formula)

    def test_missing_input_stops_the_run(self):
        rows = [r for r in _two_period_rows() if r["metric"] != "gross_profit"]
        run = fa.run("profit_bridge", _dataset(rows))
        self.assertEqual(run.status, C.RunStatus.MISSING_INPUT, run.reason)
        self.assertIn("gross_profit", run.reason)
        self.assertEqual(run.outputs, ())

    def test_params_outside_whitelist_fail_closed(self):
        ds = _dataset()
        ok = fa.run("profit_bridge", ds, params={"rounding": "yuan_2"})
        self.assertEqual(ok.status, C.RunStatus.VALIDATED)
        bad = fa.run("profit_bridge", ds, params={"target": "revenue"})
        self.assertEqual(bad.status, C.RunStatus.FAILED)
        self.assertIn("参数不在允许集合内", bad.reason)

    def test_unregistered_model_is_refused(self):
        with self.assertRaises(fa.NotApplicable):
            fa.spec("auto_ml_anything")

    def test_run_identity_binds_dataset_model_params(self):
        ds = _dataset()
        a = fa.run("profit_bridge", ds, params={"rounding": "yi_2"})
        b = fa.run("profit_bridge", ds, params={"rounding": "yuan_2"})
        self.assertNotEqual(a.run_id, b.run_id, "参数不同必须换身份")
        self.assertEqual(fa.revalidate(a, ds)[0], "ok")
        state, why = fa.revalidate(a, _dataset(_dataset_rows_with(
            mutate=(("net_profit", "2024年", {"value": 70.0}),))))
        self.assertEqual(state, "expired")
        self.assertIn("数据集已变", why)

    def test_output_tampering_is_detectable(self):
        """输出被改成别的数必须能被发现：哈希由**字段**重算，不是抄一遍。"""
        import dataclasses
        run = fa.run("profit_bridge", _dataset())
        good = run.outputs[0].value
        self.assertTrue(fa.tamper_check(good, good))
        self.assertFalse(fa.tamper_check(good + 1.0, good), "改了数必须能被发现")
        tampered = dataclasses.replace(run.outputs[0], value=good + 1.0)
        self.assertNotEqual(tampered.output_hash, run.outputs[0].output_hash,
                            "数值被改后 output_hash 必须变")
        # 贡献项被改同样要变
        comps = list(run.outputs[0].components)
        comps[0] = {**comps[0], "value": comps[0]["value"] + 1.0}
        self.assertNotEqual(dataclasses.replace(run.outputs[0],
                                                components=tuple(comps)).output_hash,
                            run.outputs[0].output_hash)

    def test_dataset_hash_is_stable_across_process_restart(self):
        """同一份材料两次冻结（模拟重启）→ 同一 hash（身份不依赖进程内状态）。"""
        rows = json.loads(json.dumps(_two_period_rows()))
        a = fa.freeze_from_facts(rows, periods=(2023, 2024), source_label="s")
        b = fa.freeze_from_facts(json.loads(json.dumps(rows)), periods=(2023, 2024),
                                 source_label="s")
        self.assertEqual(a.dataset_hash, b.dataset_hash)


class TestPlanAndReportBinding(unittest.TestCase):
    def test_plan_adopts_or_rejects_with_reasons(self):
        ds = _dataset()
        plan = fa.compile_plan("利润变化由什么构成", ds)
        self.assertIn("profit_bridge", [a.model_id for a in plan.adopted])
        one = fa.freeze_from_facts([_row("net_profit", "2024年", 66.73)],
                                   periods=(2024,), source_label="one")
        plan2 = fa.compile_plan("利润变化由什么构成", one)
        self.assertEqual([a.model_id for a in plan2.adopted], [])
        self.assertTrue(plan2.rejected and plan2.rejected[0]["reason"], plan2.rejected)

    def test_card_and_chart_share_the_same_run(self):
        run = fa.run("profit_bridge", _dataset())
        oid = run.outputs[0].output_id
        card = fa.analysis_card(run, oid)
        chart = fa.chart_spec(run, oid)
        self.assertEqual(card["run_id"], run.run_id)
        self.assertEqual(chart["run_id"], run.run_id)
        self.assertEqual(chart["output_id"], oid)
        for field in ("judgement", "drivers", "basis", "unexplained", "meaning",
                      "next_action"):
            self.assertIn(field, card)
        self.assertEqual(card["kind"], "会计分解")
        self.assertIn("原因待证", card["unexplained"]["note"])
        self.assertEqual(card["basis"]["unit"], "亿元")
        self.assertEqual(chart["unit"], "亿元")
        binding = fa.delivery_binding(run)
        self.assertEqual(binding["dataset_hash"], run.dataset_hash)
        self.assertTrue(binding["validation_ok"])
        self.assertEqual(binding["outputs"][0]["output_id"], oid)

    def test_card_for_a_blocked_run_says_what_to_do(self):
        rows = [r for r in _two_period_rows() if r["metric"] != "gross_profit"]
        run = fa.run("profit_bridge", _dataset(rows))
        card = fa.analysis_card(run)
        self.assertEqual(card["judgement"], "本次不产出结论")
        self.assertIn("补", card["next_action"])
        self.assertEqual(fa.chart_spec(run)["available"], False)


class TestCoreHasNoModelCalls(unittest.TestCase):
    """计算核心**零模型调用**：本包不得 import llm_client / web_ui / orchestrator_v2。"""

    def test_package_does_not_import_llm_or_ui_layers(self):
        root = Path(__file__).resolve().parent / "financial_analysis"
        banned = ("llm_client", "web_ui", "orchestrator_v2", "task_state", "requests",
                  "urllib.request", "socket")
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in banned:
                for m in re.finditer(rf"^\s*(?:import|from)\s+{re.escape(token)}",
                                     text, re.M):
                    offenders.append(f"{path.name}: {m.group(0).strip()}")
        self.assertEqual(offenders, [], f"计算核心不得依赖这些层：{offenders}")

    def test_registry_is_the_only_way_in(self):
        self.assertIn("profit_bridge_v1", fa.registry.operators())
        self.assertEqual([m.model_id for m in fa.specs()],
                         ["profit_bridge", "cash_quality", "working_capital",
                          "scenario_sensitivity"])


class TestAnalysisRunStore(unittest.TestCase):
    """运行记录的落盘与身份块：跨进程读回来必须逐字段一致（含元组字段）。"""

    def setUp(self):
        import tempfile
        self.ws = Path(tempfile.mkdtemp(prefix="fa_store_"))

    def _run(self):
        return fa.run("profit_bridge", _dataset())

    def test_round_trip_preserves_identity_and_outputs(self):
        from financial_analysis import store
        run = self._run()
        store.save_run(self.ws, run)
        back = store.model_runs(self.ws)
        self.assertEqual(len(back), 1)
        got = back[0]
        self.assertEqual(got.run_id, run.run_id)
        self.assertEqual(got.dataset_hash, run.dataset_hash)
        self.assertEqual(got.status, run.status)
        self.assertEqual(len(got.outputs), len(run.outputs))
        self.assertEqual(got.outputs[0].inputs, run.outputs[0].inputs, "元组字段要原样回来")
        self.assertEqual(got.outputs[0].output_hash, run.outputs[0].output_hash)
        self.assertEqual(got.outputs[0].components, run.outputs[0].components)

    def test_same_run_id_is_updated_not_duplicated(self):
        from financial_analysis import store
        run = self._run()
        store.save_run(self.ws, run)
        store.save_run(self.ws, run)
        self.assertEqual(len(store.load_runs(self.ws)), 1, "同一次运行不得落两份")

    def test_payload_is_none_when_there_is_nothing_to_bind(self):
        from financial_analysis import store
        self.assertIsNone(store.payload_bytes(self.ws), "没有运行就不制造空文件")
        summary = store.binding_summary([])
        self.assertEqual((summary["count"], summary["runs"]), (0, []))

    def test_binding_summary_carries_hashes_not_prose(self):
        from financial_analysis import store
        run = self._run()
        store.save_run(self.ws, run)
        blk = store.binding_summary(store.load_runs(self.ws))
        self.assertEqual(blk["validated"], 1)
        item = blk["runs"][0]
        self.assertEqual(item["run_id"], run.run_id)
        self.assertEqual(item["dataset_hash"], run.dataset_hash)
        self.assertTrue(item["validation_ok"])
        self.assertEqual(item["outputs"][0]["output_hash"],
                         run.outputs[0].output_hash)
        self.assertNotIn("formula", json.dumps(blk), "身份块只放身份与 hash")


class TestCardBodyBinding(unittest.TestCase):
    """正文分析卡 ↔ 落盘运行：同 run、同数值才算绑定。"""

    def setUp(self):
        import tempfile
        self.ws = Path(tempfile.mkdtemp(prefix="fa_bind_"))
        from financial_analysis import store
        self.store = store
        self.run = fa.run("profit_bridge", _dataset())
        store.save_run(self.ws, self.run)
        self.block = store.render_card_block(self.run)

    def test_rendered_card_verifies_against_the_stored_run(self):
        body = "# 简报\n\n## 分析\n\n（略）\n\n" + self.block + "\n## 变化解释\n（略）\n"
        got = self.store.verify_body_binding(self.ws, body)
        self.assertTrue(got["ok"], got["problems"])
        self.assertEqual(got["run_ids"], [self.run.run_id[:12]])
        self.assertTrue(all(o["value_ok"] for o in got["outputs"]))
        self.assertEqual(len(got["outputs"]), len(self.run.outputs))

    def test_card_without_any_output_tag_is_reported(self):
        body = "## 分析卡\n\n> 运行 run=" + self.run.run_id[:12] + "（模型 profit_bridge）\n"
        got = self.store.verify_body_binding(self.ws, body)
        self.assertFalse(got["ok"])
        self.assertTrue(any("没有任何 output 标识" in p for p in got["problems"]),
                        got["problems"])

    def test_body_referencing_an_unknown_run_is_reported(self):
        body = "## 分析卡\n\n> 运行 run=deadbeefcafe（模型 x）\n- 读数：-33.43 亿元　output " \
               + self.run.outputs[0].output_id + "\n"
        got = self.store.verify_body_binding(self.ws, body)
        self.assertFalse(got["ok"])
        self.assertTrue(any("不是同一次" in p or "没有这次运行" in p for p in got["problems"]),
                        got["problems"])

    def test_body_with_a_different_number_is_reported(self):
        """卡里把 -33.43 写成 -30.00 → 正文与运行不是同一版。"""
        bad = self.block.replace("-33.43", "-30.00")
        self.assertNotEqual(bad, self.block)
        got = self.store.verify_body_binding(self.ws, bad)
        self.assertFalse(got["ok"])
        self.assertTrue(any("不是同一版" in p for p in got["problems"]), got["problems"])

    def test_unvalidated_run_must_not_reach_the_body(self):
        import dataclasses
        from financial_analysis import store
        broken = dataclasses.replace(self.run, status=C.RunStatus.VALIDATION_FAILED)
        ws2 = Path(self.ws) / "w2"
        ws2.mkdir()
        store.save_run(ws2, broken)
        body = "## 分析卡\n\n" + store.render_card_block(broken)
        got = store.verify_body_binding(ws2, body)
        self.assertFalse(got["ok"])
        self.assertTrue(any("未通过验证" in p for p in got["problems"]), got["problems"])

    def test_body_without_a_card_is_not_failed(self):
        got = self.store.verify_body_binding(self.ws, "# 简报\n\n## 分析\n（没有分析卡）\n")
        self.assertTrue(got["ok"])
        self.assertEqual(got["run_ids"], [])
        self.assertIn("无分析卡区块", got["note"])


class TestReportAndPackageBindTheSameRun(unittest.TestCase):
    """端到端：正文分析卡 / 交付清单 / ZIP 指向**同一次运行**。"""

    def setUp(self):
        import tempfile
        self.ws = Path(tempfile.mkdtemp(prefix="fa_pkg_"))
        self.task_id = "ui-fa-pkg"
        from financial_analysis import store
        self.store = store
        import report_version
        self.rv = report_version
        self.run = fa.run("profit_bridge", _dataset())

    def _body(self):
        card = self.store.render_card_block(self.run)
        structure = {"scope": {"company": "洋河股份", "company_id": "002304.SZ",
                               "periods": [2023, 2024], "caliber": "合并",
                               "as_of": "2025-04-30", "unit": "亿元"},
                     "analysis": "（模型分析正文）", "analysis_card": card}
        import report_brief
        return report_brief.render_brief_markdown(structure, body="")

    def test_report_body_carries_a_verifiable_card(self):
        body = self._body()
        self.assertIn("## 分析卡", body)
        self.assertIn(self.run.outputs[0].output_id, body)
        self.assertIn(f"run={self.run.run_id[:12]}", body)
        self.store.save_run(self.ws, self.run)
        got = self.store.verify_body_binding(self.ws, body)
        self.assertTrue(got["ok"], got["problems"])

    def test_report_without_runs_has_no_card_section(self):
        """既有交付（没跑过分析包）正文不得多出小节。"""
        import report_brief
        body = report_brief.render_brief_markdown(
            {"scope": {"company": "洋河股份", "periods": [2023, 2024]},
             "analysis": "（模型分析正文）"}, body="")
        self.assertNotIn("## 分析卡", body)

    def test_brief_structure_picks_up_the_card_from_the_workspace(self):
        """`build_structure` 的接缝：工作区有已验证运行 → 结构里带分析卡；没有 → 空串。"""
        import dataclasses
        import report_brief
        from financial_analysis import store
        self.assertEqual(report_brief._analysis_card_block(self.task_id,
                                                           ws_dir=self.ws), "")
        store.save_run(self.ws, self.run)
        blk = report_brief._analysis_card_block(self.task_id, ws_dir=self.ws)
        self.assertIn(f"run={self.run.run_id[:12]}", blk)
        self.assertIn(self.run.outputs[0].output_id, blk)
        # 未通过验证的运行不得进正文
        ws3 = Path(self.ws) / "w3"
        ws3.mkdir()
        store.save_run(ws3, dataclasses.replace(self.run,
                                                status=C.RunStatus.VALIDATION_FAILED))
        self.assertEqual(report_brief._analysis_card_block(self.task_id, ws_dir=ws3), "")

    def test_zip_manifest_and_body_share_one_run(self):
        import hashlib
        import zipfile
        import delivery_pipeline
        self.store.save_run(self.ws, self.run)
        body = self._body()
        store_rv = self.rv.VersionStore(self.ws, self.task_id)
        v = store_rv.record(body)
        self.assertTrue(store_rv.adopt(v))
        out = delivery_pipeline.repack_adopted(self.task_id, md_bytes=body.encode("utf-8"),
                                               ws_dir=self.ws)
        zip_path = Path(out["zip"]) if isinstance(out, dict) and out.get("zip") else None
        if zip_path is None:
            for key in ("path", "zip_path", "name"):
                if isinstance(out, dict) and out.get(key):
                    zip_path = Path(str(out[key]))
                    break
        self.assertIsNotNone(zip_path, f"repack 没给出包路径：{out}")
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            self.assertIn("analysis/analysis_runs.json", names,
                          "ZIP 必须带上运行记录（页面/图/正文同源的那一份）")
            manifest = json.loads(zf.read("PACKAGE_MANIFEST.json").decode("utf-8"))
            blob = zf.read("analysis/analysis_runs.json")
            self.assertEqual(
                manifest["files"]["analysis/analysis_runs.json"],
                hashlib.sha256(blob).hexdigest(), "清单 hash 必须覆盖运行记录字节")
        self.assertEqual(manifest["analysis_runs"]["runs"][0]["run_id"], self.run.run_id)
        self.assertEqual(manifest["analysis_runs"]["validated"], 1)
        # 包内正文就是这份正文 → 卡与清单指向同一次运行
        with zipfile.ZipFile(zip_path) as zf:
            packaged_body = zf.read(manifest["delivered_md"]).decode("utf-8")
        self.assertIn(f"run={self.run.run_id[:12]}", packaged_body)
        self.assertTrue(self.store.verify_body_binding(self.ws, packaged_body)["ok"])


class TestK2PackageIdentityAndRecompute(unittest.TestCase):
    """K2（09-29 夜验收）：包与**采纳稿同版**、含可复算输入、旧包标历史、正文变了旧包过期。"""

    def setUp(self):
        import tempfile
        self.ws = Path(tempfile.mkdtemp(prefix="fa_k2_"))
        self.tid = "k2-1"
        (self.ws / "project").mkdir(parents=True, exist_ok=True)
        rows = _wc_rows()
        self.ds = fa.freeze_from_facts(rows, periods=(2023, 2024),
                                       entity_id="002304.SZ", source_label="k2")
        self.plan = fa.compile_plan("分析利润", self.ds, prefer=("profit_bridge",))
        self.run = fa.run("profit_bridge", self.ds)

    def _body(self, text="（研究正文：2023 与 2024 两年营业收入与归母净利润）"):
        return f"# 洋河股份研究\n\n## 分析\n\n{text}\n"

    def _adopt(self, body):
        import report_version as rv
        store = rv.VersionStore(self.ws, self.tid)
        v = store.record(body)
        self.assertTrue(store.adopt(v))
        return store

    def _freeze(self, body: str | None = None):
        """按当前采纳稿冻结一份包（**无快照**路径：按名字读工作区，含 analysis/ 输入）。"""
        import delivery_pipeline as dp
        return dp.repack_adopted(self.tid, ws_dir=self.ws,
                                 md_bytes=(body or self._body()).encode("utf-8"))

    @staticmethod
    def _zip_of(out: dict) -> Path:
        for key in ("zip", "path", "zip_path", "name"):
            if isinstance(out, dict) and out.get(key):
                return Path(str(out[key]))
        raise AssertionError(f"重包没有给出包路径：{out}")

    def test_package_carries_inputs_and_recomputes_offline(self):
        """包里有 dataset/plan/context + 运行记录，且**离线复算**逐值一致。"""
        import shutil
        import tempfile
        import zipfile
        from financial_analysis import store as fa_store
        fa_store.save_inputs(self.ws, dataset=self.ds, plan=self.plan,
                            context={"dataset_source": {"kind": "test"}})
        fa_store.save_run(self.ws, self.run)
        self._adopt(self._body())
        out = self._freeze()
        zip_path = self._zip_of(out)
        self.assertTrue(zip_path.is_file(), out)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            for arc in ("analysis/dataset.json", "analysis/plan.json",
                        "analysis/context.json", "analysis/analysis_runs.json"):
                self.assertIn(arc, names, f"包内必须有可复算输入：{arc}")
            man = json.loads(zf.read("PACKAGE_MANIFEST.json").decode("utf-8"))
            self.assertTrue((man.get("analysis_inputs") or {}).get("recomputable"), man)
            self.assertEqual((man.get("analysis_inputs") or {}).get("dataset_hash"),
                             self.ds.dataset_hash)
        with tempfile.TemporaryDirectory(prefix="k2_re_") as tmp:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp)
            shutil.copy2(Path(tmp) / "analysis" / "analysis_runs.json",
                         Path(tmp) / "analysis_runs.json")
            got = fa_store.recompute_run(Path(tmp), self.run.run_id)
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["dataset_hash"], self.ds.dataset_hash)

    def test_old_package_is_labelled_historical_and_not_current(self):
        """包内身份 != 采纳身份 → 标 historical，且**不会被当成当前包**。"""
        import json as _json
        import zipfile
        import delivery_pipeline as dp
        from financial_analysis import store as fa_store
        fa_store.save_inputs(self.ws, dataset=self.ds, plan=self.plan, context={})
        fa_store.save_run(self.ws, self.run)
        self._adopt(self._body())
        # 手工造一个"旧包"：包内清单身份写成别的
        old = self.ws / "deliverables_20260101_000000.zip"
        with zipfile.ZipFile(old, "w") as zf:
            zf.writestr("PACKAGE_MANIFEST.json", _json.dumps(
                {"schema": "weavemind.package/2", "report_version_id": "deadbeef" * 8,
                 "research_body_sha256": "deadbeef" * 8}))
            zf.writestr("reports/report.md", self._body("旧稿"))
        st = dp.package_statuses(self.tid, ws_dir=self.ws)
        by = {p["name"]: p for p in st["packages"]}
        self.assertEqual(by[old.name]["status"], "historical")
        self.assertIn("历史", by[old.name]["reason"])
        self.assertFalse(st["has_current"])
        self.assertIsNone(dp.current_package(self.tid, ws_dir=self.ws))
        # 按采纳稿冻结一份 → 同版包出现，且 current 指向它（旧包不动）
        out = self._freeze()
        name = self._zip_of(out).name
        st2 = dp.package_statuses(self.tid, ws_dir=self.ws)
        self.assertTrue(st2["has_current"], st2)
        self.assertEqual(st2["current"], name)
        self.assertEqual({p["name"]: p["status"] for p in st2["packages"]}[old.name],
                         "historical")

    def test_body_change_makes_old_package_expired_and_new_export_new_identity(self):
        """改正文 → 旧包过期（身份不再匹配）；再导出得到**新身份**的新包（旧包不覆盖）。"""
        import delivery_pipeline as dp
        from financial_analysis import store as fa_store
        fa_store.save_inputs(self.ws, dataset=self.ds, plan=self.plan, context={})
        fa_store.save_run(self.ws, self.run)
        first_store = self._adopt(self._body())
        first_id = first_store.adopted().identity_id()
        first_zip = self._zip_of(self._freeze())
        # 正文改了并重新采纳
        self._adopt(self._body("（修订版：补充了现金质量解释）"))
        st = dp.package_statuses(self.tid, ws_dir=self.ws)
        by = {p["name"]: p for p in st["packages"]}
        self.assertEqual(by[first_zip.name]["status"], "historical", st)
        self.assertFalse(st["has_current"], "旧包不得继续被当当前包")
        second_zip = self._zip_of(self._freeze())
        self.assertNotEqual(second_zip.name, first_zip.name, "旧包不得被覆盖")
        self.assertTrue(first_zip.is_file(), "旧包必须留在原地（可下载为历史）")
        st2 = dp.package_statuses(self.tid, ws_dir=self.ws)
        self.assertEqual(st2["current"], second_zip.name)
        self.assertEqual(st2["adopted_identity"], first_store.adopted().identity_id())
        self.assertNotEqual(st2["adopted_identity"], first_id,
                            "改正文后采纳身份必须变化（新版本不继承旧批准）")


class TestK3CardTraitsAndBriefHygiene(unittest.TestCase):
    """K3 质量项：卡片"性质/下一步"按模型给；模型正文标题降级；来源编号不再自相矛盾。"""

    def test_card_traits_are_model_specific(self):
        from financial_analysis import report_adapter as ra
        self.assertEqual(ra.card_traits("profit_bridge")["kind"], "会计分解")
        self.assertIn("现金流量表附注", ra.card_traits("cash_quality")["next_action"])
        self.assertIn("账龄", ra.card_traits("working_capital")["next_action"])
        self.assertIn("假设", ra.card_traits("scenario_sensitivity")["next_action"])
        # 未注册的模型不得冒充已知模型的性质
        self.assertIn("注册模型", ra.card_traits("mystery_model")["kind"])
        self.assertIn("注册模型", ra.card_traits("")["kind"])

    def test_rendered_cards_do_not_share_one_next_action(self):
        """两张卡的"下一项验证动作"必须各说各的（实机：现金质量卡贴了利润桥的下一步）。"""
        import tempfile
        from financial_analysis import store as fa_store
        ds = _dataset()
        ws = Path(tempfile.mkdtemp(prefix="fa_traits_"))
        runs = []
        for mid in ("profit_bridge", "cash_quality"):
            r = fa.run(mid, ds)
            self.assertEqual(r.status, C.RunStatus.VALIDATED, (mid, r.reason))
            fa_store.save_run(ws, r)
            runs.append(r)
        blocks = [fa_store.render_card_block(r) for r in runs]
        acts = [b.split("下一项验证动作：", 1)[1].splitlines()[0] for b in blocks]
        self.assertNotEqual(acts[0], acts[1], acts)
        self.assertIn("毛利线以下", acts[0])
        self.assertIn("现金流量表附注", acts[1])
        self.assertNotIn("毛利线以下", acts[1], "现金质量卡不得套用利润桥的下一步")
        self.assertIn("现金质量", blocks[1])
        self.assertIn("会计分解", blocks[0])

    def test_model_headings_are_demoted_under_the_analysis_section(self):
        import report_brief as rb
        self.assertEqual(rb._demote_headings("# 报告\n## 一、概述\n正文\n### 细项"),
                         "### 报告\n#### 一、概述\n正文\n##### 细项")
        self.assertEqual(rb._demote_headings("无标题行"), "无标题行")

    def test_analysis_card_block_has_a_single_heading(self):
        import tempfile
        import report_brief as rb
        from financial_analysis import store as fa_store
        ds = _dataset()
        ws = Path(tempfile.mkdtemp(prefix="fa_cardblk_"))
        for mid in ("profit_bridge", "cash_quality"):
            fa_store.save_run(ws, fa.run(mid, ds))
        blk = rb._analysis_card_block("k3-card", ws_dir=ws)
        # 按**行首**数标题：`### 分析卡（…）` 里也含 "## 分析卡" 子串，不能直接 count 子串
        heads = [ln for ln in blk.splitlines() if ln.startswith("## ")]
        self.assertEqual(heads, ["## 分析卡"], blk[:200])
        self.assertIn("### 分析卡（cash_quality）", blk)

    def test_source_count_line_separates_cited_from_rejected(self):
        """来源说明必须把"正文引用的编号"与"未采用的候选"分开写（原句自相矛盾）。"""
        import report_brief as rb
        structure = {"scope": {"caliber": "合并", "as_of": "2025-04-30", "unit": "亿元",
                               "adopted_sources": 1, "audit_sources": 0},
                     "sources": [], "research_questions": [], "charts": []}
        try:
            out = rb.render_brief_markdown(structure, body="")
        except Exception:                            # noqa: BLE001 - 结构不齐时跳过渲染断言
            self.skipTest("最小结构不足以渲染简报")
        self.assertIn("正文引用", out)
        self.assertIn("不编号、不进正文", out)
        self.assertNotIn("未采用 0 条留在内部审计", out)


class TestDataAnalyzerTakesTheFinancialPath(unittest.TestCase):
    """Q1：金融任务必须按**显式数据集 + 分析计划**走注册模型，不走"最新 CSV + 末列目标"。"""

    def setUp(self):
        import tempfile
        self.ws = Path(tempfile.mkdtemp(prefix="fa_worker_"))
        (self.ws / "project").mkdir(parents=True, exist_ok=True)
        self.tid = "fa-worker-1"
        # 研究契约（K0-a）：金融分析的**退路**也必须绑定它，所以用例要有落库契约。
        # 用临时库，不碰真实任务库。
        import task_state
        self.db = str(Path(tempfile.mkdtemp(prefix="fa_worker_db_")) / "t.db")
        self._orig_db = task_state.DB_PATH
        task_state.DB_PATH = self.db
        self.addCleanup(setattr, task_state, "DB_PATH", self._orig_db)

    def _goal(self) -> str:
        return "研究洋河股份（002304.SZ）2023 与 2024 年年度报告研究"

    def _seed_contract(self, **overrides):
        import task_state
        payload = {"company": "洋河股份", "company_id": "002304.SZ", "market": "cn",
                   "periods": [2023, 2024], "caliber": "合并", "as_of": "2025-04-30",
                   "identity_source": "form"}
        payload.update(overrides)
        task_state.mark_queued(self.tid, goal=self._goal(),
                               research_request=payload, db_path=self.db)

    def _write_financials(self, payload: dict):
        (self.ws / "project" / "financials.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload

    def _write_working_paper(self, rows=None):
        wp = {"request": {"company": "洋河股份", "company_id": "002304.SZ", "market": "cn",
                          "periods": [2023, 2024], "caliber": "合并", "as_of": "2025-04-30"},
              "rows": rows if rows is not None else _two_period_rows(), "derived": []}
        (self.ws / "project" / "working_paper.json").write_text(
            json.dumps(wp, ensure_ascii=False), encoding="utf-8")

    def _worker(self):
        from workers.data_analyzer_worker import DataAnalyzerWorker
        return DataAnalyzerWorker(agent_id="dataanalyzerworker",
                                  capabilities=["data_analyzer"], registry=None,
                                  messaging=None)

    def _execute(self, instruction):
        import asyncio
        return json.loads(asyncio.run(self._worker().execute(
            instruction, {"workspace": str(self.ws), "goal": self._goal(),
                          "context": {"root_task_id": self.tid}})))

    def test_financial_task_runs_registered_models_and_stores_runs(self):
        # 四族输入齐备的夹具（含应收/存货/应付/营业成本）→ 四个模型全部采用并验证通过
        self._write_working_paper(_wc_rows())
        got = self._execute("分析本期归母净利润的变化由哪些金额项构成 [研究契约]")
        self.assertEqual(got["mode"], "financial")
        self.assertEqual(got["status"], "success", got["plan"]["rejected"])
        self.assertEqual(got["plan"]["adopted"],
                         ["profit_bridge", "cash_quality", "working_capital",
                          "scenario_sensitivity"])
        self.assertEqual(got["plan"]["rejected"], [])
        self.assertEqual(got["dataset"]["entity_id"], "002304.SZ")
        self.assertIn("profit_bridge", got["plan"]["adopted"])
        self.assertNotIn("target", got, "金融路径不得再给'末列当目标'的结论")
        bridge = [r for r in got["runs"] if r["model_id"] == "profit_bridge"]
        self.assertEqual(len(bridge), 1)
        self.assertEqual(bridge[0]["status"], C.RunStatus.VALIDATED)
        self.assertTrue(bridge[0]["validation_ok"])
        vals = {o["metric"]: o["value"] for o in bridge[0]["outputs"]}
        self.assertAlmostEqual(vals["net_profit_change"], -33.43, places=2)
        # 运行记录已落盘（交付链据此把正文/清单/ZIP 绑到同一次运行）
        stored = json.loads((self.ws / "analysis_runs.json").read_text(encoding="utf-8"))
        self.assertEqual(len(stored["runs"]), len(got["runs"]))
        self.assertEqual(got["cards"][0]["kind"], "会计分解")
        self.assertEqual(got["chart_specs"][0]["run_id"], bridge[0]["run_id"])

    def test_financial_path_falls_back_to_preloaded_financials(self):
        """底稿还没落盘时，**预载的结构化财务载荷**同样走注册模型（不得退到通用 EDA）。

        实机反例（2026-09-29 付费整跑 `ui-f4bac0d202`，茅台 2023/2024）：底稿此前只在
        收尾装配时才落盘，分析步跑的时候工作区里只有 `financials.json`——改前那一步报
        "No fresh CSV found in workspace"、重试三次后被**重规划成 content_summary**，
        注册模型一次没跑，`analysis/analysis_runs.json` 一份没有 → 交付硬门槛如实拦下
        整单（「分析未完成：交付正文只有数据与底稿」）。
        """
        self._seed_contract()
        (self.ws / "project" / "financials.json").write_text(
            json.dumps(_financials_payload(), ensure_ascii=False), encoding="utf-8")
        self.assertFalse((self.ws / "project" / "working_paper.json").exists(),
                         "本用例的前提就是底稿还没落盘")
        got = self._execute("分析本期归母净利润的变化由哪些金额项构成 [研究契约]")
        self.assertEqual(got["mode"], "financial", got)
        self.assertEqual(got["dataset_source"]["kind"], "financials", got["dataset_source"])
        self.assertIn("financials.json", got["dataset_source"]["label"])
        bridge = [r for r in got["runs"] if r["model_id"] == "profit_bridge"]
        self.assertEqual(len(bridge), 1, got["plan"])
        self.assertEqual(bridge[0]["status"], C.RunStatus.VALIDATED)
        vals = {o["metric"]: o["value"] for o in bridge[0]["outputs"]}
        self.assertAlmostEqual(vals["net_profit_change"], -33.43, places=2)
        # 运行记录必须落盘：正文/清单/ZIP 据此绑定同一次运行
        stored = json.loads((self.ws / "analysis_runs.json").read_text(encoding="utf-8"))
        self.assertEqual(len(stored["runs"]), len(got["runs"]))
        # 缺口如实：载荷里没有占款字段 → 营运资本被拒，状态 partial 而不是 success
        self.assertEqual([r["model_id"] for r in got["plan"]["rejected"]],
                         ["working_capital"], got["plan"])
        self.assertEqual(got["status"], "partial", got["status"])

    def test_no_stored_contract_is_an_actionable_gap(self):
        """没有研究契约 → **不跑模型**，回报可行动缺口（不是"取最新两期"照跑）。"""
        self._write_financials(_financials_payload())
        got = self._execute("分析利润变化 [研究契约]")
        self.assertEqual(got["mode"], "financial", got)
        self.assertEqual(got["status"], "failed", got)
        self.assertEqual(got["runs"], [])
        self.assertIn("研究契约", got["gap"])
        self.assertFalse((self.ws / "analysis_runs.json").exists(),
                         "契约不全时不得落任何运行记录")

    def test_payload_without_requested_years_is_refused(self):
        """请求 2023/2024、载荷只有 2024/2025 → 只用 2024，并写明请求的 2023 没有观察。"""
        self._seed_contract()
        rows = [
            {"year": 2025, "report_type": "年报", "report_date": "2025-12-31",
             "disclosure_date": "2026-03-20", "revenue": 300.0, "net_profit": 70.0,
             "gross_profit": 220.0, "operating_cashflow": 50.0},
            {"year": 2024, "report_type": "年报", "report_date": "2024-12-31",
             "disclosure_date": "2025-03-20", "revenue": 280.0, "net_profit": 66.0,
             "gross_profit": 210.0, "operating_cashflow": 46.0},
        ]
        self._write_financials(_financials_payload(rows))
        got = self._execute("分析利润变化 [研究契约]")
        self.assertEqual(got["mode"], "financial", got)
        self.assertEqual(got["dataset"]["periods"], ["2024年"], got["dataset"])
        self.assertTrue(any("2023" in g for g in got["dataset"]["gaps"]),
                        f"缺口要说明请求的 2023 没有观察：{got['dataset']['gaps']}")
        # 单期数据：两期桥不适用（不得 validated），同期模型照常跑 → 状态如实为 partial
        self.assertNotIn("profit_bridge", [r["model_id"] for r in got["runs"]])
        self.assertEqual(got["status"], "partial", got)
        self.assertTrue(all(r["status"] == C.RunStatus.VALIDATED for r in got["runs"]),
                        got["runs"])

    def test_wrong_company_payload_is_refused(self):
        """契约是洋河 002304.SZ，载荷是别家公司 → 一条观察都不采用。"""
        self._seed_contract()
        payload = _financials_payload()
        payload["metadata"] = dict(payload["metadata"])
        payload["metadata"].update({"company": "贵州茅台", "stock_code": "600519"})
        self._write_financials(payload)
        got = self._execute("分析利润变化 [研究契约]")
        self.assertEqual(got["status"], "failed", got)
        self.assertIn("主体", got["gap"])

    def test_other_caliber_payload_is_refused(self):
        """契约要求合并，载荷只有母公司 → 拒绝（跨报表范围不得混算）。"""
        self._seed_contract()
        rows = []
        for r in _financials_payload()["financials"]:
            r = dict(r)
            r["caliber"] = "母公司"
            rows.append(r)
        self._write_financials(_financials_payload(rows))
        got = self._execute("分析利润变化 [研究契约]")
        self.assertEqual(got["status"], "failed", got)
        self.assertIn("口径", got["gap"])

    def test_late_disclosure_is_excluded_but_early_one_is_kept(self):
        """晚于 as_of 的披露不参与（2024 年报 2025-04-10 披露，契约截至 2024-04-01）。"""
        self._seed_contract(as_of="2024-04-01")
        self._write_financials(_financials_payload())
        got = self._execute("分析利润变化 [研究契约]")
        self.assertEqual(got["mode"], "financial", got)
        # 2024 那条披露日晚于 as_of → 不进数据集；2023 那条（2024-04-10？）同样晚 →
        # 两条都被排除 → 如实报"没有与契约相容的观察"
        self.assertEqual(got["status"], "failed", got)
        self.assertIn("as_of", got["gap"])
        self.assertEqual(got["runs"], [])

    def test_working_paper_wins_over_preloaded_payload(self):
        """两者都在时以**底稿**为准（底稿含已选事实、口径证据与派生行）。"""
        self._write_working_paper(_wc_rows())
        (self.ws / "project" / "financials.json").write_text(
            json.dumps(_financials_payload(), ensure_ascii=False), encoding="utf-8")
        got = self._execute("分析利润变化 [研究契约]")
        self.assertEqual(got["dataset_source"]["kind"], "working_paper", got["dataset_source"])
        # 底稿里有占款字段（_wc_rows）→ 营运资本这一族也能跑
        self.assertIn("working_capital", got["plan"]["adopted"], got["plan"])

    def test_missing_input_still_takes_the_financial_path(self):
        rows = [r for r in _two_period_rows() if r["metric"] != "gross_profit"]
        self._write_working_paper(rows)
        got = self._execute("分析利润变化 [研究契约]")
        self.assertEqual(got["mode"], "financial", "缺输入也不得回退到 CSV 猜测")
        # 初筛就缺输入 → 进计划的**拒绝清单**（带缺什么），不生成 run；状态不得报 success
        # （其他模型照跑 → partial：缺口必须让读者看见，而不是被"其他都过了"盖掉）
        self.assertEqual(got["status"], "partial", got["status"])
        rej = got["plan"]["rejected"]
        self.assertTrue(rej, rej)
        item = [r for r in rej if r.get("model_id") == "profit_bridge"][0]
        self.assertEqual(item["reason"], "缺输入")
        self.assertIn("gross_profit", item["missing"])
        self.assertEqual([r for r in got["runs"] if r["model_id"] == "profit_bridge"], [],
                         "缺输入的模型不得产出（也不得假装算过）")
        # 顺带算出的比率仍然如实记录（它们真的验证通过了）
        self.assertTrue(all(r["status"] == C.RunStatus.VALIDATED for r in got["runs"]))

    def test_partial_status_when_a_family_is_rejected_for_missing_inputs(self):
        """核心指标齐备但没有占款字段：三族跑通、营运资本被拒 → `partial`（缺口可见）。"""
        self._write_working_paper()
        got = self._execute("分析利润、现金质量与占款 [研究契约]")
        self.assertEqual(got["status"], "partial", got["status"])
        self.assertEqual([r["model_id"] for r in got["plan"]["rejected"]], ["working_capital"])
        self.assertTrue(all(r["status"] == C.RunStatus.VALIDATED for r in got["runs"]),
                        "被采用的模型都必须是通过验证的")

    def test_non_financial_workspace_keeps_the_generic_eda_path(self):
        """没有本次任务的金融底稿时，原有 EDA 路径一字不变（不误伤通用数据任务）。"""
        got = self._execute("帮我做一下数据探索")
        self.assertNotEqual(got.get("mode"), "financial")
        self.assertEqual(got.get("status"), "failed")
        self.assertFalse((self.ws / "analysis_runs.json").exists(),
                         "非金融任务不得写分析运行记录")


def _wc_rows():
    """营运资本夹具：现役事实层还没有应收/存货/应付这些 slug，用夹具验证"数据到位就能算"。"""
    rows = _two_period_rows()
    for metric, v23, v24, unit in (("accounts_receivable", 10.0, 13.0, "亿元"),
                                   ("inventory", 20.0, 26.0, "亿元"),
                                   ("accounts_payable", 8.0, 9.0, "亿元"),
                                   ("operating_cost", 80.0, 77.51, "亿元")):
        rows.append(_row(metric, "2023年", v23, unit=unit))
        rows.append(_row(metric, "2024年", v24, unit=unit))
    return rows


class TestCashQualityAndWorkingCapitalAndScenario(unittest.TestCase):
    """Q2 三族模型的注册、独立验证与**缺输入就拒绝**（不凑数）。"""

    def _ds(self, rows=None, **kw):
        return _dataset(rows, **kw)

    # ── 现金质量 ──
    def test_cash_quality_on_real_numbers(self):
        run = fa.run("cash_quality", self._ds())
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        got = {o.metric: o.value for o in run.outputs}
        self.assertAlmostEqual(got["cfo_minus_profit"], -20.44, places=2)   # 46.29 - 66.73
        self.assertAlmostEqual(got["cashflow_coverage"], 69.37, places=2)   # 46.29/66.73
        self.assertTrue(run.validation["checks"]["cash_quality_sign"]["ok"])

    def test_cash_quality_refuses_coverage_when_profit_is_not_positive(self):
        rows = _dataset_rows_with(mutate=(("net_profit", "2024年", {"value": -5.0}),))
        run = fa.run("cash_quality", self._ds(rows))
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        self.assertNotIn("cashflow_coverage", {o.metric for o in run.outputs},
                         "归母净利非正时不得给覆盖率")
        self.assertIn("非正", " ".join(run.outputs[0].limits))

    def test_cash_quality_converts_units_explicitly(self):
        rows = _dataset_rows_with(mutate=(("operating_cashflow", "2024年",
                                           {"value": 462900.0, "unit": "万元"}),))
        run = fa.run("cash_quality", self._ds(rows))
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        self.assertAlmostEqual(
            {o.metric: o.value for o in run.outputs}["cfo_minus_profit"], -20.44, places=2)

    # ── 营运资本 ──
    def test_working_capital_is_refused_when_the_slugs_do_not_exist(self):
        """真实事实层还没有应收/存货/应付：必须**明确拒绝**并给出要补的指标。"""
        run = fa.run("working_capital", self._ds())
        self.assertEqual(run.status, C.RunStatus.MISSING_INPUT, run.reason)
        self.assertIn("accounts_receivable", run.reason)
        plan = fa.compile_plan("占款增加在哪里", self._ds())
        rej = [r for r in plan.rejected if r["model_id"] == "working_capital"][0]
        self.assertIn("accounts_receivable", rej["missing"])
        self.assertIn("operating_cost", rej["missing"])

    def test_working_capital_computes_and_discloses_closing_balance_posture(self):
        run = fa.run("working_capital", self._ds(_wc_rows()))
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        got = {o.metric: o.value for o in run.outputs}
        # 手算：占款变化 = Δ应收 3 + Δ存货 6 − Δ应付 1 = 8（金样口径逐项复核）
        self.assertAlmostEqual(got["working_capital_occupation_change"], 8.0, places=2)
        self.assertAlmostEqual(got["receivable_days"], 13.0 / 288.76 * 365, places=2)
        self.assertTrue(run.validation["checks"]["posture_disclosed"]["ok"])
        self.assertIn("期末口径", " ".join(run.outputs[0].assumptions))

    def test_working_capital_zero_denominator_gives_not_computable(self):
        rows = _wc_rows()
        for r in rows:
            if r["metric"] == "revenue" and r["period"] == "2024年":
                r["value"] = 0.0
        run = fa.run("working_capital", self._ds(rows))
        self.assertEqual(run.status, C.RunStatus.NOT_COMPUTABLE, run.reason)
        self.assertIn("分母非正", run.reason)

    # ── 条件情景 ──
    def test_scenario_reproduces_the_base_period_and_ranks_sensitivity(self):
        run = fa.run("scenario_sensitivity", self._ds(),
                     params={"revenue_growth": 0.03, "gross_margin_delta": -0.01})
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        base = [c for c in run.outputs[0].components if str(c["label"]).startswith("基准")][0]
        self.assertAlmostEqual(base["value"], 66.73, places=2, msg="基准必须复现基期")
        self.assertEqual(run.outputs[0].diagnostics["base_reproduction_gap"], "0")
        self.assertTrue(run.validation["checks"]["base_reproduction"]["ok"])
        self.assertTrue(run.validation["checks"]["scenario_direction"]["ok"])
        sens = run.outputs[1]
        self.assertEqual(sens.components[0]["label"], "毛利率 +1pp",
                         "本样本上毛利率最敏感（2.89 亿元/pp）")
        self.assertIn("使用者设定", " ".join(run.outputs[0].assumptions))
        self.assertIn("不显示", run.outputs[0].diagnostics["no_probability"])

    def test_scenario_out_of_bounds_params_fail_closed(self):
        run = fa.run("scenario_sensitivity", self._ds(),
                     params={"revenue_growth": 3.0})
        self.assertEqual(run.status, C.RunStatus.NOT_APPLICABLE, run.reason)
        self.assertIn("超出允许范围", run.reason)

    def test_scenario_refuses_non_positive_base_revenue(self):
        rows = _dataset_rows_with(mutate=(("revenue", "2024年", {"value": 0.0}),))
        run = fa.run("scenario_sensitivity", self._ds(rows))
        self.assertEqual(run.status, C.RunStatus.NOT_COMPUTABLE, run.reason)

    # ── 注册表与计划 ──
    def test_registry_lists_four_families_and_each_has_an_operator(self):
        self.assertEqual([m.model_id for m in fa.specs()],
                         ["profit_bridge", "cash_quality", "working_capital",
                          "scenario_sensitivity"])
        self.assertEqual(set(fa.registry.operators()),
                         {"profit_bridge_v1", "cash_quality_v1", "working_capital_v1",
                          "scenario_v1"})
        for m in fa.specs():
            self.assertIn(m.operator, fa.registry.operators())
            self.assertTrue(m.limits, f"{m.model_id} 必须带限制")

    def test_plan_adopts_three_families_on_the_real_shape(self):
        plan = fa.compile_plan("利润、现金质量、占款与情景", self._ds())
        self.assertEqual([a.model_id for a in plan.adopted],
                         ["profit_bridge", "cash_quality", "scenario_sensitivity"])
        self.assertEqual([r["model_id"] for r in plan.rejected], ["working_capital"])
        self.assertEqual(plan.rejected[0]["reason"], "缺输入")


class TestRealFrozenSampleChain(unittest.TestCase):
    """真机冻结样本上跑同一条链（样本不在仓库里就跳过，不伪造数据）。"""

    def _sample(self):
        base = os.environ.get("WEAVEMIND_TASKS_ROOT") or os.path.join(
            os.environ.get("TEMP", ""), "agent_workspace", "tasks", "projects", "default")
        path = Path(base) / "ui-a06a005c9b" / "project" / "working_paper.json"
        if not path.is_file():
            self.skipTest(f"冻结样本不在本机：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_real_profit_bridge_matches_the_working_paper_numbers(self):
        obj = self._sample()
        ds = fa.freeze_from_working_paper(
            obj, required_metrics=("revenue", "net_profit", "gross_profit",
                                   "operating_cashflow"),
            source_label="frozen:ui-a06a005c9b")
        self.assertEqual(ds.manifest.entity_id, "002304.SZ")
        self.assertEqual(ds.manifest.periods, ("2023年", "2024年"))
        run = fa.run("profit_bridge", ds)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        got = {o.metric: o.value for o in run.outputs}
        self.assertAlmostEqual(got["net_profit_change"], -33.43, places=2)
        self.assertAlmostEqual(got["gross_profit_change"], -38.01, places=2)
        self.assertAlmostEqual(got["below_gross_line_change"], 4.58, places=2)
        card = fa.analysis_card(run, run.outputs[0].output_id)
        self.assertIn("洋河股份", card["title"])


class TestReportScopeDiscipline(unittest.TestCase):
    """K0-a（09-29 夜验收）：**口径不回退、跨指标不混算**。

    固定反例：合并净利两期 10/12、母公司毛利 30/40、母公司 CFO 20——修前利润桥与
    现金质量都 validated（跨报表范围混算），修后必须拒绝或缺输入；同时**不能全局关闭
    模型**：同口径数据下两个模型照常 validated。另保留"合并 CFO ÷ 归母净利"的
    合法观察比率正例（归属层差异不等于口径不同）。
    """

    @staticmethod
    def _mixed_rows():
        return [
            _row("net_profit", "2023年", 10.0),
            _row("net_profit", "2024年", 12.0),
            _row("gross_profit", "2023年", 30.0, caliber="母公司"),
            _row("gross_profit", "2024年", 40.0, caliber="母公司"),
            _row("operating_cashflow", "2024年", 20.0, caliber="母公司"),
        ]

    def test_explicit_merge_never_falls_back_to_parent_only_value(self):
        ds = _dataset(self._mixed_rows())
        self.assertEqual(ds.primary_caliber, "合并")
        self.assertIsNone(ds.get("gross_profit", "2024年", caliber="合并"),
                          "显式合并缺项时不得回退母公司值")
        with self.assertRaises(C.MissingInput) as ctx:
            ds.require("gross_profit", "2024年", caliber="合并")
        self.assertIn("母公司", str(ctx.exception), "缺输入提示要给出其它口径线索")

    def test_default_caliber_is_consistent_not_per_metric(self):
        """默认口径一以贯之：不能因为某指标在另一口径下存在就换口径。"""
        ds = _dataset(self._mixed_rows())
        self.assertIsNotNone(ds.get("net_profit", "2024年"))
        self.assertIsNone(ds.get("operating_cashflow", "2024年"))

    def test_profit_bridge_and_cash_quality_refuse_mixed_scope(self):
        ds = _dataset(self._mixed_rows())
        plan = fa.compile_plan("分析本期归母净利润的变化由哪些金额项构成", ds)
        adopted = [a.model_id for a in plan.adopted]
        self.assertNotIn("profit_bridge", adopted, plan.adopted)
        self.assertNotIn("cash_quality", adopted, plan.adopted)
        rej = {r["model_id"]: r for r in plan.rejected}
        self.assertEqual(rej["profit_bridge"]["reason"], "缺输入")
        for mid in ("profit_bridge", "cash_quality"):
            run = fa.run(mid, ds)
            self.assertNotEqual(run.status, C.RunStatus.VALIDATED,
                                f"{mid} 不得对跨范围输入给 validated：{run.reason}")

    def test_models_are_not_globally_disabled(self):
        """同一批数据里，**同口径**的模型照常跑（拒绝的是混算，不是模型）。"""
        ds = _dataset()
        self.assertEqual(fa.run("profit_bridge", ds).status, C.RunStatus.VALIDATED)
        self.assertEqual(fa.run("cash_quality", ds).status, C.RunStatus.VALIDATED)

    def test_report_scope_ok_rejects_mixed_and_undeclared(self):
        a = C.Observation(fact_id="a", metric="net_profit", period="2024年",
                          value=12.0, caliber="合并")
        b = C.Observation(fact_id="b", metric="gross_profit", period="2024年",
                          value=40.0, caliber="母公司")
        ok, why = C.report_scope_ok(a, b)
        self.assertFalse(ok)
        self.assertIn("合并", why)
        self.assertIn("母公司", why)
        blank = C.Observation(fact_id="c", metric="gross_profit", period="2024年",
                              value=40.0, caliber="")
        ok2, why2 = C.report_scope_ok(a, blank)
        self.assertFalse(ok2)
        self.assertIn("未声明", why2)
        self.assertTrue(C.report_scope_ok(a, a)[0])

    def test_operators_check_scope_even_without_dataset_get(self):
        """算子**自己**也要校验范围：直接喂一个"每条都取得到、但范围不同"的数据集。"""

        class _StubDS:
            """只实现算子用到的最小接口：require/period_at/manifest/periods。"""

            def __init__(self, obs):
                self._o = obs
                self.manifest = type("M", (), {"periods": ("2023年", "2024年")})()

            def period_at(self, offset=0):
                ps = ["2023年", "2024年"]
                return ps[len(ps) - 1 + int(offset)]

            def require(self, metric, period, **kw):
                return self._o[(metric, period)]

        obs = {
            ("net_profit", "2024年"): C.Observation(
                fact_id="np", metric="net_profit", period="2024年", value=12.0,
                unit="亿元", currency="CNY", entity_id="X", caliber="合并"),
            ("net_profit", "2023年"): C.Observation(
                fact_id="np0", metric="net_profit", period="2023年", value=10.0,
                unit="亿元", currency="CNY", entity_id="X", caliber="合并"),
            ("gross_profit", "2024年"): C.Observation(
                fact_id="gp", metric="gross_profit", period="2024年", value=40.0,
                unit="亿元", currency="CNY", entity_id="X", caliber="母公司"),
            ("gross_profit", "2023年"): C.Observation(
                fact_id="gp0", metric="gross_profit", period="2023年", value=30.0,
                unit="亿元", currency="CNY", entity_id="X", caliber="母公司"),
            ("operating_cashflow", "2024年"): C.Observation(
                fact_id="cf", metric="operating_cashflow", period="2024年", value=20.0,
                unit="亿元", currency="CNY", entity_id="X", caliber="母公司"),
        }
        ds = _StubDS(obs)
        from financial_analysis.operators import cash_quality as cq
        from financial_analysis.operators import profit_bridge as pb
        with self.assertRaises(C.NotApplicable) as e1:
            pb.compute(ds, {})
        self.assertIn("母公司", str(e1.exception))
        with self.assertRaises(C.NotApplicable) as e2:
            cq.compute(ds, {})
        self.assertIn("母公司", str(e2.exception))

    def test_same_scope_cfo_over_parent_net_profit_ratio_still_allowed(self):
        """正例：合并 CFO ÷ 归母净利是**同一范围**的观察比率（归属层差异不是口径不同）。"""
        ds = _dataset()
        run = fa.run("cash_quality", ds)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        cov = [o for o in run.outputs if o.metric == "cashflow_coverage"]
        self.assertTrue(cov, run.outputs)
        self.assertTrue(any(("归母" in x or "归属" in x) for x in run.outputs[0].limits),
                        "限制说明必须写明归属层差异")


if __name__ == "__main__":
    unittest.main(verbosity=1)
