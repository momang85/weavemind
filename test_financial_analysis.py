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
import shutil
import unittest
from pathlib import Path
from unittest import mock

import financial_analysis as fa
import workspace as ws_mod
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
        self.assertIn("profit_to_cash_v1", fa.registry.operators())
        # 注册表是**唯一入口**：这份清单就是"能被计划采用的模型"的完整集合
        # （L3 新增 profit_to_cash；清单变化必须是**显式**的，不接受"多出来的自动通过"）
        self.assertEqual([m.model_id for m in fa.specs()],
                         ["profit_bridge", "operating_drivers", "cash_quality",
                          "working_capital", "scenario_sensitivity", "profit_to_cash"])


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
        # 各族输入齐备的夹具（含应收/存货/应付/营业成本）→ 注册模型全部采用并验证通过。
        # L2 起"数据齐备 ≠ 适用"：问题要点名各家族（原问题保留、不改写），
        # 所以这里用一句把四类问题都说到的研究问题。
        self._write_working_paper(_wc_rows())
        got = self._execute("分析利润变化归因、现金转化、营运资本周转与情景敏感性 [研究契约]")
        self.assertEqual(got["mode"], "financial")
        self.assertEqual(got["status"], "success", got["plan"]["rejected"])
        self.assertEqual(set(got["plan"]["adopted"]),
                         {"profit_bridge", "operating_drivers", "cash_quality",
                          "working_capital", "scenario_sensitivity", "profit_to_cash"},
                         got["plan"]["rejected"])
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
        # L2：问题要点名家族（否则只有被点到的模型进计划——那是**设计**，不是缺陷）
        got = self._execute("分析利润变化归因、现金转化、营运资本周转与情景敏感性 [研究契约]")
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
        # 缺口如实：载荷里没有占款字段（也没有营业成本）→ 营运资本与经营驱动被拒，
        # 状态 partial 而不是 success（U1 新增家族：注册后同一份载荷多一条"缺输入"）
        self.assertEqual(sorted(r["model_id"] for r in got["plan"]["rejected"]),
                         ["operating_drivers", "working_capital"], got["plan"])
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
        # L2：点名"现金质量/占款/情景"——单期数据下两期模型（利润桥、利润→现金）如实缺输入，
        # 同期模型（现金质量）与单期情景照常跑通 → partial（缺口可见）
        got = self._execute("分析现金质量、占款与情景 [研究契约]")
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
        got = self._execute("分析利润变化归因与营运资本周转 [研究契约]")
        self.assertEqual(got["dataset_source"]["kind"], "working_paper", got["dataset_source"])
        # 底稿里有占款字段（_wc_rows）→ 营运资本这一族也能跑
        self.assertIn("working_capital", got["plan"]["adopted"], got["plan"])

    def test_missing_input_still_takes_the_financial_path(self):
        rows = [r for r in _two_period_rows() if r["metric"] != "gross_profit"]
        self._write_working_paper(rows)
        # L2：把四类问题都点到——缺 gross_profit 的模型被拒（缺输入），
        # 不依赖它的模型照跑 → 部分完成，缺口必须被读者看见
        got = self._execute("分析利润变化归因、现金转化、营运资本周转与情景敏感性 [研究契约]")
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
        """核心指标齐备但没有占款字段：点到的家族跑通、营运资本被拒 → `partial`（缺口可见）。"""
        self._write_working_paper()
        got = self._execute("分析利润变化归因、现金质量与占款 [研究契约]")
        self.assertEqual(got["status"], "partial", got["status"])
        rejected = [r["model_id"] for r in got["plan"]["rejected"]]
        self.assertIn("working_capital", rejected, got["plan"]["rejected"])
        wc = [r for r in got["plan"]["rejected"] if r["model_id"] == "working_capital"][0]
        self.assertEqual(wc["reason"], "缺输入", wc)
        # 没被点到的问题类型（情景）如实拒在计划里，理由写明"与所问问题无关"（L2）
        self.assertIn("scenario_sensitivity", rejected, got["plan"]["rejected"])
        sc = [r for r in got["plan"]["rejected"]
              if r["model_id"] == "scenario_sensitivity"][0]
        self.assertEqual(sc["reason"], "与所问问题无关", sc)
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
    def test_registry_lists_six_families_and_each_has_an_operator(self):
        # U1（2026-10-01）：新增第六个家族 operating_drivers（经营驱动分解）。
        self.assertEqual([m.model_id for m in fa.specs()],
                         ["profit_bridge", "operating_drivers", "cash_quality",
                          "working_capital", "scenario_sensitivity", "profit_to_cash"])
        self.assertEqual(set(fa.registry.operators()),
                         {"profit_bridge_v1", "operating_drivers_v1", "cash_quality_v1",
                          "working_capital_v1", "scenario_v1", "profit_to_cash_v1"})
        for m in fa.specs():
            self.assertIn(m.operator, fa.registry.operators())
            self.assertTrue(m.limits, f"{m.model_id} 必须带限制")

    def test_plan_adopts_input_complete_families_on_the_real_shape(self):
        # L2：问题点名四类家族；输入齐备的被采用，缺字段的（经营驱动缺营业成本、
        # 营运资本缺占款）带原因被拒。采用**顺序**按"命中词多的类型优先"，故比集合不比顺序。
        plan = fa.compile_plan("利润变化归因、现金质量、营运资本周转与情景敏感性", self._ds())
        self.assertEqual({a.model_id for a in plan.adopted},
                         {"profit_bridge", "profit_to_cash", "cash_quality",
                          "scenario_sensitivity"})
        rej = {r["model_id"]: r for r in plan.rejected}
        self.assertEqual(set(rej), {"operating_drivers", "working_capital"})
        self.assertEqual(rej["operating_drivers"]["reason"], "缺输入")
        self.assertEqual(rej["operating_drivers"]["missing"], ["operating_cost"],
                         "经营驱动桥需要营业成本：缺它就拒，不拿毛利顶替")
        self.assertEqual(rej["working_capital"]["reason"], "缺输入")
        self.assertEqual(set(plan.question_types),
                         {"profit_attribution", "cash_conversion", "working_capital",
                          "scenario"})


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


class TestL0ATrustedInputOutputContract(unittest.TestCase):
    """L0-a（2026-09-30 架构复核 F1/F4/F5）：完整身份、血缘传播、独立验证覆盖面。"""

    def test_cross_company_cross_currency_inputs_are_not_applicable(self):
        """F1 反例：洋河/CNY 两期净利 + 茅台/USD 两期毛利 → **不适用**，不得 validated。

        原先跨指标只比报表范围与量纲，于是这个载荷跑出闭合的桥，且所有输出标成洋河/CNY。
        """
        rows = _two_period_rows()
        for r in rows:
            if r["metric"] == "gross_profit":
                r.update({"entity": "贵州茅台", "entity_id": "600519.SH",
                          "currency": "USD"})
        ds = _dataset(rows)
        run = fa.run("profit_bridge", ds)
        self.assertEqual(run.status, C.RunStatus.NOT_APPLICABLE, run.reason)
        self.assertIn("主体", run.reason)
        self.assertFalse(run.outputs, "不适用时不得留下任何标着洋河/CNY 的输出")

    def test_identity_check_is_shared_by_operators_and_validation(self):
        """判据是**共用的那一条**：算子入口与独立验证都调 `full_identity_ok`。"""
        a = C.Observation(fact_id="a", metric="revenue", period="2024年", value=1.0,
                          unit="亿元", currency="CNY", entity_id="X", caliber="合并")
        b = C.Observation(fact_id="b", metric="gross_profit", period="2024年", value=1.0,
                          unit="亿元", currency="USD", entity_id="X", caliber="合并")
        ok, why = C.full_identity_ok(a, b)
        self.assertFalse(ok)
        self.assertIn("币种", why)
        self.assertTrue(C.full_identity_ok(a, a)[0])
        c = C.Observation(fact_id="c", metric="revenue", period="2024年", value=1.0,
                          unit="天", currency="CNY", entity_id="X", caliber="合并")
        self.assertIn("金额", C.full_identity_ok(a, c)[1])

    def test_lineage_conflict_propagates_to_derived_observation(self):
        """F4 反例：父观察冲突 → 派生观察同样不可用（不能留成一个 ok 的算出数）。"""
        rows = [
            _row("revenue", "2024年", 100.0, fact_id="f-rev-cny"),
            _row("revenue", "2024年", 100.0, fact_id="f-rev-usd", currency="USD"),
            _row("operating_cost", "2024年", 60.0, fact_id="f-cost"),
            _row("gross_profit", "2024年", 40.0, fact_id="f-gp",
                 derived_from=("f-rev-cny", "f-cost"), formula_version="gross_profit_v1"),
        ]
        ds = fa.freeze_from_facts(rows, periods=(2024,), source_label="l0a")
        gp = next(o for o in ds.observations if o.fact_id == "f-gp")
        self.assertFalse(gp.usable, "父观察不可用时派生观察不得可用")
        self.assertIn("血缘", gp.note)
        self.assertTrue(any("血缘传播" in c for c in ds.manifest.conflicts),
                        ds.manifest.conflicts)

    def test_component_tamper_on_any_output_is_caught(self):
        """F5：篡改**任一**输出的分项（±999999）必须被独立验证发现。"""
        ds = _dataset()
        payload = _payload_of("profit_bridge", ds)
        payload["outputs"][0]["components"][0]["value"] = 999999.0
        payload["outputs"][0]["components"][1]["value"] = -999999.0
        res = fa.validation.validate_output(fa.registry.spec("profit_bridge"), ds, payload)
        self.assertFalse(res["ok"], res)
        self.assertIn("identity", res["failed"])

    def test_output_period_outside_the_dataset_is_caught(self):
        """F5：把 output_period 改成 2030 年（数据集里没有）必须失败。"""
        ds = _dataset()
        payload = _payload_of("profit_bridge", ds)
        payload["outputs"][0]["output_period"] = "2030年较2029年"
        res = fa.validation.validate_output(fa.registry.spec("profit_bridge"), ds, payload)
        self.assertFalse(res["ok"], res)
        self.assertIn("output_shape", res["failed"])

    def test_declared_identity_contradicting_inputs_is_caught(self):
        """F5：诊断里声明主体/币种为别的公司/币种必须失败（输入绑定复核）。"""
        ds = _dataset()
        payload = _payload_of("profit_bridge", ds)
        payload["diagnostics"]["entity_id"] = "WRONG.SH"
        payload["diagnostics"]["currency"] = "USD"
        res = fa.validation.validate_output(fa.registry.spec("profit_bridge"), ds, payload)
        self.assertFalse(res["ok"], res)
        self.assertIn("binding", res["failed"])

    def test_legit_payloads_still_pass_the_new_checks(self):
        """正例：合法数据 + 合法载荷在新规则下仍通过（不靠收紧到什么都过不了）。"""
        ds = _dataset()
        for model_id in ("profit_bridge", "cash_quality", "scenario_sensitivity"):
            payload = _payload_of(model_id, ds)
            res = fa.validation.validate_output(fa.registry.spec(model_id), ds, payload)
            self.assertTrue(res["ok"], f"{model_id}: {res}")

    def test_rules_version_is_recorded_and_old_rules_need_recompute(self):
        """验证规则版本进 run 身份：规则变了，旧 run 不得冒充"按新规则已验证"。"""
        ds = _dataset()
        run = fa.run("profit_bridge", ds)
        self.assertEqual(run.rules_version, fa.validation.RULES_VERSION)
        state, why = fa.revalidate(run, ds)
        self.assertEqual(state, "ok", why)
        stale = fa.contracts.ModelRun(**{**run.__dict__, "rules_version": "validation/0.9.0"})
        state2, why2 = fa.revalidate(stale, ds)
        self.assertEqual(state2, "rules_changed", why2)
        self.assertIn("规则", why2)


class TestR1BComponentwiseValidation(unittest.TestCase):
    """R1-b（09-30 下午复核）：**合计相等不算验证通过**，分项要逐项独立核对。

    反例（复核原文）：利润桥两个分项 +10/−10、情景的基准 +10/使用者情景 −10——合计一模
    一样，旧验证（金样只核主输出、`identity` 只核合计）全部通过。
    """

    def test_profit_bridge_offset_cancellation_is_refused(self):
        ds = _dataset()
        payload = _payload_of("profit_bridge", ds)
        comps = payload["outputs"][0]["components"]
        comps[0]["value"] = float(comps[0]["value"]) + 10.0
        comps[1]["value"] = float(comps[1]["value"]) - 10.0      # 合计不变
        res = fa.validation.validate_output(fa.registry.spec("profit_bridge"), ds, payload)
        self.assertFalse(res["ok"], res)
        self.assertIn("components", res["failed"])
        self.assertIn("gross_profit_change", res["checks"]["components"]["detail"])

    def test_scenario_base_no_longer_trusts_self_reported_gap(self):
        """情景基准必须直接对**真实基期读数**：改基准 +10、使用者情景 −10 → 失败。"""
        import financial_analysis.operators.scenario as SC
        ds = _dataset(_wc_rows())
        payload = SC.compute(ds, {})
        comps = payload["outputs"][0]["components"]
        comps[0]["value"] = float(comps[0]["value"]) + 10.0       # base 篡改
        comps[1]["value"] = float(comps[1]["value"]) - 10.0       # user 反向抵消
        res = fa.validation.validate_output(fa.registry.spec("scenario_sensitivity"), ds,
                                           payload)
        self.assertFalse(res["ok"], res)
        detail = res["checks"]["components"]["detail"]
        self.assertIn("base", detail)
        self.assertIn("user", detail)

    def test_swap_missing_and_unknown_component_ids_are_refused(self):
        ds = _dataset()
        spec = fa.registry.spec("profit_bridge")
        base = _payload_of("profit_bridge", ds)
        # ① 两个分项的值互换（合计仍然不变，但每个 id 都错了）
        swapped = json.loads(json.dumps(base))
        c = swapped["outputs"][0]["components"]
        c[0]["value"], c[1]["value"] = c[1]["value"], c[0]["value"]
        res = fa.validation.validate_output(spec, ds, swapped)
        self.assertFalse(res["ok"], res)
        self.assertIn("components", res["failed"])
        # ② 缺 component_id
        noid = json.loads(json.dumps(base))
        noid["outputs"][0]["components"][0].pop("component_id")
        res = fa.validation.validate_output(spec, ds, noid)
        self.assertFalse(res["ok"], res)
        self.assertIn("component_id", res["checks"]["components"]["detail"])
        # ③ 重复 id（把第二项也标成第一项的 id）
        dup = json.loads(json.dumps(base))
        dc = dup["outputs"][0]["components"]
        dc[1]["component_id"] = dc[0]["component_id"]
        res = fa.validation.validate_output(spec, ds, dup)
        self.assertFalse(res["ok"], res)
        self.assertIn("components", res["failed"])
        # ④ 少一项
        missing = json.loads(json.dumps(base))
        missing["outputs"][0]["components"] = missing["outputs"][0]["components"][:1]
        res = fa.validation.validate_output(spec, ds, missing)
        self.assertFalse(res["ok"], res)

    def test_declared_component_ids_have_an_independent_path(self):
        """契约完整性：声明了 component_ids 的模型必须有 `components_gold` 且 id 一致。"""
        ds = _dataset(_wc_rows())
        declared = {m.model_id: m.component_ids for m in fa.specs() if m.component_ids}
        self.assertTrue(declared, "至少利润桥与情景要声明分项")
        for model_id, ids in declared.items():
            spec = fa.registry.spec(model_id)
            mod = fa.registry.OPERATORS[spec.operator][0]
            fn = getattr(mod, "components_gold", None)
            self.assertTrue(callable(fn), f"{model_id} 缺 components_gold")
            got = fn(ds, {})
            self.assertEqual(set(got), set(ids), f"{model_id} 独立计算的输出集合不符")
            for metric, want in ids.items():
                self.assertEqual(set(got.get(metric) or {}), set(want),
                                 f"{model_id}.{metric} 的 component_id 与声明不一致")

    def test_legit_component_payloads_still_pass(self):
        ds = _dataset(_wc_rows())
        for model_id in ("profit_bridge", "scenario_sensitivity"):
            payload = _payload_of(model_id, ds)
            res = fa.validation.validate_output(fa.registry.spec(model_id), ds, payload)
            self.assertTrue(res["ok"], f"{model_id}: {res}")


class TestR3IntentPerSubquestion(unittest.TestCase):
    """R3（09-30 下午复核）：计划不能拿"关键词联合"代替任务意图。

    反例一：`"预测明年的经营现金流"` 同时命中 forecast+cash → 采用 `profit_to_cash`/`cash_quality`，
    计划说明却写"不采用任何注册模型"（**自相矛盾**：历史结论被当成预测的答案）。
    反例二：`"只分析营业收入增减原因"` 未命中规则 → 自动跑四个模型（拿无关探索当回答）。
    """

    def test_forecast_only_with_indicator_words_adopts_nothing(self):
        for q in ("预测明年的经营现金流", "预测 2025 年营业收入和净利润",
                  "明年收入超过 300 亿的概率是多少"):
            with self.subTest(question=q):
                plan = fa.compile_plan(q, _dataset(_wc_rows()))
                self.assertEqual([a.model_id for a in plan.adopted], [],
                                 f"纯预测请求不得启动历史模型：{q}")
                self.assertTrue(plan.subquestions, plan.notes)
                self.assertTrue(all(s["kind"] == "forecast" for s in plan.subquestions),
                                plan.subquestions)
                self.assertTrue(all(not s["answered"] for s in plan.subquestions))
                self.assertTrue(any("未回答" in n for n in plan.notes), plan.notes)
                self.assertTrue(any("统计预测门槛与禁用清单" in n for n in plan.notes),
                                plan.notes)

    def test_mixed_request_answers_history_and_leaves_forecast_unanswered(self):
        """混合请求：历史子问题照答，**预测子问题保持未回答**（各自一条子问题状态）。"""
        plan = fa.compile_plan("分析 2024 年利润有没有转成现金，并预测 2025 年的经营现金流",
                               _dataset(_wc_rows()))
        kinds = {s["kind"]: s for s in plan.subquestions}
        self.assertIn("forecast", kinds, plan.subquestions)
        self.assertIn("history", kinds, plan.subquestions)
        self.assertFalse(kinds["forecast"]["answered"], kinds["forecast"])
        self.assertTrue(kinds["history"]["answered"], kinds["history"])
        self.assertTrue(plan.adopted, "历史子问题应当照常回答")
        self.assertTrue(all(a.model_id in kinds["history"]["models"]
                            for a in plan.adopted), plan.adopted)

    def test_notes_never_contradict_the_actual_plan(self):
        """说明与实际计划同源：采用了模型就不得写"没有任何模型被采用"。"""
        q = "分析 2024 年利润有没有转成现金，并预测 2025 年的经营现金流"
        plan = fa.compile_plan(q, _dataset(_wc_rows()))
        joined = " ".join(plan.notes)
        self.assertTrue(plan.adopted)
        self.assertNotIn("没有任何模型被采用", joined, joined)
        self.assertNotIn("不采用任何注册模型、不出预测数", joined, joined)

    def test_forecast_marker_in_a_clause_does_not_start_history_models_for_it(self):
        """同一子句里出现预测意图 → 该子句不拿同子句的指标词去启动历史模型。"""
        plan = fa.compile_plan("预测明年经营活动现金流净额", _dataset(_wc_rows()))
        self.assertEqual([a.model_id for a in plan.adopted], [], plan.adopted)
        self.assertEqual([s["kind"] for s in plan.subquestions], ["forecast"])

    def test_scenario_is_not_mistaken_for_a_statistical_forecast(self):
        """显式未来**条件假设**（情景）不等于统计预测：情景模型照常回答。"""
        plan = fa.compile_plan("按收入 +10%、毛利率 +2pp 做个情景", _dataset(_wc_rows()))
        kinds = [s["kind"] for s in plan.subquestions]
        self.assertIn("scenario", kinds, plan.subquestions)
        self.assertNotIn("forecast", kinds, plan.subquestions)
        self.assertIn("scenario_sensitivity", [a.model_id for a in plan.adopted],
                      plan.adopted)


class TestL0CScenarioSpeaksFromItsOwnParams(unittest.TestCase):
    """L0-c（复核 M1）：数值与解释同一份参数；图型按声明的结构选。"""

    def test_labels_and_formulas_use_the_same_resolved_params(self):
        ds = _dataset()
        run = fa.run("scenario_sensitivity", ds,
                     params={"revenue_growth": 0.10, "gross_margin_delta": 0.02,
                             "expense_change_ratio": 0.10})
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        main = run.outputs[0]
        comps = {c["label"]: c for c in main.components}
        up = [c for c in main.components if "使用者情景" in c["label"]]
        self.assertTrue(up, main.components)
        self.assertIn("+10.00%", up[0]["label"], "标签必须写使用者给的实际收入增速")
        self.assertIn("+2.00pp", up[0]["label"], f"毛利率要按百分点写：{up[0]['label']}")
        self.assertNotIn("+5%", " ".join(comps), "不得再出现写死的默认 +5%/+1pp")
        self.assertNotIn("+1pp", " ".join(comps))
        self.assertIn("0.1000", up[0]["formula"])

    def test_negative_growth_is_not_called_up_side(self):
        ds = _dataset()
        run = fa.run("scenario_sensitivity", ds, params={"revenue_growth": -0.08})
        up = [c for c in run.outputs[0].components if "使用者情景" in c["label"]][0]
        self.assertIn("-8.00%", up["label"])
        self.assertNotIn("上行", up["label"], "负增速不得固定称上行")
        self.assertNotIn("下行", up["label"], "使用者情景不叫下行")
        self.assertIn("使用者情景", run.outputs[0].output_period)

    def test_below_gross_block_is_not_called_pure_expense(self):
        ds = _dataset()
        run = fa.run("scenario_sensitivity", ds)
        joined = " ".join(run.outputs[0].assumptions) + " " + run.outputs[0].formula
        self.assertIn("隐含块", joined)
        self.assertTrue("含费用" in joined or "不是纯费用" in joined, joined)

    def test_chart_kind_follows_declared_structure(self):
        """并行情景不是加总贡献桥：不得再被画成瀑布。"""
        from financial_analysis import report_adapter as ra
        ds = _dataset()
        scen = fa.run("scenario_sensitivity", ds)
        spec_chart = ra.chart_spec(scen, scen.outputs[0].output_id)
        self.assertEqual(spec_chart["kind"], "bar_grouped", spec_chart)
        sens = ra.chart_spec(scen, scen.outputs[1].output_id)
        self.assertEqual(sens["kind"], "bar_sorted", sens)
        bridge = fa.run("profit_bridge", ds)
        self.assertEqual(ra.chart_spec(bridge, bridge.outputs[0].output_id)["kind"],
                         "waterfall")


class TestL3ProfitToCash(unittest.TestCase):
    """L3（09-30 深化）：利润→现金转化链——闭合的金额分解 + 未解释差额 + 可信的比率。

    这一族回答"本期利润增长有没有转成现金、哪些因素还没被解释"：金额分解必须**闭合**，
    比率只在**两期归母净利都为正**时给（负/零分母没有可比含义），并且**不把**缺口说成
    已解释（模型手里没有现金流量表调节表）。
    """

    def test_chain_validates_and_the_bridge_closes(self):
        ds = _dataset()
        run = fa.run("profit_to_cash", ds)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        checks = run.validation["checks"]
        for name in ("gold", "identity", "unit", "output_shape", "binding",
                     "cash_conversion_sign"):
            self.assertTrue(checks[name]["ok"], f"{name}: {checks[name]['detail']}")
        vals = {o.metric: o.value for o in run.outputs}
        # 两期样例：净利 100.16 → 66.73（-33.43）、毛利 249.26 → 211.25（-38.01）
        self.assertAlmostEqual(vals["profit_change"], -33.43, places=2)
        self.assertAlmostEqual(vals["cash_profit_gap_change"],
                               (46.29 - 61.30) - (66.73 - 100.16), places=2)
        main = [o for o in run.outputs if o.metric == "profit_change"][0]
        self.assertEqual(main.residual, 0.0)
        comps = {c["label"][:4]: c["value"] for c in main.components}
        self.assertAlmostEqual(sum(c["value"] for c in main.components),
                               main.value, places=2)
        self.assertTrue(any("毛利端变化" in c["label"] for c in main.components), comps)

    def test_conversion_change_is_given_when_both_periods_are_profitable(self):
        ds = _dataset()
        run = fa.run("profit_to_cash", ds)
        conv = [o for o in run.outputs if o.metric == "cash_conversion_change"]
        self.assertTrue(conv, run.outputs)
        self.assertEqual(conv[0].unit, "%")
        self.assertAlmostEqual(conv[0].value,
                               (46.29 / 66.73 - 61.30 / 100.16) * 100, places=2)

    def test_negative_profit_period_refuses_the_ratio_but_keeps_the_amounts(self):
        """负分母不给比率（既有裁决）：金额差额照给，比率不给并写清哪一期非正。"""
        rows = _dataset_rows_with(mutate=(("net_profit", "2024年", {"value": -5.0}),))
        ds = _dataset(rows)
        run = fa.run("profit_to_cash", ds)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        outs = {o.metric for o in run.outputs}
        self.assertIn("cash_profit_gap_change", outs)
        self.assertNotIn("cash_conversion_change", outs, "负分母不得给比率")
        joined = " ".join(run.outputs[0].assumptions) + " " + run.outputs[0].formula
        self.assertIn("现金转化", joined)
        self.assertTrue(any("负" in x or "非正" in x for x in run.outputs[0].limits), run.outputs[0].limits)

    def test_cross_entity_or_currency_inputs_are_not_applicable(self):
        rows = _dataset_rows_with(mutate=(("operating_cashflow", "2024年",
                                           {"entity_id": "600519.SH", "currency": "USD"}),))
        ds = _dataset(rows)
        run = fa.run("profit_to_cash", ds)
        self.assertEqual(run.status, C.RunStatus.NOT_APPLICABLE, run.reason)

    def test_tampered_period_or_ratio_unit_is_caught(self):
        """篡改：输出期间写成数据集外的年份、或把百分点的单位改成金额 → validation_failed。"""
        ds = _dataset()
        payload = _payload_of("profit_to_cash", ds)
        bad = json.loads(json.dumps(payload))
        bad["outputs"][0]["output_period"] = "2030年较2029年"
        res = fa.validation.validate_output(fa.registry.spec("profit_to_cash"), ds, bad)
        self.assertFalse(res["ok"], res)
        self.assertIn("output_shape", res["failed"])
        bad2 = json.loads(json.dumps(payload))
        for o in bad2["outputs"]:
            if o["metric"] == "cash_conversion_change":
                o["unit"] = "亿元"
        res2 = fa.validation.validate_output(fa.registry.spec("profit_to_cash"), ds, bad2)
        self.assertFalse(res2["ok"], res2)
        self.assertIn("output_shape", res2["failed"])
        # 比率被删掉却仍声称"可算" → 签名检查失败（不是只看模型自报字段）
        bad3 = json.loads(json.dumps(payload))
        bad3["outputs"] = [o for o in bad3["outputs"]
                           if o["metric"] != "cash_conversion_change"]
        res3 = fa.validation.validate_output(fa.registry.spec("profit_to_cash"), ds, bad3)
        self.assertFalse(res3["ok"], res3)
        self.assertIn("cash_conversion_sign", res3["failed"])

    def test_no_probability_or_forecast_claims(self):
        ds = _dataset()
        run = fa.run("profit_to_cash", ds)
        joined = " ".join(run.outputs[0].limits) + " ".join(run.outputs[0].assumptions)
        self.assertIn("零概率零预测", joined)
        self.assertNotIn("置信区间", " ".join(run.outputs[0].assumptions))


class TestL2QuestionDrivenSelection(unittest.TestCase):
    """L2（09-30 深化）：**问题参与模型选择**——数据齐备不等于适用（复核 M2 反例）。

    M2 的反例：同一份数据下，"只研究现金转换，不做情景预测"与"只分析营收变动的量价因素"
    此前选出**完全相同**的三个模型（输入齐备性驱动）。现在问题先映射到问题类型，
    只有相关模型进计划，无关模型带理由被拒，缺料如实列缺口而不换跑无关模型。
    """

    def test_cash_only_question_does_not_pull_in_the_scenario_model(self):
        plan = fa.compile_plan("只研究现金转换，不做情景预测", _dataset())
        self.assertEqual({a.model_id for a in plan.adopted},
                         {"profit_to_cash", "cash_quality"}, plan.rejected)
        self.assertNotIn("scenario_sensitivity", [a.model_id for a in plan.adopted],
                         "问的是现金转化：不得顺手跑情景")
        rej = {r["model_id"]: r for r in plan.rejected}
        self.assertEqual(rej["scenario_sensitivity"]["reason"], "与所问问题无关", rej)
        self.assertIn("利润变化归因", rej["profit_bridge"]["detail"])
        # 否定语境不被当成"在研究情景"：命中词里只有现金转换
        self.assertEqual(list(plan.question_types), ["cash_conversion"], plan.question_types)

    def test_volume_price_question_reports_gaps_and_runs_nothing_irrelevant(self):
        # U1（2026-10-01）规格变化：量价分解**已有注册模型**（operating_drivers）。
        # 本例数据集缺营业成本 → 相关模型带"缺输入"被拒，仍**不采用任何无关模型**，
        # 缺口与说明必须点名缺哪张表（旧断言写死"没有注册模型"，注册之后就是错话）。
        plan = fa.compile_plan("只分析营收变动的量价因素", _dataset())
        self.assertEqual(list(plan.question_types), ["volume_price"])
        self.assertEqual([a.model_id for a in plan.adopted], [],
                         "输入不齐时不采用任何模型（更不拿无关模型充数）")
        rej = {r["model_id"]: r for r in plan.rejected}
        self.assertEqual(rej["operating_drivers"]["reason"], "缺输入")
        self.assertEqual(rej["operating_drivers"]["missing"], ["operating_cost"])
        for model_id in ("profit_bridge", "cash_quality", "scenario_sensitivity",
                         "profit_to_cash"):
            self.assertEqual(rej[model_id]["reason"], "与所问问题无关", model_id)
        self.assertTrue(any("sales_volume" in g for g in plan.gaps), plan.gaps)
        self.assertTrue(any("输入不齐" in n and "operating_drivers" in n
                            for n in plan.notes), plan.notes)
        self.assertFalse(any("没有注册模型" in n for n in plan.notes),
                         "已有注册模型时不得再写“没有注册模型”")

    def test_plan_binds_question_types_and_outputs(self):
        plan = fa.compile_plan("分析利润变化归因", _dataset())
        self.assertEqual(plan.question_types, ("profit_attribution",))
        self.assertEqual(plan.question_type_labels, ("利润变化归因",))
        item = plan.adopted[0]
        self.assertEqual(item.model_id, "profit_bridge")
        self.assertIn("net_profit_change", item.outputs, "计划必须绑定这次跑出什么")
        self.assertEqual(item.question_types, ("profit_attribution",))
        self.assertTrue(any("输入齐备" in item.reason for item in plan.adopted))
        # 计划整体可序列化（进 analysis/plan.json 与包内清单）
        blob = plan.as_dict()
        self.assertEqual(blob["question_types"], ["profit_attribution"])
        self.assertIn("outputs", blob["adopted"][0])

    def test_unclassified_question_gets_a_restricted_plan_not_a_guess(self):
        """R3（09-30 下午复核）：问题没被规则化 → **受限计划**：不自动采入任何模型，
        只把可用模型列为"可选探索"（旧行为是"按输入齐备性照跑四个模型"，等于拿无关
        探索结果当答案）。"""
        plan = fa.compile_plan("随便看看这份数据", _dataset())
        self.assertEqual(plan.question_types, ())
        self.assertEqual([a.model_id for a in plan.adopted], [],
                         "未规则化的问题不得自动采入模型")
        self.assertTrue(plan.exploratory, "可选探索模型要列出来（但不进正文）")
        self.assertTrue(any("待澄清" in n or "未命中" in n for n in plan.notes), plan.notes)
        self.assertTrue(any("探索" in n for n in plan.notes), plan.notes)

    def test_model_specs_declare_which_questions_they_answer(self):
        for m in fa.specs():
            self.assertTrue(m.question_types, f"{m.model_id} 必须声明能回答的问题类型")
        from financial_analysis import questions as q
        for m in fa.specs():
            for qid in m.question_types:
                self.assertIn(qid, {t.qid for t in q.QUESTION_TYPES}, f"{m.model_id}/{qid}")


class TestQ4ForecastStaysClosed(unittest.TestCase):
    """Q4（09-30 深化）：**统计预测未开放**——预测类问题不得拿到"不回答它"的模型输出。

    实测反例（修前读数）：问"预测洋河股份2025年营业收入和净利润"，计划未命中任何类型 →
    退回"按输入齐备性选模型" → 采用全部 5 个模型（利润桥/现金质量/营运资金/情景/利润—现金），
    读者会以为这些输出就是对"预测"的回答。现在预测类问题是**显式的问题类型**且无注册模型：
    零采用 + 写明"不开放" + 门槛与禁用清单入口。
    """

    FORECAST_QUESTIONS = (
        "预测洋河股份2025年营业收入和净利润",
        "给出未来三年收入增长趋势并做回归",
        "明年收入超过300亿的概率是多少",
        "给出目标价与估值区间",
    )

    def test_forecast_questions_adopt_no_model(self):
        for q in self.FORECAST_QUESTIONS:
            with self.subTest(question=q):
                plan = fa.compile_plan(q, _dataset())
                self.assertIn("forecast_trend", plan.question_types, plan.question_types)
                self.assertEqual([a.model_id for a in plan.adopted], [],
                                 "预测未开放：不得用无关模型充当回答")
                self.assertTrue(all(r["reason"] == "与所问问题无关" for r in plan.rejected),
                                plan.rejected)
                self.assertTrue(any("不开放" in n for n in plan.notes), plan.notes)
                self.assertTrue(any("统计预测门槛与禁用清单" in n for n in plan.notes),
                                "必须给出可行动的入口，而不是一句「不支持」")

    def test_forecast_type_declares_no_model_and_a_gate(self):
        from financial_analysis import questions as q
        qt = q.question_type("forecast_trend")
        self.assertEqual(qt.models, (), "未开放的类型不得绑定任何注册模型")
        self.assertIn("5 个连续年度", qt.needs_note)
        self.assertIn("不提供预测", qt.needs_note)

    def test_negated_forecast_is_not_a_hit(self):
        """「不做情景预测」仍只选现金类模型（否定语境不算命中）。"""
        plan = fa.compile_plan("只研究现金转换，不做情景预测", _dataset())
        self.assertNotIn("forecast_trend", plan.question_types, plan.question_types)
        self.assertEqual({a.model_id for a in plan.adopted},
                         {"profit_to_cash", "cash_quality"}, plan.rejected)

    def test_history_question_still_works(self):
        """对照：描述历史的归因问题不受影响（"利润变化趋势"不是预测命中词）。"""
        plan = fa.compile_plan("分析2024年利润变化来自哪里、有没有转成现金", _dataset())
        self.assertNotIn("forecast_trend", plan.question_types, plan.question_types)
        self.assertTrue(plan.adopted, plan.rejected)


class TestL1OfficialMaterialFeedsFacts(unittest.TestCase):
    """L1（09-30 深化，复核 S2）：**已准入的官方年报原文 → 财务事实 → 底稿 → 数据集 → 模型**。

    反例：官方 PDF 此前只进叙事证据——`working_paper_export.build_result` 没有
    `financials.json` 就直接 skipped，于是结构化 API 不可用时模型**一个数也拿不到**。
    这里钉住正常资料入口：材料经准入进索引 → 抽取三表 → 过契约（主体/口径/披露时点）
    → 底稿 → 冻结数据集 → 注册模型跑出并验证。
    """

    _TEXT = (
        "洋河股份 2024 年年度报告全文\n"
        "1、合并利润表\n"
        "单位：元\n"
        "项目 2024年12月31日 2023年12月31日\n"
        "营业收入 28,876,000,000.00 33,126,000,000.00\n"
        "营业成本 7,751,000,000.00 8,200,000,000.00\n"
        "归属于母公司股东的净利润 6,673,000,000.00 10,016,000,000.00\n"
        "2、合并现金流量表\n"
        "单位：元\n"
        "项目 2024年12月31日 2023年12月31日\n"
        "经营活动产生的现金流量净额 4,629,000,000.00 6,130,000,000.00\n"
    )

    def setUp(self):
        import tempfile
        import task_state
        self.tmp = Path(tempfile.mkdtemp(prefix="fa_l1_ws_"))
        self._old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", self._old_root)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = str(Path(tempfile.mkdtemp(prefix="fa_l1_db_")) / "t.db")
        self._orig_db = task_state.DB_PATH
        task_state.DB_PATH = self.db
        self.addCleanup(setattr, task_state, "DB_PATH", self._orig_db)
        self.tid = "l1-official"
        self.goal = "研究洋河股份（002304.SZ）2023 与 2024 年年度报告研究"
        task_state.mark_queued(
            self.tid, goal=self.goal, db_path=self.db,
            research_request={"company": "洋河股份", "company_id": "002304.SZ",
                              "market": "cn", "periods": [2023, 2024], "caliber": "合并",
                              "as_of": "2025-04-30", "identity_source": "form"})
        self.ws = Path(ws_mod.task_workspace(self.tid, "default"))
        (self.ws / "project").mkdir(parents=True, exist_ok=True)
        self.assertFalse((self.ws / "project" / "financials.json").exists(),
                         "本用例前提：结构化 API 不可用（没有 financials.json）")

    def _admit_material(self, *, disclosure_date: str = "2025-04-03", status: str = "admitted"):
        """把一份"已准入的年报原文"写进材料索引（正文用替身，不解析真 PDF）。"""
        import material_intake as mi
        mid = "mat-l1-1"
        meta = {"material_id": mid, "channel": mi.CHANNEL_UPLOAD,
                "kind": "pdf", "title": "洋河股份2024年年度报告",
                "url": "https://static.cninfo.com.cn/finalpage/2025-04-03/x.PDF",
                "bytes": len(self._TEXT), "raw_sha256": "h-l1",
                "status": status, "reason": "", "provenance": "official_discovery",
                "source_class": "official_disclosure", "doc_type": "年度报告",
                "period": "2024年", "disclosure_date": disclosure_date,
                "text_sha256": "t-l1", "created_at": "2025-04-03T10:00:00"}
        mi.save_meta(self.tid, meta, project="default")
        doc = {"title": "洋河股份 2024 年年度报告全文", "text": self._TEXT,
               "url": meta["url"], "page_offsets": [(0, 12)]}
        p = mock.patch.object(mi, "load_doc", lambda *a, **k: dict(doc))
        p.start()
        self.addCleanup(p.stop)
        return mid

    def test_official_pdf_becomes_facts_paper_and_dataset(self):
        import financial_analysis as fa
        from working_paper_export import write_working_paper
        from workers.data_analyzer_worker import DataAnalyzerWorker
        self._admit_material()
        wp = write_working_paper(self.tid, self.goal, project="default")
        self.assertTrue(wp.get("ok"), wp)
        self.assertGreater(wp.get("rows") or 0, 0, wp)
        self.assertEqual(wp.get("request_source"), "official_material", wp)
        facts = wp.get("facts") or []
        metrics = {str(f.get("metric")) for f in facts}
        self.assertIn("revenue", metrics, facts)
        self.assertIn("net_profit", metrics, facts)
        self.assertIn("gross_profit", metrics, "毛利由营业收入−营业成本派生（带血缘）")
        rev = [f for f in facts if f.get("metric") == "revenue"]
        self.assertEqual({x["period"] for x in rev}, {"2023年", "2024年"})
        self.assertTrue(all(x.get("unit") == "元" for x in rev), rev)
        # 底稿 → 冻结数据集 → 注册模型（同一条现役链，不是旁路脚本）
        ds, label = DataAnalyzerWorker._freeze_dataset(
            "working_paper", self.ws / "project" / "working_paper.json",
            task={"goal": self.goal, "context": {"root_task_id": self.tid}},
            required_metrics=("revenue", "net_profit", "gross_profit",
                              "operating_cashflow"),
            available_models=[m.model_id for m in fa.specs()])
        self.assertEqual(tuple(ds.manifest.periods), ("2023年", "2024年"), ds.manifest.gaps)
        self.assertTrue(ds.require("revenue", "2024年").usable)
        # R1-a：期间身份必须**贯通到底**——材料事实 → 底稿行 → 冻结观察都要带上
        # 期间性质/列头/区间（此前底稿与冻结各丢一次，模型只看到"2024年"）。
        self.assertTrue(all(str(f.get("period_kind") or "") == "flow" for f in facts), facts)
        self.assertTrue(all(str(f.get("period_label") or "") for f in facts),
                        "底稿行必须带列头原文（否则冻结无从判断半年度/期初）")
        rev24 = ds.require("revenue", "2024年")
        self.assertEqual(rev24.period_kind, "flow")
        self.assertTrue(rev24.period_label, "冻结观察必须保留列头原文")
        run = fa.run("profit_bridge", ds)
        self.assertEqual(run.status, fa.RunStatus.VALIDATED, run.reason)
        vals = {o.metric: o.value for o in run.outputs}
        self.assertAlmostEqual(vals["net_profit_change"],
                               (6673000000.0 - 10016000000.0), places=0)

    def test_empty_financials_does_not_block_the_official_route(self):
        """R2-b（09-30 下午复核）：`financials.json` 存在但内容空/错误 → 不得阻断官方年报退路。

        反例：载荷 `{"error":"upstream unavailable","data":[]}` 时旧实现只看"
        文件存在"，直接走结构化分支 → rows=0、paper_ok=false，官方 helper **调用 0 次**。
        """
        from working_paper_export import build_result
        self._admit_material()
        (self.ws / "project" / "financials.json").write_text(
            json.dumps({"error": "upstream unavailable", "data": []},
                       ensure_ascii=False), encoding="utf-8")
        res = build_result(self.tid, self.goal, project="default")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("request_source"), "official_material", res)
        self.assertGreater(res.get("rows") or 0, 0, res)
        self.assertTrue(any("未采用" in n for n in (res.get("official_notes") or [])),
                        f"必须说明为什么没用结构化载荷：{res.get('official_notes')}")

    def test_report_version_arbitration_ignores_index_order(self):
        """R2-a：同 as_of 下原稿/更正稿必须按**披露版本**裁决，目录顺序不得决定读数。

        复核反例：仅调换索引顺序，2020 净利就从 −2,354,850,607.11 变成 −2,399,698,095.52。
        """
        import material_intake as mi
        from working_paper_export import build_result

        def _text(np_2024: float) -> str:
            return self._TEXT.replace("6,673,000,000.00", f"{np_2024:,.2f}")

        plan = [
            {"material_id": "mat-orig", "title": "洋河股份2024年年度报告",
             "disclosure_date": "2025-04-03", "np": 6673000000.0},
            {"material_id": "mat-corr", "title": "洋河股份2024年年度报告（更正后）",
             "disclosure_date": "2025-04-20", "np": 6873000000.0},
        ]
        entries, docs = [], {}
        for spec in plan:
            text = _text(spec["np"])
            entries.append({**spec, "status": "admitted", "kind": "pdf",
                            "url": f"https://static.cninfo.com.cn/{spec['material_id']}.PDF",
                            "caliber": "合并", "material_id": spec["material_id"]})
            docs[spec["material_id"]] = {"title": spec["title"], "text": text,
                                         "url": entries[-1]["url"], "page_offsets": [(0, 12)]}
        p = mock.patch.object(mi, "load_doc",
                              lambda tid, mid, **kw: dict(docs.get(str(mid), {})))
        p.start()
        self.addCleanup(p.stop)

        results = {}
        for order in ([0, 1], [1, 0]):
            seq = [entries[i] for i in order]
            with mock.patch.object(mi, "read_index", lambda *a, **k: list(seq)):
                res = build_result(self.tid, self.goal, project="default")
            np24 = [f for f in (res.get("facts") or [])
                    if f.get("metric") == "net_profit" and f.get("period") == "2024年"]
            self.assertTrue(np24, res)
            results[tuple(order)] = (float(np24[0]["value"]),
                                     tuple(sorted((f.get("metric"), f.get("period"),
                                                   f.get("value"))
                                                  for f in (res.get("facts") or []))))
            notes = " ".join(res.get("official_notes") or [])
            self.assertIn("版本差异", notes, notes)
            self.assertIn("更正", notes, notes)
        self.assertEqual(results[(0, 1)][0], 6873000000.0,
                         "披露更晚的更正稿应当胜出（不是索引里先出现的那份）")
        self.assertEqual(results[(0, 1)], results[(1, 0)],
                         "调换索引顺序不得改变任何读数")

    def test_late_disclosure_is_not_taken_into_facts(self):
        self._admit_material(disclosure_date="2025-06-30")   # 晚于契约 as_of=2025-04-30
        from working_paper_export import write_working_paper
        wp = write_working_paper(self.tid, self.goal, project="default")
        self.assertFalse(wp.get("ok"), wp)
        self.assertTrue(wp.get("skipped"), wp)
        self.assertTrue(any("晚于" in n for n in (wp.get("official_notes") or [])), wp)

    def test_unadmitted_material_is_not_used(self):
        self._admit_material(status="pending")
        from working_paper_export import write_working_paper
        wp = write_working_paper(self.tid, self.goal, project="default")
        self.assertFalse(wp.get("ok"), wp)
        self.assertIn("官方", str(wp.get("reason") or ""), wp)

    def test_no_material_gives_an_actionable_skip(self):
        from working_paper_export import write_working_paper
        wp = write_working_paper(self.tid, self.goal, project="default")
        self.assertFalse(wp.get("ok"), wp)
        self.assertTrue(wp.get("skipped"), wp)
        self.assertIn("官方", str(wp.get("reason") or ""), wp)


def _payload_of(model_id: str, dataset) -> dict:
    """跑一次算子拿**原始载荷**（独立验证的输入），并带上 params（与 runner 一致）。"""
    spec = fa.registry.spec(model_id)
    _impl, compute, _gold = fa.registry.OPERATORS[spec.operator]
    payload = dict(compute(dataset, {}))
    payload["params"] = {}
    return payload


class TestR1APeriodChain(unittest.TestCase):
    """R1-a（09-30 下午复核）：期间语义要从**材料事实贯通到冻结数据集与身份**。

    反例（复核原文）：`dataset._observation_of` 丢 `period_kind`/`period_label`，
    于是"毛利 flow→stock 互换"冻结 hash 不变且利润桥仍 validated；"毛利截至 6/30、
    净利全年"也 validated。这里逐个钉住：冻结必须带字段、改 kind/label 必须换身份、
    半年度不得冒年报、同期区间必须相容、合法全年 + 年末存量照常通过。
    """

    @staticmethod
    def _with_periods(rows):
        """给每行按指标角色补上期间性质与起止（模拟抽取器/底稿给出的期间身份）。"""
        import financial_analysis.contracts as C
        out = []
        for r in rows:
            r = dict(r)
            role = C.period_role_of(r.get("metric"))
            if role:
                y = str(r.get("period") or "")[:4]
                r["period_kind"] = role
                r["period_start"] = f"{y}-01-01"
                r["period_end"] = f"{y}-12-31"
                r["period_label"] = (f"{y}年12月31日" if role == "stock" else f"{y}年度")
            out.append(r)
        return out

    def _bridge_rows(self, *, np_24="{y}-12-31", gp_24="{y}-12-31", kinds=None):
        rows = [
            _row("net_profit", "2023年", 100.16, period_kind="flow",
                 period_start="2023-01-01", period_end="2023-12-31",
                 period_label="2023年度"),
            _row("net_profit", "2024年", 66.73, period_kind=(kinds or {}).get("np", "flow"),
                 period_start="2024-01-01",
                 period_end=np_24.format(y=2024), period_label="2024年度"),
            _row("gross_profit", "2023年", 249.26, period_kind="flow",
                 period_start="2023-01-01", period_end="2023-12-31",
                 period_label="2023年度"),
            _row("gross_profit", "2024年", 211.25,
                 period_kind=(kinds or {}).get("gp", "flow"),
                 period_start="2024-01-01",
                 period_end=gp_24.format(y=2024), period_label="2024年度"),
        ]
        return rows

    def test_freeze_keeps_period_kind_and_label_in_identity(self):
        ds = _dataset(self._with_periods(_two_period_rows()))
        got = {(o.metric, o.period): o for o in ds.observations}
        rev23 = got[("revenue", "2023年")]
        self.assertEqual(rev23.period_kind, "flow", "冻结不得丢掉期间性质")
        self.assertEqual(rev23.period_label, "2023年度")
        self.assertEqual(rev23.period_start, "2023-01-01")
        self.assertEqual(rev23.period_end, "2023-12-31")
        # 只改**列头原文** → 换了一份观察（也换数据集身份）
        rows2 = self._with_periods(_two_period_rows())
        for r in rows2:
            if r["metric"] == "revenue" and r["period"] == "2023年":
                r["period_label"] = "2023年1-6月"
        ds2 = _dataset(rows2)
        got2 = {(o.metric, o.period): o for o in ds2.observations}
        self.assertNotEqual(rev23.observation_hash,
                            got2[("revenue", "2023年")].observation_hash,
                            "只改期间列头必须让观察身份失效")
        self.assertNotEqual(ds.dataset_hash, ds2.dataset_hash)

    def test_stock_flow_swap_is_refused(self):
        """两期毛利一条 flow 一条 stock（同一指标口径）→ 不得计算。"""
        rows = self._bridge_rows(kinds={"gp": "stock"})
        ds = _dataset(rows)
        run = fa.run("profit_bridge", ds)
        self.assertEqual(run.status, "not_applicable", run.reason)
        self.assertIn("期间", run.reason)

    def test_half_year_flow_does_not_become_an_annual_period(self):
        rows = self._bridge_rows(np_24="2024-06-30", gp_24="2024-06-30")
        ds = _dataset(rows)
        self.assertNotIn("2024年", ds.manifest.periods, ds.manifest.periods)
        self.assertTrue(any("不按年度期间使用" in g for g in ds.manifest.gaps),
                        ds.manifest.gaps)
        run = fa.run("profit_bridge", ds)
        self.assertNotEqual(run.status, "validated", run.reason)

    def test_same_period_mixed_intervals_are_refused(self):
        """同一 "2024年"：净利全年、毛利半年 → 同期区间不相容，不得混算。"""
        rows = self._bridge_rows(gp_24="2024-06-30")
        ds = _dataset(rows)
        run = fa.run("profit_bridge", ds)
        self.assertEqual(run.status, "not_applicable", run.reason)
        self.assertIn("区间", run.reason)

    def test_legit_full_year_and_year_end_stock_still_pass(self):
        ds = _dataset(self._with_periods(_wc_rows()))
        self.assertEqual(fa.run("profit_bridge", ds).status, "validated")
        wc = fa.run("working_capital", ds)
        self.assertEqual(wc.status, "validated", wc.reason)
        # 期初→上期末：上一年的 12-31 存量是**合法**的上期余额，不得被期间判据拒掉
        got = {o.metric: o for o in ds.observations}
        self.assertEqual(got["inventory"].period_kind, "stock")
        self.assertTrue(all(o.period_end.endswith("12-31")
                            for o in ds.observations if o.metric == "inventory"))


class TestOperatingDriversYanghe(unittest.TestCase):
    """U1（2026-10-01）：经营驱动利润桥用**洋河 2024 年报真实披露数**验收。

    数值来源与页码见 `scripts/u1_yanghe_drivers.py` 头部表格；这里按 元 入数据集，
    手算期望值写在断言里（亿元换算到 3 位）。分段里 2023 年白酒成本由披露同比推算，
    标 `derived_from`——算子只把它当输入，报告层要能看出它是推算值。
    """

    # 合并利润表（元）：(2023, 2024)
    IS_YUAN = {
        "revenue": (33_126_277_551.51, 28_876_296_993.56),
        "operating_cost": (8_200_245_255.42, 7_751_218_356.66),
        "net_profit": (10_015_930_040.27, 6_673_388_602.12),
        "net_profit_consolidated": (10_020_768_556.47, 6_666_455_819.96),
        "taxes_and_surcharges": (5_269_245_592.35, 4_826_086_952.64),
        "selling_expense": (5_386_953_700.62, 5_516_238_544.79),
        "admin_expense": (1_764_423_149.06, 1_924_730_302.35),
        "rd_expense": (284_753_881.33, 104_796_407.26),
        "finance_expense": (-754_525_568.63, -610_889_994.14),
        "other_income": (56_179_399.53, 59_667_934.13),
        "investment_income": (255_520_777.61, 146_415_168.80),
        "fair_value_change": (-37_082_477.77, -396_164_080.43),
        "credit_impairment": (881_383.32, 667_208.93),
        "asset_impairment": (-2_828_018.24, -11_203_156.73),
        "asset_disposal_income": (-5_282_977.32, -2_729_328.84),
        "non_operating_income": (39_176_788.83, 52_446_752.81),
        "non_operating_expense": (63_913_298.25, 70_140_310.99),
        "income_tax_expense": (3_197_064_562.60, 2_476_620_791.72),
        "minority_interest": (4_838_516.20, -6_932_782.16),
    }
    SEG_REVENUE_YUAN = (32_389_581_931.71, 28_175_707_878.18)      # 白酒
    SEG_COST_CUR_YUAN = 7_281_082_736.44
    SEG_COST_YOY = -0.0543                                          # 披露同比 → 推算 2023
    VOLUME_TON = (166_154.73, 139_076.05)                           # 白酒销量

    def _rows(self, *, drop=(), segment=True, volume=True):
        rows = []
        for metric, (prev, cur) in self.IS_YUAN.items():
            if metric in drop:
                continue
            rows.append(_row(metric, "2023年", prev, unit="元",
                             fact_id=f"fact-{metric}-2023"))
            rows.append(_row(metric, "2024年", cur, unit="元",
                             fact_id=f"fact-{metric}-2024"))
        if segment:
            seg_cost_prev = self.SEG_COST_CUR_YUAN / (1 + self.SEG_COST_YOY)
            for metric, (prev, cur) in (("revenue", self.SEG_REVENUE_YUAN),
                                        ("operating_cost", (seg_cost_prev,
                                                            self.SEG_COST_CUR_YUAN))):
                rows.append(_row(metric, "2023年", prev, unit="元", caliber="分产品:白酒",
                                 fact_id=f"fact-seg-{metric}-2023",
                                 derived_from=(("fact-annual-tables",)
                                               if metric == "operating_cost" else ())))
                rows.append(_row(metric, "2024年", cur, unit="元", caliber="分产品:白酒",
                                 fact_id=f"fact-seg-{metric}-2024"))
        if volume:
            for period, q, fid in (("2023年", self.VOLUME_TON[0], "fact-vol-2023"),
                                   ("2024年", self.VOLUME_TON[1], "fact-vol-2024")):
                rows.append(_row("sales_volume", period, q, unit="吨",
                                 caliber="分产品:白酒", fact_id=fid))
        return rows

    def _dataset(self, **kw):
        return fa.freeze_from_facts(self._rows(**kw), periods=(2023, 2024),
                                    entity="洋河股份", entity_id="002304.SZ",
                                    as_of="2025-04-30", source_label="test:yah")

    def _run(self, **kw):
        return fa.run("operating_drivers", self._dataset(**kw))

    def _yi(self, run, metric):
        out = next(o for o in run.outputs if o.metric == metric)
        return out.value / 1e8

    def test_real_numbers_close_and_match_the_hand_computed_bridge(self):
        run = self._run()
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        self.assertEqual(run.validation["failed"], [])
        self.assertAlmostEqual(self._yi(run, "net_profit_change"), -33.4254, places=3)
        self.assertAlmostEqual(self._yi(run, "gross_profit_change"), -38.0095, places=3)
        self.assertAlmostEqual(self._yi(run, "revenue_scale_effect"), -31.5354, places=3)
        self.assertAlmostEqual(self._yi(run, "gross_margin_effect"), -6.4741, places=3)
        self.assertAlmostEqual(self._yi(run, "below_gross_line_change"), 4.5841, places=3)
        self.assertAlmostEqual(self._yi(run, "revenue_scale_effect")
                               + self._yi(run, "gross_margin_effect"),
                               self._yi(run, "gross_profit_change"), places=2)
        self.assertAlmostEqual(self._yi(run, "gross_profit_change")
                               + self._yi(run, "below_gross_line_change"),
                               self._yi(run, "net_profit_change"), places=2)

    def test_line_items_explain_below_gross_line_and_residual_is_zero_when_complete(self):
        run = self._run()
        detail = next(o for o in run.outputs if o.metric == "net_profit_change_detail")
        comp = {c["component_id"]: c["value"] / 1e8 for c in detail.components}
        self.assertAlmostEqual(comp["income_tax_expense"], 7.2044, places=3,
                               msg="所得税减少 7.20 亿元（随利润下滑的被动结果）")
        self.assertAlmostEqual(comp["taxes_and_surcharges"], 4.4316, places=3)
        self.assertAlmostEqual(comp["fair_value_change"], -3.5908, places=3)
        self.assertAlmostEqual(comp["admin_expense"], -1.6031, places=3)
        self.assertEqual(comp["unexplained_residual"], 0.0,
                         "披露项目齐全时未解释差额必须为 0（不是摊派）")
        diag = run.outputs[0].diagnostics
        self.assertEqual(diag["line_items_missing"], [])
        self.assertEqual(diag["line_items_rejected"], [])
        self.assertEqual(diag["unexplained_residual_yuan"], 0.0)

    def test_missing_disclosure_stays_in_residual_not_allocated(self):
        """只给收入/成本/净利时：ΔB 全部进未解释差额，不得摊到任何已列项目。"""
        run = self._run(drop=tuple(k for k in self.IS_YUAN
                                   if k not in ("revenue", "operating_cost", "net_profit")),
                        segment=False, volume=False)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        detail = next(o for o in run.outputs if o.metric == "net_profit_change_detail")
        listed = [c for c in detail.components
                  if c["component_id"] != "unexplained_residual"]
        self.assertEqual(listed, [], "没取到的项目不得凭空出现在分项里")
        comp = {c["component_id"]: c["value"] / 1e8 for c in detail.components}
        self.assertAlmostEqual(comp["unexplained_residual"], 4.5841, places=3)

    def test_report_scope_is_not_treated_as_a_segment(self):
        """U1 实测反例：抽取器同时给出合并与母公司两套利润表时，「母公司」**不是**分段切法。

        把它当分段 → 未分类差额会被算成 −34.67 亿（真实是 −0.05 亿，且本该没有分段输出）。
        """
        rows = [r for r in self._rows(segment=False, volume=False)]
        # 追加一套"母公司"范围的收入/成本（真实年报里确实同时存在）
        for metric, prev, cur in (("revenue", 13_212_200_864.23, 12_852_221_243.40),
                                  ("operating_cost", 6_866_625_130.04, 6_840_375_733.91)):
            rows.append(_row(metric, "2023年", prev, unit="元", caliber="母公司",
                             fact_id=f"fact-parent-{metric}-2023"))
            rows.append(_row(metric, "2024年", cur, unit="元", caliber="母公司",
                             fact_id=f"fact-parent-{metric}-2024"))
        ds = fa.freeze_from_facts(rows, periods=(2023, 2024), entity="洋河股份",
                                  entity_id="002304.SZ", as_of="2025-04-30",
                                  source_label="test:yah-parent")
        run = fa.run("operating_drivers", ds)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        self.assertNotIn("gross_profit_change_by_segment",
                         [o.metric for o in run.outputs],
                         "只有报表范围、没有分段维度时不得输出分段分解")
        diag = run.outputs[0].diagnostics
        self.assertEqual(diag["segments_used"], [])
        self.assertTrue(any("报表范围" in s for s in diag["segments_skipped"]),
                        diag["segments_skipped"])

    def test_segments_are_alternative_cuts_not_additive(self):
        run = self._run()
        seg = next(o for o in run.outputs if o.metric == "gross_profit_change_by_segment")
        comp = {c["component_id"]: c["value"] / 1e8 for c in seg.components}
        self.assertAlmostEqual(comp["segment:分产品:白酒"], -37.9579, places=2)
        self.assertAlmostEqual(comp["unclassified_gross_profit_change"], -0.0516, places=2,
                               msg="公司毛利变化 − 已列口径：其他业务/口径差单独列出")
        self.assertAlmostEqual(sum(comp.values()), self._yi(run, "gross_profit_change"),
                               places=2)

    def test_volume_price_with_structure_caveat(self):
        run = self._run()
        vp = next(o for o in run.outputs if o.metric == "volume_price_decomposition")
        comp = {c["component_id"]: c["value"] / 1e8 for c in vp.components}
        self.assertAlmostEqual(vp.value / 1e8, -42.1380, places=2,
                               msg="白酒收入变化 −42.14 亿元")
        self.assertAlmostEqual(comp["volume_effect"] + comp["price_effect"],
                               vp.value / 1e8, places=2)
        blob = " ".join(str(c.get("formula", "")) for c in vp.components)
        self.assertIn("结构混合", blob, "均价必须标注含产品结构混合")
        self.assertIn("不得称“提价效果”", blob, "不得把均价变化命名成提价效果")

    def test_tampered_components_fail_independent_validation(self):
        """两个分项 +1/−1（合计不变）→ 逐项核对/金样必须报红（R1-b 反例形状）。"""
        from financial_analysis.operators import operating_drivers as od
        ds = self._dataset()
        payload = od.compute(ds)
        gp = next(o for o in payload["outputs"] if o["metric"] == "gross_profit_change")
        gp["components"][0]["value"] += 100_000_000.0    # 1 亿元：合计不变、分项被换
        gp["components"][1]["value"] -= 100_000_000.0
        res = fa.validation.validate_output(fa.registry.spec("operating_drivers"), ds, payload)
        self.assertFalse(res["ok"], "分项被换而合计不变：必须失败")
        self.assertIn("components", res["failed"])

    def test_card_and_brief_block_carry_contributions_segments_and_alternatives(self):
        """U1：分析卡/正文必须同时给出**逐项贡献、分段切法、量价与替代解释**。

        只给"毛利/毛利线以下"两段，读者仍不知道钱从哪来、还有没有别的解释（这是 K3 之后
        仍存在的浅解释问题）。
        """
        from financial_analysis import report_adapter as ra
        from financial_analysis import store as fa_store
        run = self._run()
        card = ra.analysis_card(run)
        self.assertIn("经营驱动", card["kind"])
        self.assertGreaterEqual(len(card.get("contributions") or []), 15,
                                "逐项贡献要一起出来（含未解释差额）")
        self.assertEqual(len(card.get("segment_cuts") or []), 1,
                         "本夹具只有分产品一个口径；真实年报里四种切法各一张（见案例证据）")
        self.assertIn("分产品", str(card["segment_cuts"][0]["cut"]))
        self.assertTrue(card.get("volume_price"), "量价要在卡上")
        alt = card.get("alternative_explanations") or {}
        self.assertAlmostEqual(float(alt["effective_tax_rate"]["prev"]), 0.2419, places=3)
        self.assertAlmostEqual(float(alt["effective_tax_rate"]["cur"]), 0.2709, places=3)
        self.assertAlmostEqual(float(alt["tax_at_prior_rate"]["rate_effect_yuan"]) / 1e8,
                               2.65, places=1, msg="税率因素多吃掉约 2.65 亿元")
        self.assertIn("fair_value_change", alt.get("non_operating_items") or {})
        self.assertTrue(card.get("data_gaps"), "缺料（红酒/其他无成本）要如实列出")

        block = fa_store.render_card_block(run)
        for needle in ("三项最大利润贡献", "亿元", "不可跨切法相加", "实际税率",
                       "提价效果", "待核查"):
            self.assertIn(needle, block, f"正文分析卡缺「{needle}」")
        self.assertNotIn("没有注册模型", block)

    def test_components_gold_matches_payload_item_by_item(self):
        from financial_analysis.operators import operating_drivers as od
        ds = self._dataset()
        payload = od.compute(ds)
        expect = od.components_gold(ds)
        for metric, ids in od.SPEC.component_ids.items():
            out = next(o for o in payload["outputs"] if o["metric"] == metric)
            got = {c["component_id"]: c["value"] for c in out["components"]}
            self.assertEqual(set(got), set(ids), metric)
            for cid in ids:
                want, _unit = expect[metric][cid]
                self.assertAlmostEqual(got[cid], float(want), places=2,
                                       msg=f"{metric}.{cid}")


if __name__ == "__main__":
    unittest.main(verbosity=1)
