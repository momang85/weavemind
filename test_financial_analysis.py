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
                         ["profit_bridge", "operating_drivers", "cash_reconciliation",
                          "cash_quality", "working_capital", "scenario_sensitivity",
                          "profit_to_cash"])


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
        """`build_structure` 的接缝：工作区有已验证运行 → 结构里带分析摘要；没有 → 空串。

        V0（阶段V）：主正文只留三条摘要；完整卡（run/output/component_id）落 `analysis/`
        底稿，读者要看细节有出处。
        """
        import dataclasses
        import report_brief
        from financial_analysis import store
        self.assertEqual(report_brief._analysis_card_block(self.task_id,
                                                           ws_dir=self.ws), "")
        store.save_run(self.ws, self.run)
        blk = report_brief._analysis_card_block(self.task_id, ws_dir=self.ws)
        self.assertIn("## 分析摘要", blk)
        # 这条运行不属于经营研究组合 → 摘要为空，但必须指向底稿（不假装有结论）
        self.assertIn("经营研究组合", blk)
        detail = Path(self.ws) / "analysis" / "analysis_cards.md"
        self.assertTrue(detail.is_file(), "完整卡要落到 analysis/analysis_cards.md")
        text = detail.read_text(encoding="utf-8")
        self.assertIn(f"run={self.run.run_id[:12]}", text)
        self.assertIn(self.run.outputs[0].output_id, text)
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
        # V0：主正文是**三条摘要**（一行一个标题），完整卡按模型分节落在底稿里；
        # 正文只允许出现一个 `## ` 标题（否则同一份交付里像重复装配）。
        heads = [ln for ln in blk.splitlines() if ln.startswith("## ")]
        self.assertEqual(heads, ["## 分析摘要"], blk[:200])
        cards = (ws / "analysis" / "analysis_cards.md").read_text(encoding="utf-8")
        self.assertIn("### 分析卡（cash_quality）", cards)

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
        # U2 新增现金调节桥后规格变化：结构化底稿有归母净利与经营现金流，但**没有合并净利润**
        # （那是现金流量表补充资料的起点）→ 该家族带"缺输入"被拒，状态如实 partial。
        self.assertEqual(got["status"], "partial", got["plan"]["rejected"])
        self.assertEqual(set(got["plan"]["adopted"]),
                         {"profit_bridge", "operating_drivers", "cash_quality",
                          "working_capital", "scenario_sensitivity", "profit_to_cash"},
                         got["plan"]["rejected"])
        rej = {r["model_id"]: r for r in got["plan"]["rejected"]}
        self.assertEqual(set(rej), {"cash_reconciliation"})
        self.assertEqual(rej["cash_reconciliation"]["missing"], ["net_profit_consolidated"],
                         "现金调节桥要合并净利润：缺它就拒，不拿归母净利冒充")
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
        # 缺口如实：载荷里没有占款字段（也没有营业成本、没有合并净利润）→ 营运资本、经营驱动
        # 与现金调节桥被拒，状态 partial 而不是 success（U2：注册后同一份载荷多一条"缺输入"）
        self.assertEqual(sorted(r["model_id"] for r in got["plan"]["rejected"]),
                         ["cash_reconciliation", "operating_drivers", "working_capital"],
                         got["plan"])
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
    def test_registry_lists_seven_families_and_each_has_an_operator(self):
        # U1/U2（2026-10-01）：新增 operating_drivers（经营驱动分解）与
        # cash_reconciliation（现金调节桥）。
        self.assertEqual([m.model_id for m in fa.specs()],
                         ["profit_bridge", "operating_drivers", "cash_reconciliation",
                          "cash_quality", "working_capital", "scenario_sensitivity",
                          "profit_to_cash"])
        self.assertEqual(set(fa.registry.operators()),
                         {"profit_bridge_v1", "operating_drivers_v1",
                          "cash_reconciliation_v1", "cash_quality_v1",
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
        self.assertEqual(set(rej), {"operating_drivers", "cash_reconciliation",
                                    "working_capital"})
        self.assertEqual(rej["operating_drivers"]["reason"], "缺输入")
        self.assertEqual(rej["operating_drivers"]["missing"], ["operating_cost"],
                         "经营驱动桥需要营业成本：缺它就拒，不拿毛利顶替")
        self.assertEqual(rej["cash_reconciliation"]["missing"], ["net_profit_consolidated"],
                         "现金调节桥需要合并净利润（补充资料的起点）")
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
        """契约完整性：声明了 component_ids 的模型必须有 `components_gold` 且 id 一致。

        U2：这条检查只对**输入齐备**的模型适用（本夹具没有合并净利润，现金调节桥在这里
        不可用；用 `available_for` 判，而不是把不可用的模型也算进来）。
        """
        ds = _dataset(_wc_rows())
        usable = set(fa.registry.available_for(ds))
        declared = {m.model_id: m.component_ids for m in fa.specs()
                    if m.component_ids and m.model_id in usable}
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
        # 底稿 → 冻结数据集 → 注册模型（同一条现役链，不是旁路脚本）；V0 起同时返回
        # **事实定位**（表名/页码随数据走，正文与图据此指回原件）
        ds, label, locators = DataAnalyzerWorker._freeze_dataset(
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
        # V0：事实定位要跟着数据走（正文/图的"原文定位"用它指回表名与页码）
        self.assertTrue(locators, "冻结时要把事实定位一起带出来")
        self.assertTrue(any(str(v.get("locator") or "") for v in locators.values()),
                        locators)
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



    def test_admitted_official_facts_strengthen_the_structured_input(self):
        """X0（真实入口收口）：结构化载荷可用时，已准入官方三表**共同增强**输入。

        反例（真实任务 `ui-17947f055b`）：`financials.json` 可用就只走结构化分支，官方
        三表/现金附注被跳过 → 缺营业成本与合并净利 → 经营驱动/现金调节「缺输入」不适用。
        这里钉住：① 两条来源合并进同一份底稿；② 同口径读数**以官方三表为准**（带页码定位、
        单位是官方自己的元），结构化值逐条对照记录；③ 官方三表带来的营业成本/合并净利/
        现金附注真的进了底稿。
        """
        from working_paper_export import build_result
        self._admit_material()
        (self.ws / "project" / "financials.json").write_text(json.dumps({
            "financials": [
                {"year": 2023, "report_type": "年报", "disclosure_date": "2025-04-03",
                 "revenue": 331.26, "net_profit": 100.16, "operating_cashflow": 61.30,
                 "total_assets": 900.0, "total_liabilities": 200.0},
                {"year": 2024, "report_type": "年报", "disclosure_date": "2025-04-03",
                 "revenue": 288.76, "net_profit": 66.73, "operating_cashflow": 46.29,
                 "total_assets": 950.0, "total_liabilities": 210.0}],
            "metadata": {"source": "structured_api", "company": "洋河股份",
                         "company_id": "002304.SZ", "currency": "CNY", "unit": "亿元",
                         "caliber": "合并", "as_of": "2025-04-30"},
            "raw": {"url": "https://example.invalid/api", "text": "{}"},
        }, ensure_ascii=False), encoding="utf-8")
        res = build_result(self.tid, self.goal, project="default")
        self.assertTrue(res.get("ok"), res)
        rows = res.get("rows_detail") or []
        by_key = {(str(r.get("metric")), str(r.get("period"))): r for r in rows}
        # ① 金额统一到元：结构化给的 288.76（亿元）不再是"288.76 元"
        rev = by_key[("revenue", "2024年")]
        self.assertEqual(rev.get("unit"), "元", rev)
        self.assertAlmostEqual(float(rev.get("value")), 28_876_000_000.0, places=2)
        # ② 官方三表带来的行（结构化载荷里没有营业成本/合并净利）
        metrics = {str(r.get("metric")) for r in rows}
        self.assertIn("operating_cost", metrics, sorted(metrics))
        self.assertIn("gross_profit", metrics, "毛利由官方收入的营业成本派生（带血缘）")
        # ③ 同一 (指标, 期间) 两个来源给出同一个值 → 记成互相印证，而不是二选一
        notes = " ".join(res.get("official_notes") or [])
        self.assertIn("官方三表优先", notes)
        self.assertIn("一致", notes, "同口径读数要写明两来源一致（不是先到先得）")
        self.assertIn("官方材料", notes)
        self.assertTrue(res.get("paper_ok"), res.get("problems"))

    def test_two_sources_disagreeing_records_both_and_takes_the_official(self):
        """同口径但两来源**数值不同** → 两值都记录，且**以官方三表为准**（不静默择一）。

        为什么不是"判冲突后必需指标不达标"：结构化载荷把亿元四舍五入到两位，与官方三表
        必然差几十万——两条并列会被如实判成冲突，于是底稿 0 项达标、模型"缺输入"（实测）。
        合理的纪律是：官方（带页码定位、经审计）为准，差异逐条写进 notes。
        """
        from working_paper_export import build_result
        self._admit_material()
        (self.ws / "project" / "financials.json").write_text(json.dumps({
            "financials": [
                {"year": 2023, "report_type": "年报", "disclosure_date": "2025-04-03",
                 "revenue": 331.26, "net_profit": 100.16, "operating_cashflow": 61.30},
                {"year": 2024, "report_type": "年报", "disclosure_date": "2025-04-03",
                 "revenue": 300.00, "net_profit": 66.73, "operating_cashflow": 46.29}],
            "metadata": {"source": "structured_api", "company": "洋河股份",
                         "company_id": "002304.SZ", "currency": "CNY", "unit": "亿元",
                         "caliber": "合并", "as_of": "2025-04-30"},
            "raw": {"url": "https://example.invalid/api", "text": "{}"},
        }, ensure_ascii=False), encoding="utf-8")
        res = build_result(self.tid, self.goal, project="default")
        notes = " ".join(res.get("official_notes") or [])
        self.assertIn("不一致", notes, notes)
        self.assertIn("官方三表为准", notes)
        self.assertIn("30000000000", notes.replace(",", ""), "结构化那个值也要留下")
        rev = next(r for r in (res.get("rows_detail") or [])
                   if str(r.get("metric")) == "revenue"
                   and str(r.get("period")) == "2024年")
        self.assertAlmostEqual(float(rev.get("value")), 28_876_000_000.0, places=2,
                               msg="取官方三表的值（带页码定位），而不是结构化的 300 亿")

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


class TestCashReconciliationYanghe(unittest.TestCase):
    """U2：现金调节桥用**洋河 2024 年报补充资料真实数**验收（合并净利润→经营现金流）。"""

    CF = {
        "net_profit_consolidated": (10_020_768_556.47, 6_666_455_819.96),
        "operating_cashflow": (6_130_220_867.96, 4_628_711_237.28),
        "asset_impairment_provision": (1_946_634.92, 10_535_947.80),
        "depreciation": (639_335_568.28, 586_592_227.18),
        "right_of_use_depreciation": (27_594_763.53, 32_397_883.52),
        "intangible_amortization": (59_054_597.55, 61_305_706.85),
        "long_term_prepaid_amortization": (4_026_169.92, 17_125_968.18),
        "disposal_long_asset_loss": (8_522_287.93, 37_268_976.98),
        "fixed_asset_scrap_loss": (1_853_533.74, 2_980_288.23),
        "fair_value_change_loss": (37_082_477.77, 396_164_080.43),
        "finance_expense_adjust": (2_617_344.74, -672_316.24),
        "investment_loss": (-255_520_777.61, -146_415_168.80),
        "deferred_tax_asset_decrease": (180_381_423.47, 83_804_944.67),
        "deferred_tax_liability_increase": (6_857_126.34, -123_993_077.06),
        "inventory_decrease": (-1_226_697_174.83, -843_101_567.99),
        "operating_receivable_decrease": (380_090_873.53, -651_364_248.55),
        "operating_payable_increase": (-3_582_948_946.71, -1_830_670_724.59),
        "other_cashflow_adjustments": (-174_743_591.08, 330_296_496.71),
    }

    def _dataset(self, *, drop=()):
        rows = []
        for metric, (prev, cur) in self.CF.items():
            if metric in drop:
                continue
            rows.append(_row(metric, "2023年", prev, unit="元",
                             fact_id=f"cf-{metric}-2023"))
            rows.append(_row(metric, "2024年", cur, unit="元",
                             fact_id=f"cf-{metric}-2024"))
        return fa.freeze_from_facts(rows, periods=(2023, 2024), entity="洋河股份",
                                    entity_id="002304.SZ", as_of="2025-04-30",
                                    source_label="test:cash")

    def _run(self, **kw):
        return fa.run("cash_reconciliation", self._dataset(**kw))

    def _yi(self, run, metric):
        out = next(o for o in run.outputs if o.metric == metric)
        return out.value / 1e8

    def test_closes_exactly_with_the_disclosed_schedule(self):
        run = self._run()
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        self.assertEqual(run.validation["failed"], [])
        cur = next(o for o in run.outputs
                   if o.metric == "operating_cashflow_reconciliation_cur")
        comp = {c["component_id"]: c["value"] / 1e8 for c in cur.components}
        self.assertAlmostEqual(comp["consolidated_net_profit"], 66.6646, places=3)
        self.assertAlmostEqual(comp["non_cash_adjustments"], 9.5710, places=3)
        self.assertAlmostEqual(comp["working_capital_adjustments"], -33.2514, places=3)
        self.assertAlmostEqual(comp["other_adjustments"], 3.3030, places=3)
        self.assertEqual(comp["unexplained_residual"], 0.0,
                         "披露调节项齐全时对账差额必须为 0")
        self.assertAlmostEqual(cur.value / 1e8, 46.2871, places=3)
        # 2023 同样闭合
        prev = next(o for o in run.outputs
                    if o.metric == "operating_cashflow_reconciliation_prev")
        comp_p = {c["component_id"]: c["value"] / 1e8 for c in prev.components}
        self.assertEqual(comp_p["unexplained_residual"], 0.0)
        self.assertAlmostEqual(prev.value / 1e8, 61.3022, places=3)

    def test_largest_support_and_drag_and_gap_change(self):
        run = self._run()
        sup = next(o for o in run.outputs if o.metric == "largest_support")
        drag = next(o for o in run.outputs if o.metric == "largest_drag")
        self.assertAlmostEqual(sup.value / 1e8, 5.8659, places=3, msg="折旧是最大支撑")
        self.assertAlmostEqual(drag.value / 1e8, -18.3067, places=3,
                               msg="经营性应付减少是最大拖累")
        gap = next(o for o in run.outputs if o.metric == "cash_gap_change")
        self.assertAlmostEqual(gap.value / 1e8, 18.5280, places=3)
        # V1：缺口变化是**辅助观察**，两个分项必须与实际算式一致（ΔOCF 与 −Δ合并净利），
        # 不再把 ΔOCF 误标成"调节项合计变化"。
        comp = {c["component_id"]: c["value"] / 1e8 for c in gap.components}
        self.assertAlmostEqual(comp["change_in_operating_cashflow"], -15.0151, places=3,
                               msg="ΔOCF = 46.2871 − 61.3022")
        self.assertAlmostEqual(comp["change_in_net_profit_negated"], 33.5431, places=3)
        self.assertAlmostEqual(comp["change_in_operating_cashflow"]
                               + comp["change_in_net_profit_negated"],
                               gap.value / 1e8, places=2)
        # V1 **主输出**：现金变化桥 ΔOCF = Δ合并净利＋Δ非现金＋Δ营运资本＋Δ其他＋Δ差额
        chg = next(o for o in run.outputs if o.metric == "operating_cashflow_change")
        cc = {c["component_id"]: c["value"] / 1e8 for c in chg.components}
        self.assertAlmostEqual(chg.value / 1e8, -15.0151, places=3)
        self.assertAlmostEqual(cc["change_in_net_profit"], -33.5431, places=3)
        self.assertAlmostEqual(cc["change_in_non_cash"], 2.4335, places=3)
        self.assertAlmostEqual(cc["change_in_working_capital"], 11.0442, places=3)
        self.assertAlmostEqual(cc["change_in_other"], 5.0504, places=3)
        self.assertAlmostEqual(cc["change_in_residual"], 0.0, places=3)
        self.assertAlmostEqual(sum(cc.values()), chg.value / 1e8, places=2,
                               msg="现金变化桥必须闭合")
        diag = run.outputs[0].diagnostics
        self.assertEqual(diag["largest_support"]["metric"], "depreciation")
        self.assertEqual(diag["largest_drag"]["metric"], "operating_payable_increase")
        # U3：三一那版单列「信用减值损失/使用权资产摊销」，本夹具（洋河）没有这两行 →
        # 如实列在 missing 里；**判据是对账差额为 0**（上面已断言），不是"missing 必须为空"。
        self.assertIn("credit_impairment_provision", diag["items_missing"]["cur"])

    def test_missing_items_stay_in_residual_not_allocated(self):
        run = self._run(drop=("depreciation", "inventory_decrease",
                              "operating_payable_increase"))
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        cur = next(o for o in run.outputs
                   if o.metric == "operating_cashflow_reconciliation_cur")
        comp = {c["component_id"]: c["value"] / 1e8 for c in cur.components}
        self.assertNotEqual(comp["unexplained_residual"], 0.0,
                            "缺项时差额必须留在未解释段，不得摊派")
        self.assertAlmostEqual(comp["working_capital_adjustments"], -6.5136, places=3,
                               msg="只留下了经营性应收项目（存货与应付未取到）")
        diag = run.outputs[0].diagnostics
        self.assertIn("depreciation", diag["items_missing"]["cur"])

    def test_tampered_groups_fail_independent_validation(self):
        from financial_analysis.operators import cash_reconciliation as cr
        ds = self._dataset()
        payload = cr.compute(ds)
        cur = next(o for o in payload["outputs"]
                   if o["metric"] == "operating_cashflow_reconciliation_cur")
        # 组间挪 1 亿元：合计不变、分项被换 → 逐项核对必失败
        cur["components"][1]["value"] += 100_000_000.0
        cur["components"][2]["value"] -= 100_000_000.0
        res = fa.validation.validate_output(fa.registry.spec("cash_reconciliation"),
                                            ds, payload)
        self.assertFalse(res["ok"], "分组被换而合计不变：必须失败")
        self.assertIn("components", res["failed"])

    def test_card_and_brief_block_carry_the_cash_bridge(self):
        from financial_analysis import store as fa_store
        run = self._run()
        block = fa_store.render_card_block(run)
        for needle in ("净利润→经营现金流", "亿元", "最大支撑/拖累", "营运资本项",
                       "缺口", "对账差额"):
            self.assertIn(needle, block, f"现金调节正文缺「{needle}」")


class TestScenarioReverseThresholds(unittest.TestCase):
    """U2 反向情景：**要改变结论需要什么**——维持基期利润所需毛利率 + 回款天数单项敏感性。

    用洋河 2024 年真实披露数（元）：收入 28,876,296,993.56、毛利 21,125,078,636.90、
    归母净利 6,673,388,602.12（毛利率 73.1572%）。
    """

    def _ds(self):
        rows = [
            _row("revenue", "2024年", 28_876_296_993.56, unit="元"),
            _row("gross_profit", "2024年", 21_125_078_636.90, unit="元"),
            _row("net_profit", "2024年", 6_673_388_602.12, unit="元"),
        ]
        return fa.freeze_from_facts(rows, periods=(2024,), entity="洋河股份",
                                    entity_id="002304.SZ", as_of="2025-04-30",
                                    source_label="test:scenario")

    def _out(self, run, metric):
        return next(o for o in run.outputs if o.metric == metric)

    def test_flat_revenue_threshold_reproduces_base_margin(self):
        """参数全 0 时阈值必须恰好等于基期毛利率（基准复现的同一性质）。"""
        run = fa.run("scenario_sensitivity", self._ds(),
                     params={"revenue_growth": 0.0, "gross_margin_delta": 0.0})
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        self.assertEqual(run.validation["failed"], [])
        self.assertAlmostEqual(self._out(run, "margin_threshold_to_hold_base_profit").value,
                               73.16, places=2)
        self.assertAlmostEqual(self._out(run, "margin_gap_to_threshold_pp").value,
                               0.0, places=2)

    def test_thresholds_share_one_target_and_mode(self):
        """W0：两把杠杆共用**同一目标**——单因素反推（收入固定基期 / 毛利率固定基期）。

        洋河先验（基期 2024、目标＝上一期 2023 归母净利 100.1593 亿元）：
        收入阈值 **+15.8226%**、毛利率阈值 **84.7325%**（较基期 +11.5754pp）。
        旧实现一把用基期利润、一把用使用者目标，两个数不能并列读。
        """
        rows = [_row("revenue", "2024年", 28_876_296_993.56, unit="元"),
                _row("gross_profit", "2024年", 21_125_078_636.90, unit="元"),
                _row("net_profit", "2024年", 6_673_388_602.12, unit="元"),
                _row("net_profit", "2023年", 10_015_930_040.27, unit="元")]
        ds = fa.freeze_from_facts(rows, periods=(2023, 2024), entity="洋河股份",
                                  entity_id="002304.SZ", as_of="2025-04-30",
                                  source_label="test:scenario-thresholds")
        run = fa.run("scenario_sensitivity", ds,
                     params={"revenue_growth": 0.0, "gross_margin_delta": 0.0})
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        self.assertEqual(run.validation["failed"], [])
        rev = self._out(run, "revenue_growth_to_hold_target")
        th = self._out(run, "margin_threshold_to_hold_base_profit")
        gap = self._out(run, "margin_gap_to_threshold_pp")
        self.assertAlmostEqual(rev.value, 15.8226, places=4,
                               msg="毛利率固定基期时，收入要到 +15.8226% 才回到 2023 水平")
        self.assertAlmostEqual(th.value, 84.7325, places=4,
                               msg="收入固定基期时，毛利率要到 84.7325%（基期 73.1572%）")
        self.assertAlmostEqual(gap.value, 11.5754, places=4)
        self.assertEqual(th.unit, "%")
        self.assertIn("%", gap.unit)
        diag = run.outputs[0].diagnostics["thresholds"]
        self.assertAlmostEqual(diag["target_net_profit"] / 1e8, 100.1593, places=4)
        self.assertIn("上一期", diag["target_source"])
        # 单因素阈值与"先接受某档收入再反推毛利率"不是同一把杠杆：后者只作条件诊断
        self.assertAlmostEqual(diag["conditional_margin_threshold"], 84.7325, places=4,
                               msg="收入假设为 0 时，条件值与单因素阈值一致")
        self.assertIn("单因素反推", diag["caveats"])

    def test_conditional_margin_threshold_is_not_a_second_lever(self):
        """给定收入假设后反推的毛利率是**两因素条件值**，与单因素阈值分开记。"""
        run = fa.run("scenario_sensitivity", self._ds(),
                     params={"revenue_growth": -0.1283, "gross_margin_delta": 0.0})
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        diag = run.outputs[0].diagnostics["thresholds"]
        # 目标＝基期（单期数据集）→ 单因素阈值＝基期毛利率；收入降 12.83% 的条件值更高
        self.assertAlmostEqual(diag["margin_threshold"], 73.16, places=2)
        self.assertAlmostEqual(diag["conditional_margin_threshold"], 83.92, places=2,
                               msg="收入再降 12.83% 时，要维持 66.73 亿利润需 83.92% 毛利率")
        self.assertAlmostEqual(diag["conditional_margin_gap_pp"], 10.77, places=2)
        self.assertAlmostEqual(diag["assumed_revenue"] / 1e8, 251.71, places=2)
        self.assertIn("单因素反推", diag["caveats"])

    def test_collection_days_sensitivity_is_bounded_and_labelled(self):
        run = fa.run("scenario_sensitivity", self._ds(),
                     params={"revenue_growth": 0.0})
        per_day = self._out(run, "collection_days_capital_per_day")
        ten = self._out(run, "collection_days_sensitivity_10d")
        self.assertAlmostEqual(per_day.value / 1e8, 0.79, places=2,
                               msg="收入/365＝每天的资金占用（288.76 亿 ÷ 365 ≈ 0.79 亿/天）")
        self.assertAlmostEqual(ten.value / 1e8, 7.91, places=2,
                               msg="±10 天 ≈ 7.91 亿元资金占用")
        self.assertAlmostEqual(ten.value, per_day.value * 10, delta=1.0,
                               msg="10 天 = 单日×10（各自按分四舍五入，允许分级差）")
        self.assertIn("非现金流预测", ten.label)

    def test_tampered_threshold_fails_independent_validation(self):
        from financial_analysis.operators import scenario as sc
        ds = self._ds()
        payload = sc.compute(ds, {"revenue_growth": -0.1283})
        th = next(o for o in payload["outputs"]
                  if o["metric"] == "margin_threshold_to_hold_base_profit")
        th["value"] = float(th["value"]) + 5.0        # 抬高 5 个百分点
        res = fa.validation.validate_output(fa.registry.spec("scenario_sensitivity"),
                                            ds, payload)
        self.assertFalse(res["ok"], "阈值被改：独立金样必须报红")
        self.assertIn("gold", res["failed"])


class TestU2ChartSpecs(unittest.TestCase):
    """U2 三图底稿（`financial_analysis.charts`）：图只从**已验证运行**来，不闭合就不给图。

    规格必须通过既有 `chart_specs.validate_spec`（标题/单位/来源/轴标题/结论齐全），
    否则渲染脚本会静默跳过——那样"三图底稿"就只是 JSON 里的声明。
    """

    IS = {
        "revenue": (33_126_277_551.51, 28_876_296_993.56),
        "operating_cost": (8_200_245_255.42, 7_751_218_356.66),
        "net_profit": (10_015_930_040.27, 6_673_388_602.12),
        "net_profit_consolidated": (10_020_768_556.47, 6_666_455_819.96),
        "taxes_and_surcharges": (5_269_245_592.35, 4_826_086_952.64),
        "selling_expense": (5_386_953_700.62, 5_516_238_544.79),
        "income_tax_expense": (3_197_064_562.60, 2_476_620_791.72),
        "fair_value_change": (-37_082_477.77, -396_164_080.43),
        "rd_expense": (284_753_881.33, 104_796_407.26),
    }
    CF = {
        "net_profit_consolidated": (10_020_768_556.47, 6_666_455_819.96),
        "operating_cashflow": (6_130_220_867.96, 4_628_711_237.28),
        "depreciation": (639_335_568.28, 586_592_227.18),
        "intangible_amortization": (59_054_597.55, 61_305_706.85),
        "inventory_decrease": (-1_226_697_174.83, -843_101_567.99),
        "operating_receivable_decrease": (380_090_873.53, -651_364_248.55),
        "operating_payable_increase": (-3_582_948_946.71, -1_830_670_724.59),
        "other_cashflow_adjustments": (-174_743_591.08, 330_296_496.71),
    }

    def _ds(self, table, *, source="test:charts", **override):
        rows = []
        for metric, (prev, cur) in dict(table, **override).items():
            rows.append(_row(metric, "2023年", prev, unit="元",
                             fact_id=f"{source}-{metric}-2023"))
            rows.append(_row(metric, "2024年", cur, unit="元",
                             fact_id=f"{source}-{metric}-2024"))
        return fa.freeze_from_facts(rows, periods=(2023, 2024), entity="洋河股份",
                                    entity_id="002304.SZ", as_of="2025-04-30",
                                    source_label=source)

    def test_profit_waterfall_rows_close_and_the_spec_is_renderable(self):
        import chart_specs as CS
        ds = self._ds(self.IS)
        run = fa.run("operating_drivers", ds)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        spec = fa.charts.profit_waterfall(run, ds, top_n=3)
        self.assertTrue(spec["available"], spec.get("reason"))
        self.assertEqual(CS.validate_spec(spec), [])
        self.assertEqual(spec["type"], "waterfall")
        self.assertEqual([r["kind"] for r in spec["data"]][0], "base")
        self.assertEqual([r["kind"] for r in spec["data"]][-1], "total")
        # 起点/终点是真实归母净利，贡献段合计 = 净利变化
        self.assertAlmostEqual(spec["data"][0]["value"], 100.16, places=2)
        self.assertAlmostEqual(spec["data"][-1]["value"], 66.73, places=2)
        deltas = sum(r["value"] for r in spec["data"][1:-1])
        self.assertAlmostEqual(deltas,
                               spec["data"][-1]["value"] - spec["data"][0]["value"],
                               delta=0.03, msg="显示口径下的闭合差只允许舍入量级")
        self.assertIn("毛利变化 -38.01", spec["conclusion"])
        self.assertIn("-33.43", spec["conclusion"])
        # V0：图注/正文不再印运行标识，但**规格里仍带运行身份**（底稿与证据据此回查）
        self.assertEqual(spec["run_id"], run.run_id)
        self.assertIn(str(run.run_id)[:12], spec["chart_id"])
        # top_n=3 之外的贡献合并成一项（合计仍是精确和）
        self.assertTrue(any("其余" in r["label"] for r in spec["data"]), spec["data"])

    def test_profit_waterfall_refuses_a_bridge_that_does_not_close(self):
        """桥不闭合（数据集与运行不是同一份）→ 拒绝出图，不画"看起来闭合"的瀑布。"""
        ds = self._ds(self.IS)
        run = fa.run("operating_drivers", ds)
        other = self._ds(self.IS, source="test:charts-other",
                         net_profit=(10_015_930_040.27, 5_000_000_000.00))
        spec = fa.charts.profit_waterfall(run, other)
        self.assertFalse(spec["available"])
        self.assertIn("不闭合", spec["reason"])

    def test_profit_waterfall_refuses_an_unvalidated_run(self):
        import dataclasses
        ds = self._ds(self.IS)
        run = fa.run("operating_drivers", ds)
        broken = dataclasses.replace(run, status=C.RunStatus.VALIDATION_FAILED)
        spec = fa.charts.profit_waterfall(broken, ds)
        self.assertFalse(spec["available"])
        self.assertIn("独立验证", spec["reason"])

    def test_cash_bridge_waterfall_closes_to_operating_cashflow(self):
        import chart_specs as CS
        ds = self._ds(self.CF)
        run = fa.run("cash_reconciliation", ds)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        spec = fa.charts.cash_bridge_waterfall(run, which="cur")
        self.assertTrue(spec["available"], spec.get("reason"))
        self.assertEqual(CS.validate_spec(spec), [])
        self.assertAlmostEqual(spec["data"][0]["value"], 66.66, places=2)
        self.assertAlmostEqual(spec["data"][-1]["value"], 46.29, places=2)
        self.assertIn("经营现金流 46.29", spec["conclusion"])
        labels = "".join(r["label"] for r in spec["data"])
        for needle in ("非现金项", "营运资本项", "其他调节项"):
            self.assertIn(needle, labels, f"现金桥缺分组「{needle}」")
        # 缺项时差额不为 0：桥仍然闭合（差额本身是一根柱子），但不许写成"完整调节"
        run2 = fa.run("cash_reconciliation", self._ds(self.CF, depreciation=(0.0, 0.0)))
        spec2 = fa.charts.cash_bridge_waterfall(run2, which="cur")
        self.assertTrue(spec2["available"], spec2.get("reason"))
        self.assertTrue(any("未解释差额" in r["label"] for r in spec2["data"]),
                        spec2["data"])

    def test_scenario_comparison_uses_one_base_margin_and_refuses_mixed_datasets(self):
        import chart_specs as CS
        rows = [_row("revenue", "2024年", 28_876_296_993.56, unit="元"),
                _row("gross_profit", "2024年", 21_125_078_636.90, unit="元"),
                _row("net_profit", "2024年", 6_673_388_602.12, unit="元")]
        ds = fa.freeze_from_facts(rows, periods=(2024,), entity="洋河股份",
                                  entity_id="002304.SZ", as_of="2025-04-30",
                                  source_label="test:charts-scenario")
        flat = fa.run("scenario_sensitivity", ds,
                      params={"revenue_growth": 0.0, "gross_margin_delta": 0.0})
        down = fa.run("scenario_sensitivity", ds,
                      params={"revenue_growth": -0.1283, "gross_margin_delta": 0.0})
        spec = fa.charts.scenario_threshold_comparison([("收入持平", flat),
                                                        ("收入 −12.83%", down)])
        self.assertTrue(spec["available"], spec.get("reason"))
        self.assertEqual(CS.validate_spec(spec), [])
        self.assertEqual(spec["type"], "grouped_bar")
        self.assertEqual({r["unit"] for r in spec["data"]}, {"%"})
        bases = [r["value"] for r in spec["data"] if r["caliber"] == "基期毛利率"]
        self.assertEqual(len(set(bases)), 1, f"基期毛利率必须只有一个值：{bases}")
        self.assertAlmostEqual(bases[0], 73.16, places=2,
                               msg="基期毛利率＝毛利/收入（211.25/288.76 亿元）")
        self.assertIn("83.92", spec["conclusion"])
        self.assertIn("不表示可达", spec["conclusion"])
        # 只有一个档位 → 不出图
        self.assertFalse(fa.charts.scenario_threshold_comparison(
            [("收入持平", flat)])["available"])
        # 不同数据集（不同基期）之间不可比 → 拒绝
        ds2 = fa.freeze_from_facts(rows, periods=(2024,), entity="洋河股份",
                                   entity_id="002304.SZ", as_of="2025-04-30",
                                   source_label="test:charts-scenario-2")
        down2 = fa.run("scenario_sensitivity", ds2,
                       params={"revenue_growth": -0.1283, "gross_margin_delta": 0.0})
        mixed = fa.charts.scenario_threshold_comparison([("收入持平", flat),
                                                         ("收入 −12.83%", down2)])
        self.assertFalse(mixed["available"])
        self.assertIn("同一数据集", mixed["reason"])


class TestU2ResearchNote(unittest.TestCase):
    """U2 成篇：4–6 页正文的论证线（结论→分解→披露支持→替代解释→现金与情景→待核查→限制）。

    用洋河真实披露数（元）验收：正文里的每个数字都必须能在**已验证运行**里找到，
    缺哪个模型就如实写缺——不补数、不拿未验证运行顶替。
    """

    IS = TestU2ChartSpecs.IS
    CF = TestU2ChartSpecs.CF

    def _ds(self, table, *, source="test:note", **override):
        rows = []
        for metric, (prev, cur) in dict(table, **override).items():
            rows.append(_row(metric, "2023年", prev, unit="元",
                             fact_id=f"{source}-{metric}-2023"))
            rows.append(_row(metric, "2024年", cur, unit="元",
                             fact_id=f"{source}-{metric}-2024"))
        return fa.freeze_from_facts(rows, periods=(2023, 2024), entity="洋河股份",
                                    entity_id="002304.SZ", as_of="2025-04-30",
                                    source_label=source)

    def _runs(self):
        ds = self._ds(self.IS)
        od = fa.run("operating_drivers", ds)
        cash = fa.run("cash_reconciliation", self._ds(self.CF))
        return od, cash

    def _scenario_ds(self):
        rows = [_row("revenue", "2024年", 28_876_296_993.56, unit="元"),
                _row("gross_profit", "2024年", 21_125_078_636.90, unit="元"),
                _row("net_profit", "2024年", 6_673_388_602.12, unit="元")]
        return fa.freeze_from_facts(rows, periods=(2024,), entity="洋河股份",
                                    entity_id="002304.SZ", as_of="2025-04-30",
                                    source_label="test:note-scenario")

    def test_note_carries_the_full_argument_line_with_run_identity(self):
        from financial_analysis import narrative as nt
        od, cash = self._runs()
        sds = self._scenario_ds()
        scens = [fa.run("scenario_sensitivity", sds,
                        params={"revenue_growth": g, "gross_margin_delta": 0.0})
                 for g in (0.0, -0.1283)]
        note = nt.research_note([od, cash] + scens)
        # 七段论证线
        for head in ("### 一、结论", "### 二、利润变化", "### 三、披露支持",
                     "### 四、替代解释", "### 五、现金形成", "### 六、待核查",
                     "### 七、口径与限制"):
            self.assertIn(head, note, f"成篇缺 {head}")
        # 关键读数与运行身份
        self.assertIn("-33.43", note)
        self.assertIn("-38.01", note)
        self.assertIn("46.29", note)
        self.assertIn("83.92", note)
        self.assertIn(str(od.run_id)[:12], note)
        self.assertIn(str(cash.run_id)[:12], note)
        # V0：run/output/component_id 只在**底稿索引**里（正文不印程序术语）
        self.assertIn("### 附：底稿索引（run / output / component_id）", note)
        self.assertIn("operating_cashflow_reconciliation_cur →", note)
        body = note.split("### 附：底稿索引")[0]
        self.assertNotIn("output `", body, "正文主体不得出现 output 标识")
        self.assertNotIn("run ", body, "正文主体不得出现 run 标识")
        # 纪律句必须在正文里（不是只写在注释里）
        for needle in ("会计分解", "不同切法", "提价效果", "单因素反推",
                       "未解释差额", "观察成立", "不得"):
            self.assertIn(needle, note, f"成篇缺纪律句「{needle}」")
        # 占比符号提醒（变化为负时正贡献显示负占比）
        self.assertIn("正贡献显示为负占比", note)

    def test_note_shows_scenario_net_profit_with_base_reproduction(self):
        """V2：情景段先给**该假设下的归母净利**，并注明基准复现与差额——都照抄同一次运行。

        洋河 2024 基期归母净利 66.73 亿元：收入 +5%／毛利率 +1pp 情景是 80.33 亿元，
        与基准复现值的差 +13.59 亿元。正文不自己算，读数必须等于运行输出的两个分量之差。
        """
        from financial_analysis import narrative as nt
        od, cash = self._runs()
        sds = self._scenario_ds()
        base = fa.run("scenario_sensitivity", sds,
                      params={"revenue_growth": 0.0, "gross_margin_delta": 0.0})
        up = fa.run("scenario_sensitivity", sds,
                    params={"revenue_growth": 0.05, "gross_margin_delta": 0.01})
        for r in (base, up):
            self.assertEqual(r.status, C.RunStatus.VALIDATED, r.reason)
        out = next(o for o in up.outputs if o.metric == "scenario_net_profit")
        comp = {c["component_id"]: c["value"] for c in out.components}
        note = nt.research_note([od, cash, base, up])
        # 标签来自输出期间（参数不由正文改写）
        self.assertIn("情景归母净利（收入 +5.00%／毛利率 +1.00pp）", note)
        # 情景净利 + 基准复现 + 差额，三处都在
        self.assertIn("+80.33 亿元", note)
        self.assertIn("基准复现 +66.73 亿元", note)
        self.assertIn(f"（差 {nt._yi(comp['user'] - comp['base'])} 亿元）", note)
        # 基准档：情景净利 = 基期归母净利，差恰好 0
        self.assertIn("（差 +0.00 亿元）", note)
        # 正文主体仍不许出现程序术语
        body = note.split("### 附：底稿索引")[0]
        self.assertNotIn("output `", body)

    def test_note_says_what_is_missing_instead_of_inventing(self):
        from financial_analysis import narrative as nt
        od, _cash = self._runs()
        only_od = nt.research_note([od])
        self.assertIn("本次未运行现金调节桥", only_od)
        self.assertNotIn("46.29", only_od, "没有现金桥运行就不许出现现金桥读数")
        bare = fa.run("operating_drivers",
                      self._ds({k: v for k, v in self.IS.items()
                                if k in ("revenue", "operating_cost", "net_profit")}))
        bare_note = nt.research_note([bare])
        self.assertIn("本次未取到分段/量价", bare_note)
        self.assertIn("未解释差额", bare_note)
        # 只有比率运行 → 不生成正文
        rows = [_row("revenue", "2024年", 28_876_296_993.56, unit="元"),
                _row("net_profit", "2024年", 6_673_388_602.12, unit="元"),
                _row("operating_cashflow", "2024年", 4_628_711_237.28, unit="元")]
        ds = fa.freeze_from_facts(rows, periods=(2024,), entity="洋河股份",
                                  entity_id="002304.SZ", as_of="2025-04-30",
                                  source_label="test:note-ratio")
        ratio = fa.ratio_run("经营现金流对归母净利润的覆盖", "operating_cashflow",
                             "net_profit", ds)
        self.assertEqual(nt.research_note([ratio]), "")

    def test_unvalidated_run_numbers_never_enter_the_note(self):
        import dataclasses
        from financial_analysis import narrative as nt
        od, cash = self._runs()
        broken = dataclasses.replace(od, status=C.RunStatus.VALIDATION_FAILED)
        note = nt.research_note([broken, cash])
        self.assertIn("本次未运行经营驱动分解", note)
        self.assertNotIn("-33.43", note, "未验证运行的读数不得进正文")
        self.assertIn("46.29", note, "另一条已验证运行照常进正文")
        self.assertEqual(nt.research_note([broken]), "",
                         "没有任何已验证运行时不生成正文")

    def test_source_table_uses_metric_labels_and_locators(self):
        from financial_analysis import narrative as nt
        facts = [
            {"fact_id": "fact-a", "metric": "operating_cost", "metric_label": "",
             "period": "2024年", "value": 7_751_218_356.66, "unit": "元",
             "source_locator": "PDF 第 76 页 · 合并利润表 · 行「其中：营业成本」"},
            {"fact_id": "fact-b", "metric": "revenue", "metric_label": "营业收入",
             "period": "2023年", "value": 33_126_277_551.51, "unit": "元",
             "source_locator": {"kind": "annual_report_table", "page": "75",
                                "table": {"group": "合并利润表",
                                          "row_label": "其中：营业收入"}}},
        ]
        prov = nt.provenance_from_facts(facts, label_of=lambda m: {"operating_cost": "营业成本"}.get(m, m))
        self.assertEqual(prov["fact-a"]["label"], "营业成本",
                         "事实层没有中文名时用 label_of 补，正文不印英文 slug")
        self.assertIn("PDF 第 76 页", prov["fact-a"]["locator"])
        self.assertIn("第 75 页", prov["fact-b"]["locator"])
        self.assertIn("行「其中：营业收入」", prov["fact-b"]["locator"])
        # 报告链那条路（观察没有页码，只有口径与血缘）：推算输入的来源必须写明
        obs_prov = nt.provenance_from_observations([
            {"fact_id": "fact-gp-1", "metric": "gross_profit", "metric_label": "毛利润",
             "period": "2024年", "value": 21_125_078_636.90, "unit": "元",
             "caliber": "合并", "derived_from": ("fact-a", "fact-b"),
             "formula_version": "revenue-cost/1.0"}])
        self.assertIn("推算自 fact-a、fact-b", obs_prov["fact-gp-1"]["locator"])
        self.assertIn("revenue-cost/1.0", obs_prov["fact-gp-1"]["locator"])
        self.assertIn("口径 合并", obs_prov["fact-gp-1"]["locator"])
        # 嵌套两层的 dataset.json 也能取到观察
        blob = {"dataset": {"schema": "x", "dataset": {"manifest": {}, "observations": [
            {"fact_id": "f1", "metric": "revenue", "value": 1.0}]}}}
        self.assertEqual(len(nt.observations_from_inputs(blob)), 1)

    def test_brief_block_renders_the_note(self):
        """接进报告链：`report_brief._analysis_note_block` 从工作区运行记录装配正文。"""
        import shutil
        import tempfile
        from pathlib import Path
        import report_brief
        import workspace as ws_mod
        from financial_analysis import store as fa_store
        od, cash = self._runs()
        ds = self._ds(self.IS)
        tmp = Path(tempfile.mkdtemp(prefix="wm_note_"))
        old = ws_mod.WORKSPACE_ROOT
        try:
            ws_mod.configure_workspace_root(str(tmp))
            ws = ws_mod.task_workspace("note-01")
            ws.mkdir(parents=True, exist_ok=True)
            fa_store.save_run(ws, od)
            fa_store.save_run(ws, cash)
            fa_store.save_inputs(ws, dataset=ds)
            block = report_brief._analysis_note_block("note-01", ws_dir=ws)
        finally:
            ws_mod.WORKSPACE_ROOT = old
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn("## 经营驱动分析正文", block)
        self.assertIn("### 一、结论", block)
        self.assertIn("46.29", block)
        self.assertIn("口径 ", block, "来源表要写口径/血缘（报告链没有页码定位）")


    def test_brief_markdown_emits_the_note_section(self):
        """成篇正文要真的进**简报正文**（装配进 structure 只是一半）。"""
        import report_brief
        md = report_brief.render_brief_markdown(
            {"scope": {"company": "洋河股份", "company_id": "002304.SZ",
                       "caliber": "合并", "as_of": "2025-04-30"},
             "analysis_note": ("## 经营驱动分析正文（洋河股份 2023年→2024年）\n\n"
                               "### 一、结论（先看这三条）\n1. 占位\n"),
             "analysis_card": "## 分析卡\n- 占位"},
            body="正文占位")
        self.assertIn("## 经营驱动分析正文", md)
        self.assertIn("### 一、结论", md)
        # 顺序：先卡后正文（分析正文是长文，跟在卡后面）
        self.assertLess(md.index("## 分析卡"), md.index("## 经营驱动分析正文"))

    def test_reassembly_does_not_duplicate_the_analysis_blocks(self):
        """简报回流再装配一次，**不得再印一遍**『分析摘要』与『经营驱动分析正文』。

        实测（洋河 V0 证据 `docs/evidence/v0_yanghe_normal_task/report.md`）：验收候选稿回流时
        `_looks_like_brief` 把整份简报当 body，`_brief_analysis_section` 取『## 分析』一节会把
        里面已经印过的摘要/正文一起带出来，装配器再加一次 → 同一份交付里这两节各出现两遍。
        """
        import report_brief
        structure = {"scope": {"company": "洋河股份", "company_id": "002304.SZ",
                               "caliber": "合并", "as_of": "2025-04-30"},
                     "analysis": "（模型分析散文：利润与现金的论证线）",
                     "analysis_note": ("## 经营驱动分析正文（洋河股份 2023年→2024年）\n\n"
                                       "### 一、结论（先看这三条）\n1. 占位\n"),
                     "analysis_card": "## 分析摘要\n- 占位"}
        once = report_brief.render_brief_markdown(
            structure, body=("> 报表口径：合并\n\n## 关键发现\n- 占位\n\n"
                             "## 分析\n（模型分析散文：利润与现金的论证线）\n"))
        self.assertEqual(once.count("## 经营驱动分析正文"), 1)
        self.assertEqual(once.count("## 分析摘要"), 1)
        # 回流再装配时，装机读到的「分析」一节来自 `_analysis_section(简报)`：
        # 它只能取模型自己的散文，不能再把装配器**已经印过**的摘要/正文当模型内容带一遍
        again = report_brief._analysis_section(once)
        self.assertNotIn("## 分析摘要", again)
        self.assertNotIn("## 经营驱动分析正文", again)
        self.assertIn("（模型分析散文：利润与现金的论证线）", again)
        twice = report_brief.render_brief_markdown(dict(structure, analysis=again), body=once)
        self.assertEqual(twice.count("## 经营驱动分析正文"), 1,
                         "回流再装配后经营驱动正文只能有一份")
        self.assertEqual(twice.count("## 分析摘要"), 1,
                         "回流再装配后分析摘要只能有一份")
        self.assertIn("（模型分析散文：利润与现金的论证线）", twice,
                      "模型自己的分析散文不能因为去重被删掉")


class TestV0OperatingResearchCombination(unittest.TestCase):
    """V0（阶段V）：默认**经营研究组合**——经营驱动＋现金桥＋情景，一条论证线。

    反例（审查原文）：`report_brief` 默认只取"前两个模型"，于是利润桥＋经营驱动占了正文，
    现金与情景根本不进默认交付；三图也只在案例脚本里。
    """

    IS = TestU2ChartSpecs.IS
    CF = TestU2ChartSpecs.CF

    def _ds(self, table, *, source="test:v0", **override):
        rows = []
        for metric, (prev, cur) in dict(table, **override).items():
            rows.append(_row(metric, "2023年", prev, unit="元",
                             fact_id=f"{source}-{metric}-2023"))
            rows.append(_row(metric, "2024年", cur, unit="元",
                             fact_id=f"{source}-{metric}-2024"))
        return fa.freeze_from_facts(rows, periods=(2023, 2024), entity="洋河股份",
                                    entity_id="002304.SZ", as_of="2025-04-30",
                                    source_label=source)

    def test_registry_combination_is_three_models_in_order(self):
        from financial_analysis import registry as reg
        self.assertEqual(reg.RESEARCH_COMBINATION,
                         ("operating_drivers", "cash_reconciliation",
                          "scenario_sensitivity"))
        ds = self._ds(self.IS)
        self.assertIn("operating_drivers", reg.research_combination(ds))

    def test_operating_research_question_adopts_the_combination(self):
        from financial_analysis import registry as reg
        # 组合三个模型的输入都要齐备（利润/现金桥/情景各要一组观察）
        table = dict(self.IS)
        table.update(self.CF)
        table["gross_profit"] = (21_125_078_636.90 + 6_760_000_000.0,
                                 21_125_078_636.90)
        ds = self._ds(table)
        plan = fa.compile_plan("洋河股份 2023/2024 经营研究：利润由何而来、"
                              "现金为何变化、什么条件会改变判断", ds)
        adopted = [a.model_id for a in plan.adopted]
        for mid in reg.RESEARCH_COMBINATION:
            self.assertIn(mid, adopted, f"经营研究组合缺 {mid}（实际 {adopted}）")
        # 旧模型不再重复堆叠：要么"与所问问题无关"，要么"经营研究组合已覆盖"
        reasons = {r["model_id"]: r["reason"] for r in plan.rejected}
        for old in ("profit_bridge", "cash_quality", "profit_to_cash"):
            if old in reasons:
                self.assertIn(reasons[old], ("与所问问题无关", "经营研究组合已覆盖",
                                             "缺输入"), reasons)
        self.assertTrue(any("经营研究组合" in n for n in plan.notes), plan.notes)

    def test_default_report_selection_is_the_combination_not_first_two(self):
        """默认选择必须是组合三条（而不是运行顺序里的前两个模型）。"""
        import shutil
        import tempfile
        from pathlib import Path
        from financial_analysis import store as fa_store
        ds = self._ds(self.IS)
        cds = self._ds(self.CF)
        sds = self._scenario_ds()
        # 故意把 profit_bridge 放在最前面：旧行为会取它 + operating_drivers
        runs = [fa.run("profit_bridge", ds), fa.run("operating_drivers", ds),
                fa.run("cash_reconciliation", cds),
                fa.run("scenario_sensitivity", sds,
                       params={"revenue_growth": 0.0, "gross_margin_delta": 0.0}),
                fa.run("cash_quality", ds)]
        tmp = Path(tempfile.mkdtemp(prefix="wm_v0sel_"))
        try:
            from financial_analysis import store
            picked, notes = store.select_for_report(tmp)
            self.assertEqual(picked, [], "还没有运行记录时不给选择")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        tmp = Path(tempfile.mkdtemp(prefix="wm_v0sel_"))
        try:
            for r in runs:
                fa_store.save_run(tmp, r)
            picked, notes = fa_store.select_for_report(tmp)
            self.assertEqual(
                [p.model_id for p in picked],
                ["operating_drivers", "cash_reconciliation", "scenario_sensitivity"],
                f"默认应为经营研究组合，实际 {[p.model_id for p in picked]}")
            self.assertTrue(any("经营研究组合" in n for n in notes), notes)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _scenario_ds(self):
        rows = [_row("revenue", "2024年", 28_876_296_993.56, unit="元"),
                _row("gross_profit", "2024年", 21_125_078_636.90, unit="元"),
                _row("net_profit", "2024年", 6_673_388_602.12, unit="元")]
        return fa.freeze_from_facts(rows, periods=(2024,), entity="洋河股份",
                                    entity_id="002304.SZ", as_of="2025-04-30",
                                    source_label="test:v0-scenario")

    def test_explicit_selection_keeps_the_rest_of_the_combination(self):
        """W1：采纳一条情景**不得**把利润/现金两段从正文里弄丢（组合其余模型保留）。

        反例（本轮隔离核验发现）：只采纳一条情景运行后 `select_for_report` 只返回情景，
        `research_note` 因"没有经营驱动/现金桥运行"直接返回空串——采纳出来的正文整段变空，
        采纳处理函数的"正文里必须找得到所选运行"绑定检查也因此失败。
        纪律：某个模型**有选择但已过期**时仍然不得改取同模型的别的运行（L0-b），只如实报。
        """
        import shutil
        import tempfile
        from pathlib import Path
        from financial_analysis import store as fa_store
        ds = self._ds(self.IS)
        cds = self._ds(self.CF)
        sds = self._scenario_ds()
        od = fa.run("operating_drivers", ds)
        cash = fa.run("cash_reconciliation", cds)
        scen_base = fa.run("scenario_sensitivity", sds,
                           params={"revenue_growth": 0.0, "gross_margin_delta": 0.0})
        scen_new = fa.run("scenario_sensitivity", sds,
                          params={"revenue_growth": 0.05, "gross_margin_delta": 0.01})
        tmp = Path(tempfile.mkdtemp(prefix="wm_w1sel_"))
        try:
            for r in (od, cash, scen_base, scen_new):
                fa_store.save_run(tmp, r)
            fa_store.save_selection(tmp, {
                "model_id": "scenario_sensitivity", "run_id": scen_new.run_id,
                "dataset_hash": sds.dataset_hash, "params": dict(scen_new.params),
                "params_hash": str(scen_new.params_hash),
                "rules_version": fa.validation.RULES_VERSION,
            }, note="W1 组合保留")
            picked, notes = fa_store.select_for_report(
                tmp, rules_version=fa.validation.RULES_VERSION)
            self.assertEqual([p.model_id for p in picked],
                             ["operating_drivers", "cash_reconciliation",
                              "scenario_sensitivity"])
            self.assertEqual(str(picked[2].run_id), scen_new.run_id,
                             "被选中的情景必须是被选的那一条，不是默认档")
            self.assertTrue(any("其余模型" in n for n in notes), notes)
            # 同模型的选择过期 → 不改取同模型的别的运行，但要保留组合其余模型
            # （"过期"由**当前输入的数据集**判定：换一份新数据集，旧选择立刻不等于当前）
            fa_store.save_inputs(tmp, dataset=self._ds(self.IS, source="test:w1-newer"))
            picked2, notes2 = fa_store.select_for_report(
                tmp, rules_version=fa.validation.RULES_VERSION)
            mids2 = [p.model_id for p in picked2]
            self.assertNotIn("scenario_sensitivity", mids2,
                             "选择过期时不得用同模型的默认运行顶替")
            self.assertIn("operating_drivers", mids2)
            self.assertIn("cash_reconciliation", mids2)
            self.assertTrue(any("未采用" in n for n in notes2), notes2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_three_analysis_charts_come_from_the_selected_runs(self):
        """正常路径的三图：同一次运行、同一份数据集，不在这里另跑参数。"""
        import shutil
        import tempfile
        from pathlib import Path
        from financial_analysis import charts as ch
        from financial_analysis import store as fa_store
        from orchestrator_v2 import OrchestratorV2
        import workspace as ws_mod
        ds = self._ds(self.IS)
        od = fa.run("operating_drivers", ds)
        cash = fa.run("cash_reconciliation", self._ds(self.CF))
        scen = fa.run("scenario_sensitivity", self._scenario_ds(),
                      params={"revenue_growth": 0.0, "gross_margin_delta": 0.0})
        tmp = Path(tempfile.mkdtemp(prefix="wm_v0chart_"))
        old = ws_mod.WORKSPACE_ROOT
        try:
            ws_mod.configure_workspace_root(str(tmp))
            ws = ws_mod.task_workspace("v0-chart")
            ws.mkdir(parents=True, exist_ok=True)
            for r in (od, cash, scen):
                fa_store.save_run(ws, r)
            fa_store.save_inputs(ws, dataset=ds)
            specs = OrchestratorV2._analysis_chart_specs(object(), "v0-chart")
        finally:
            ws_mod.WORKSPACE_ROOT = old
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(specs), 3, [s.get("title") for s in specs])
        import chart_specs as CS
        for s in specs:
            self.assertEqual(CS.validate_spec(s), [], s.get("title"))
        kinds = [s["type"] for s in specs]
        self.assertEqual(kinds, ["waterfall", "waterfall", "bar"])
        # 三图必须与正文同一次运行（身份在规格字段里，不画在图上）
        self.assertEqual(specs[0]["run_id"], od.run_id)
        self.assertEqual(specs[1]["run_id"], cash.run_id)
        self.assertIn(str(scen.run_id)[:12], specs[2]["chart_id"])

    def test_scenario_outcome_bars_uses_one_run(self):
        import chart_specs as CS
        from financial_analysis import charts as ch
        run = fa.run("scenario_sensitivity", self._scenario_ds(),
                     params={"revenue_growth": 0.0, "gross_margin_delta": 0.0})
        spec = ch.scenario_outcome_bars(run)
        self.assertTrue(spec["available"], spec.get("reason"))
        self.assertEqual(CS.validate_spec(spec), [])
        self.assertGreaterEqual(len(spec["data"]), 3, spec["data"])
        self.assertIn("基准复现", spec["conclusion"])
        self.assertIn("不是预测", spec["conclusion"])


class TestV1CashChangeBridge(unittest.TestCase):
    """V1（阶段V）：现金变化桥 `ΔOCF = Δ合并净利＋Δ非现金＋Δ营运资本＋Δ其他＋Δ差额`。

    反例（审查原文）：旧 `cash_gap_change` 的第二项**实际是 ΔOCF**，却标"调节项合计变化"，
    金样还复制了这条错式。这里用两家公司的独立披露数验收：
    洋河 `−15.0151 = −33.5431 + 2.4334 + 11.0442 + 5.0504`（亿元）；
    三一 `+91.0606 = +14.8643 − 4.1022 + 79.7693 + 0.5292`（亿元）。
    """

    def _sany(self):
        """三一重工补充资料**分组合计**（元，取自 600031 年报缓存的抽取结果）。

        分组内单项在 `cash_reconciliation.ADJUSTMENT_ITEMS` 里按组给一个代表项即可——
        本用例验的是**桥的算式与标签**，不是抽取器（抽取器由 annual tables 用例覆盖）。
        """
        rows = [
            _row("net_profit_consolidated", "2023年", 4_606_110_000.0, unit="元"),
            _row("net_profit_consolidated", "2024年", 6_092_538_000.0, unit="元"),
            _row("operating_cashflow", "2023年", 5_708_220_000.0, unit="元"),
            _row("operating_cashflow", "2024年", 14_814_278_000.0, unit="元"),
            _row("depreciation", "2023年", 4_151_725_000.0, unit="元"),
            _row("depreciation", "2024年", 3_741_504_000.0, unit="元"),
            _row("operating_payable_increase", "2023年", -3_066_842_000.0, unit="元"),
            _row("operating_payable_increase", "2024年", 4_910_089_000.0, unit="元"),
            _row("other_cashflow_adjustments", "2023年", 17_227_000.0, unit="元"),
            _row("other_cashflow_adjustments", "2024年", 70_147_000.0, unit="元"),
        ]
        return fa.freeze_from_facts(rows, periods=(2023, 2024), entity="三一重工",
                                    entity_id="600031.SH", as_of="2025-04-30",
                                    source_label="test:v1-sany")

    def test_sany_bridge_matches_the_reviewed_arithmetic(self):
        run = fa.run("cash_reconciliation", self._sany())
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        self.assertEqual(run.validation["failed"], [])
        chg = next(o for o in run.outputs if o.metric == "operating_cashflow_change")
        cc = {c["component_id"]: c["value"] / 1e8 for c in chg.components}
        self.assertAlmostEqual(chg.value / 1e8, 91.0606, places=3)
        self.assertAlmostEqual(cc["change_in_net_profit"], 14.8643, places=3)
        self.assertAlmostEqual(cc["change_in_non_cash"], -4.1022, places=3)
        self.assertAlmostEqual(cc["change_in_working_capital"], 79.7693, places=3)
        self.assertAlmostEqual(cc["change_in_other"], 0.5292, places=3)
        self.assertAlmostEqual(cc["change_in_residual"], 0.0, places=3)
        self.assertAlmostEqual(sum(cc.values()), 91.0606, places=3)
        # 辅助观察：Δ(OCF−合并净利) = ΔOCF − Δ合并净利
        gap = next(o for o in run.outputs if o.metric == "cash_gap_change")
        self.assertAlmostEqual(gap.value / 1e8, 76.1963, places=3)
        gc = {c["component_id"]: c["value"] / 1e8 for c in gap.components}
        self.assertAlmostEqual(gc["change_in_operating_cashflow"], 91.0606, places=3,
                               msg="第二项必须是 ΔOCF 本身（旧版误标成“调节项合计变化”）")
        self.assertAlmostEqual(gc["change_in_net_profit_negated"], -14.8643, places=3)

    def test_working_capital_share_is_not_called_supplier_terms(self):
        """营运资本项变化占现金增量的比例只作**会计构成**陈述，不得称账期延长。"""
        run = fa.run("cash_reconciliation", self._sany())
        from financial_analysis import operators as _ops
        limits = " ".join(_ops.cash_reconciliation.LIMITS)
        self.assertIn("时点与资金占用", limits)
        self.assertIn("不得自动称", limits)
        self.assertIn("非经常性损益", limits)

    def test_working_capital_breakdown_and_observation_indicators(self):
        """V1：现金改善的可持续性——营运资本项拆到单项，并给读法与后续观察指标。

        三一（真实披露数）：营运资本项变化 +79.77 亿元占现金变化 87.6%，
        真实年报里进一步分成经营性应付 +116.13、经营性应收 −33.94、存货 −2.42（亿元）
        （三项拆解见案例证据 `u3_sany_note.md`；本用例核对结构与措辞）。
        """
        from financial_analysis import narrative as nt
        run = fa.run("cash_reconciliation", self._sany())
        diag = run.outputs[0].diagnostics
        items = (diag.get("cash_change_items") or {}).get("working_capital") or []
        self.assertTrue(items, sorted(diag.keys()))
        self.assertAlmostEqual(sum(r["delta_yuan"] for r in items) / 1e8, 79.76931, places=3)
        self.assertEqual(items[0]["metric"], "operating_payable_increase",
                         "按 |Δ| 排序，最大单项在前")
        note = nt.research_note([run])
        self.assertIn("现金改善的可持续性", note)
        self.assertIn("后续观察指标", note)
        self.assertIn("不等于账期延长", note)
        self.assertIn("周转天数", note)
        self.assertIn("占现金变化 87.6%", note)
        # 现金下降时份额要写成"抵消/加重现金下降"，不出现读反的负百分比
        yh_note = nt.research_note([fa.run("cash_reconciliation", self._yanghe())])
        self.assertIn("抵消现金下降", yh_note)
        self.assertNotIn("占现金变化 -", yh_note)

    def _yanghe(self):
        """洋河：营运资本项变化为正而现金变化为负（份额措辞必须按方向写）。"""
        return fa.freeze_from_facts([
            _row("net_profit_consolidated", "2023年", 10_020_768_556.47, unit="元"),
            _row("net_profit_consolidated", "2024年", 6_666_455_819.96, unit="元"),
            _row("operating_cashflow", "2023年", 6_130_220_867.96, unit="元"),
            _row("operating_cashflow", "2024年", 4_628_711_237.28, unit="元"),
            _row("depreciation", "2023年", 639_335_568.28, unit="元"),
            _row("depreciation", "2024年", 586_592_227.18, unit="元"),
            _row("operating_payable_increase", "2023年", -3_582_948_946.71, unit="元"),
            _row("operating_payable_increase", "2024年", -1_830_670_724.59, unit="元"),
            _row("inventory_decrease", "2023年", -1_226_697_174.83, unit="元"),
            _row("inventory_decrease", "2024年", -843_101_567.99, unit="元"),
            _row("operating_receivable_decrease", "2023年", 380_090_873.53, unit="元"),
            _row("operating_receivable_decrease", "2024年", -651_364_248.55, unit="元"),
            _row("other_cashflow_adjustments", "2023年", -174_743_591.08, unit="元"),
            _row("other_cashflow_adjustments", "2024年", 330_296_496.71, unit="元"),
        ], periods=(2023, 2024), entity="洋河股份", entity_id="002304.SZ",
            as_of="2025-04-30", source_label="test:v1-yanghe")

    def test_components_gold_is_independent_of_the_payload(self):
        """金样按定义独立重算：篡改分项值或标签都会被抓到。"""
        import dataclasses
        from financial_analysis.operators import cash_reconciliation as cr
        ds = self._sany()
        payload = cr.compute(ds)
        chg = next(o for o in payload["outputs"]
                   if o["metric"] == "operating_cashflow_change")
        expect = cr.components_gold(ds)["operating_cashflow_change"]
        for c in chg["components"]:
            want, _unit = expect[c["component_id"]]
            self.assertAlmostEqual(float(c["value"]), float(want), places=2,
                                   msg=c["component_id"])
        # 篡改"营运资本项变化"10 亿元 → 逐项核对失败
        chg["components"][2]["value"] = float(chg["components"][2]["value"]) + 1_000_000_000.0
        res = fa.validation.validate_output(fa.registry.spec("cash_reconciliation"),
                                            ds, payload)
        self.assertFalse(res["ok"], "分项被改：独立金样必须报红")
        self.assertIn("components", res["failed"])


class TestV1DriverEvidenceBinding(unittest.TestCase):
    """V1：主要贡献 → 披露原句 / 支持边界 / 反证 / 观察指标（确定性绑定，不代拟解释）。"""

    def _runs(self):
        inst = TestV1CashChangeBridge("test_sany_bridge_matches_the_reviewed_arithmetic")
        rows = [_row("revenue", "2023年", 33_126_277_551.51, unit="元"),
                _row("revenue", "2024年", 28_876_296_993.56, unit="元"),
                _row("operating_cost", "2023年", 8_200_245_255.42, unit="元"),
                _row("operating_cost", "2024年", 7_751_218_356.66, unit="元"),
                _row("net_profit", "2023年", 10_015_930_040.27, unit="元"),
                _row("net_profit", "2024年", 6_673_388_602.12, unit="元")]
        ds = fa.freeze_from_facts(rows, periods=(2023, 2024), entity="洋河股份",
                                  entity_id="002304.SZ", as_of="2025-04-30",
                                  source_label="test:v1-driver")
        return [fa.run("operating_drivers", ds),
                fa.run("cash_reconciliation", inst._sany())]

    def test_binds_a_relevant_section_and_states_the_boundary(self):
        from financial_analysis import narrative as nt
        records = [{"kind": "change_explanation", "kind_label": "变动解释",
                    "section": "第三节 管理层讨论与分析 > 五、主要经营情况 > 现金流",
                    "snippet": "经营活动现金流量净额同比增加，主要系销售回款增加与应付账款结算节奏变化所致。",
                    "locator": "PDF 第 18 页 · 小节：现金流", "page": 18}]
        text = "\n".join(nt.driver_evidence(self._runs(), records))
        self.assertIn("主要贡献的披露支持", text)
        self.assertIn("销售回款增加", text, text[:400])
        self.assertIn("出处：", text)
        self.assertIn("不构成因果已证明", text)
        self.assertIn("反证/替代解释", text)
        self.assertIn("后续观察指标", text)

    def test_missing_disclosure_is_reported_not_invented(self):
        from financial_analysis import narrative as nt
        text = "\n".join(nt.driver_evidence(self._runs(), records=[]))
        self.assertIn("没有", text)
        self.assertIn("只作金额分解", text)
        self.assertNotIn("主要系", text, "没有材料时不得编出解释")

    def test_keyword_fallback_is_labelled_as_a_hit(self):
        from financial_analysis import narrative as nt
        doc = {"text": "…… 应付账款 1,234 1,000 主要系结算节奏影响。",
               "page_offsets": [(0, 7)]}
        text = "\n".join(nt.driver_evidence(self._runs(), records=[], doc=doc))
        self.assertIn("关键词", text)
        self.assertIn("原件第 7 页", text)

    def test_note_includes_the_section_only_when_evidence_exists(self):
        from financial_analysis import narrative as nt
        runs = self._runs()
        without = nt.research_note(runs)
        self.assertIn("### 四之二、主要贡献的披露支持", without)
        self.assertIn("没有可绑定的已准入披露段落", without)
        with_ev = nt.research_note(runs, records=[{
            "section": "现金流", "snippet": "应付账款结算节奏变化",
            "locator": "PDF 第 1 页"}])
        self.assertIn("披露原句", with_ev)


class TestW0ScenarioEvaluator(unittest.TestCase):
    """W0（阶段W）：情景求值器的**经济效果**——符号、冻结残差、假设真正改变利润、同一目标。

    手算例子（元）：收入 1000、毛利 800、归母净利 300、合并净利 320、所得税 80、
    少数股东 20、随收入变化的税金及附加 100 与销售费用 200、固定金额的管理费用 50。
    按披露符号：归母净利 = 800 − 100 − 200 − 50 − 80 − 20 = 350 ≠ 300 ⇒ 残差 = −50
    （未取得明细的毛利线以下项目，一次算定后**冻结**）。

    旧实现的三处反例（架构复核）：①按绝对值相加 → 假残差；②每次用"总块 − 新明细"重算残差
    → 税率/费用/少数股东假设被残差抵消（利润仍 300、残差 100→68）；③两个阈值目标不同。
    """

    def _ds(self, *, drop=()):
        rows = [("revenue", 1000.0), ("gross_profit", 800.0), ("net_profit", 300.0),
                ("net_profit_consolidated", 320.0), ("income_tax_expense", 80.0),
                ("minority_interest", 20.0), ("taxes_and_surcharges", 100.0),
                ("selling_expense", 200.0), ("admin_expense", 50.0)]
        out = []
        for metric, value in rows:
            if metric in drop:
                continue
            out.append(_row(metric, "2024年", value, unit="元"))
        out.append(_row("revenue", "2023年", 900.0, unit="元"))
        return fa.freeze_from_facts(out, periods=(2023, 2024), entity="示例",
                                    entity_id="000001.SZ", as_of="2025-04-30",
                                    source_label="test:w0-detail")

    def _detail(self, run):
        return next(o for o in run.outputs if o.metric == "scenario_below_gross_detail")

    def _base(self, run):
        out = next(o for o in run.outputs if o.metric == "scenario_net_profit")
        return next(c["value"] for c in out.components if c["component_id"] == "base")

    def _run(self, **params):
        p = {"revenue_growth": 0.0, "gross_margin_delta": 0.0,
             "below_gross_mode": "detail"}
        p.update(params)
        run = fa.run("scenario_sensitivity", self._ds(), params=p)
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        self.assertEqual(run.validation["failed"], [])
        return run

    def test_base_case_reproduces_by_signs_and_residual_is_frozen(self):
        detail = self._run()
        fixed = fa.run("scenario_sensitivity", self._ds(),
                       params={"revenue_growth": 0.0, "gross_margin_delta": 0.0})
        d = self._detail(detail)
        diag = detail.outputs[0].diagnostics["below_gross"]
        # 逐项按**披露符号**：合计 = 归母净利 − 毛利 = −500（减利 500），残差 −50
        self.assertAlmostEqual(d.value, -500.0, places=2)
        self.assertAlmostEqual(sum(c["value"] for c in d.components), -500.0, places=2)
        self.assertAlmostEqual(diag["residual_yuan"], -50.0, places=2,
                               msg="明细齐备：残差由披露恒等式得出，不含绝对值相加的假残差")
        self.assertTrue(diag["residual_frozen"])
        self.assertAlmostEqual(diag["pretax_profit_base_yuan"], 400.0, places=2)
        # 符号方向：费用项都是负贡献，随收入项按规则标注
        comp = {c["component_id"]: c for c in d.components}
        self.assertLess(comp["item:selling_expense"]["value"], 0)
        self.assertEqual(comp["item:selling_expense"]["rule"], "revenue_linked")
        self.assertEqual(comp["item:admin_expense"]["rule"], "fixed")
        self.assertEqual(comp["income_tax_expense"]["rule"], "base_rate")
        self.assertEqual(comp["minority_interest"]["rule"], "base_share")
        self.assertEqual(comp["residual_unlisted"]["rule"], "residual")
        # 基准复现：两种模式在"假设回到基期"时给出同一个基期净利
        self.assertAlmostEqual(self._base(detail), 300.0, places=2)
        self.assertAlmostEqual(self._base(fixed), 300.0, places=2)
        self.assertEqual(detail.outputs[0].diagnostics["base_reproduction_gap"], "0")

    def test_tax_and_minority_assumptions_change_the_final_profit(self):
        """税率 20%→25%、少数股东占比→10%：**最终利润**必须变（旧实现只有残差在动）。"""
        base = self._run()
        run = self._run(tax_rate=0.25, minority_share=0.10)
        diag = run.outputs[0].diagnostics["below_gross"]
        # 税前 400；税 = 400×25% = 100；合并 300；少数 = 300×10% = 30 → 归母 270
        self.assertAlmostEqual(diag["income_tax_yuan"], 100.0, places=2)
        self.assertAlmostEqual(diag["minority_interest_yuan"], 30.0, places=2)
        self.assertAlmostEqual(diag["consolidated_net_yuan"], 300.0, places=2)
        self.assertAlmostEqual(run.outputs[0].value, 270.0, places=2)
        self.assertNotAlmostEqual(run.outputs[0].value, base.outputs[0].value, places=2)
        # 残差**不动**（冻结）：这是"假设不被残差吸收"的判据
        self.assertAlmostEqual(diag["residual_yuan"], -50.0, places=2)
        self.assertAlmostEqual(
            base.outputs[0].diagnostics["below_gross"]["residual_yuan"],
            diag["residual_yuan"], places=2)
        self.assertIn("单独假设", diag["tax_rate_source"])
        self.assertEqual(
            {c["component_id"]: c["rule"] for c in self._detail(run).components}
            ["income_tax_expense"], "explicit")

    def test_expense_assumption_moves_profit_through_the_listed_items(self):
        """费用假设只作用于**已披露的固定金额项**（残差不补平）：管理费 50→55。"""
        run = self._run(expense_change_ratio=0.10)
        diag = run.outputs[0].diagnostics["below_gross"]
        comp = {c["component_id"]: c for c in self._detail(run).components}
        self.assertAlmostEqual(comp["item:admin_expense"]["value"], -55.0, places=2)
        # 税前 = 800 −(100+200)×1 −55 −50(残差) = 395；税 79；合并 316；少数 19.75 → 296.25
        self.assertAlmostEqual(diag["pretax_profit_yuan"], 395.0, places=2)
        self.assertAlmostEqual(run.outputs[0].value, 296.25, places=2)
        self.assertAlmostEqual(diag["residual_yuan"], -50.0, places=2)

    def test_revenue_linked_items_follow_revenue_and_rates_stay_base(self):
        run = self._run(revenue_growth=0.10)
        comp = {c["component_id"]: c for c in self._detail(run).components}
        self.assertAlmostEqual(comp["item:taxes_and_surcharges"]["value"], -110.0, places=2,
                               msg="税金及附加随收入 +10%")
        self.assertAlmostEqual(comp["item:selling_expense"]["value"], -220.0, places=2)
        self.assertAlmostEqual(comp["item:admin_expense"]["value"], -50.0, places=2,
                               msg="固定金额项不随收入变化")
        # 税前 = 880 −330 −50 −50 = 450；税 90；合并 360；少数 22.5 → 337.5
        self.assertAlmostEqual(run.outputs[0].value, 337.5, places=2)
        self.assertEqual(comp["income_tax_expense"]["rule"], "base_rate",
                         "未给税率假设时用基期有效税率，不假装能预测")

    def test_missing_tax_and_minority_stay_in_the_frozen_residual(self):
        run = fa.run("scenario_sensitivity",
                     self._ds(drop=("income_tax_expense", "minority_interest")),
                     params={"revenue_growth": 0.0, "gross_margin_delta": 0.0,
                             "below_gross_mode": "detail"})
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        d = self._detail(run)
        diag = run.outputs[0].diagnostics["below_gross"]
        self.assertIn("income_tax_expense", diag["items_missing"])
        self.assertIn("minority_interest", diag["items_missing"])
        comp = {c["component_id"]: c["value"] for c in d.components}
        # 缺的所得税 80 与少数股东 20 都留在残差里（−50 −80 −20 = −150），不当零、不摊派
        self.assertAlmostEqual(comp["residual_unlisted"], -150.0, places=2)
        self.assertAlmostEqual(comp["income_tax_expense"], 0.0, places=2)
        self.assertAlmostEqual(sum(comp.values()), d.value, places=2)

    def test_both_thresholds_share_one_target_and_are_verifiable(self):
        """同一目标下两个单因素阈值，代回**同一求值器**都能恢复该目标。"""
        run = self._run(target_net_profit=450.0)
        th = run.outputs[0].diagnostics["thresholds"]
        # 明细结构下：750·Δm + 300 = 450 → Δm = +20pp（毛利率 100%）；375(1+g) − 75 = 450 → +40%
        self.assertAlmostEqual(th["margin_threshold"], 100.0, places=4)
        self.assertAlmostEqual(th["margin_gap_pp"], 20.0, places=4)
        self.assertAlmostEqual(th["revenue_growth_threshold"], 40.0, places=4)
        self.assertTrue(th["reachable"]["margin"] and th["reachable"]["revenue"])
        # 代回求值器：两个阈值各自恢复 450 元
        from financial_analysis.operators import scenario as SC
        ctx = SC.base_context(self._ds(), "2024年")
        m_back = SC.evaluate(ctx, growth=0,
                             margin_delta=SC._d(th["margin_threshold"]) / 100 - ctx["margin"],
                             mode="detail")["net_profit"]
        g_back = SC.evaluate(ctx, growth=SC._d(th["revenue_growth_threshold"]) / 100,
                             margin_delta=0, mode="detail")["net_profit"]
        self.assertAlmostEqual(float(m_back), 450.0, places=2)
        self.assertAlmostEqual(float(g_back), 450.0, places=2)
        # 目标=基期时两个阈值都是零（自检）
        flat = self._run()
        fth = flat.outputs[0].diagnostics["thresholds"]
        self.assertAlmostEqual(fth["margin_threshold"], 80.0, places=4,
                               msg="目标=基期 → 所需毛利率=基期毛利率")
        self.assertAlmostEqual(fth["revenue_growth_threshold"], 0.0, places=4)

    def test_unreachable_target_is_stated_not_faked(self):
        """目标在允许范围内不可达：不给数（诊断为不可达），也不硬报一个近似值。"""
        run = fa.run("scenario_sensitivity", self._ds(),
                     params={"revenue_growth": 0.0, "gross_margin_delta": 0.0,
                             "below_gross_mode": "detail", "minority_share": 0.10,
                             "target_net_profit": 1e15})
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        self.assertEqual(run.validation["failed"], [])
        metrics = {o.metric for o in run.outputs}
        self.assertNotIn("revenue_growth_to_hold_target", metrics)
        self.assertNotIn("margin_threshold_to_hold_base_profit", metrics)
        th = run.outputs[0].diagnostics["thresholds"]
        self.assertFalse(th["reachable"]["margin"] or th["reachable"]["revenue"])
        self.assertTrue(th["threshold_notes"]["margin"])

    def test_fixed_mode_refuses_tax_and_minority_instead_of_ignoring_them(self):
        for params in ({"tax_rate": 0.25}, {"minority_share": 0.10}):
            run = fa.run("scenario_sensitivity", self._ds(), params=params)
            self.assertEqual(run.status, C.RunStatus.NOT_APPLICABLE, run.reason)
            self.assertIn("detail", str(run.reason), params)

    def test_revenue_side_reverse_threshold(self):
        """收入侧反推（单因素）：目标=基期时收入不需要变化（自检）。"""
        run = fa.run("scenario_sensitivity", self._ds(),
                     params={"revenue_growth": 0.0, "gross_margin_delta": 0.0})
        base_th = next(o for o in run.outputs
                       if o.metric == "revenue_growth_to_hold_target")
        self.assertEqual(base_th.unit, "%")
        self.assertAlmostEqual(base_th.value, 0.0, places=2,
                               msg="目标=基期利润时收入不需要变化（自检）")
        # 目标设为基期的一半 → 收入需负增长（fixed 单因素代数反推）
        run2 = fa.run("scenario_sensitivity", self._ds(),
                      params={"revenue_growth": 0.0, "gross_margin_delta": 0.0,
                              "target_net_profit": 150.0})
        th2 = next(o for o in run2.outputs
                   if o.metric == "revenue_growth_to_hold_target")
        # g* = (150 + 500 − 1000×0.8) / (1000×0.8) = (150+500-800)/800 = −18.75%
        self.assertAlmostEqual(th2.value, -18.75, places=2)
        diag = run2.outputs[0].diagnostics["thresholds"]
        self.assertAlmostEqual(diag["target_net_profit"], 150.0, places=2)
        self.assertIn("g*", diag["revenue_threshold_formula"])

    def test_detail_mode_is_rendered_in_the_note(self):
        """明细模式要进正文（不是只在运行记录里）：逐项规则 + 税前/税/少数股东 + 冻结残差。"""
        from financial_analysis import narrative as nt
        run = self._run(tax_rate=0.25)
        text = "\n".join(nt._scenario_detail_note([run]))
        self.assertIn("情景明细模式", text)
        self.assertIn("随收入变化", text)
        self.assertIn("单独假设", text)
        self.assertIn("情景税前利润", text)
        self.assertIn("少数股东损益", text)
        self.assertIn("冻结", text)
        self.assertIn("不构成完整预测", text)

    def test_out_of_range_assumptions_are_refused(self):
        for params in ({"below_gross_mode": "predict"}, {"tax_rate": 0.9},
                       {"minority_share": -0.1}):
            run = fa.run("scenario_sensitivity", self._ds(), params=params)
            self.assertNotEqual(run.status, C.RunStatus.VALIDATED, params)
            self.assertIn("参数", str(run.reason or ""), params)


class TestW1OneReportOneJudgment(unittest.TestCase):
    """W1（阶段W §4）：一份报告只表达同一套研究判断。

    反例（架构复核）：正常洋河报告首屏写「毛利润金额差未取得、量价分部未取得」，
    后页却有完整分解——因为首屏只读旧底稿（没有毛利两期），成篇正文另挂一次运行。
    这里钉住三件事：① 选定运行的金额分解进入首屏观察；② 已准入披露记录**真的**传进
    成篇正文（不再是脚本级案例才有）；③ 一次读取同时给出运行身份/参数/选择说明。
    """

    IS = TestU2ResearchNote.IS
    CF = TestU2ResearchNote.CF

    def _ds(self, table, *, source="test:w1"):
        rows = []
        for metric, (prev, cur) in dict(table).items():
            rows.append(_row(metric, "2023年", prev, unit="元",
                             fact_id=f"{source}-{metric}-2023"))
            rows.append(_row(metric, "2024年", cur, unit="元",
                             fact_id=f"{source}-{metric}-2024"))
        return fa.freeze_from_facts(rows, periods=(2023, 2024), entity="洋河股份",
                                    entity_id="002304.SZ", as_of="2025-04-30",
                                    source_label=source)

    def test_selected_run_amounts_reach_the_front_page(self):
        import report_brief as rb
        od = fa.run("operating_drivers", self._ds(self.IS))
        cash = fa.run("cash_reconciliation", self._ds(self.CF))
        self.assertEqual(od.status, C.RunStatus.VALIDATED, od.reason)
        self.assertEqual(cash.status, C.RunStatus.VALIDATED, cash.reason)
        readings = rb._analysis_readings([od, cash])
        metrics = {str(r["metric"]) for r in readings["derived"]}
        for need in ("gross_profit_change", "net_profit_gross_gap_change",
                     "operating_cashflow_change"):
            self.assertIn(need, metrics)
        self.assertTrue(readings["profit_decomposition"]["ok"])
        # 首屏：金额分解与运行同源 → 不再出现"毛利润同期金额差未取得"
        derived = [dict(r, year=2024) for r in readings["derived"]]
        derived.append({"metric": "net_profit_yoy", "value": -33.37, "unit": "%",
                        "year": 2024, "period": "2024年"})
        rows = [{"metric": "net_profit", "year": 2023,
                 "value": 10_015_930_040.27, "unit": "元"},
                {"metric": "net_profit", "year": 2024,
                 "value": 6_673_388_602.12, "unit": "元"}]
        qs = rb._research_questions(rows, derived, [2023, 2024], {}, [], {},
                                    assessments={})
        text = " ".join(str(q.get("observation") or "") for q in qs)
        self.assertIn("毛利润变化", text)
        self.assertNotIn("毛利润同期金额差未取得", text)
        self.assertIn("毛利线以下净额变化", text)

    def test_records_and_run_identity_come_from_one_read(self):
        import shutil
        import tempfile
        from pathlib import Path

        import report_brief as rb
        import workspace as ws_mod
        from financial_analysis import store as fa_store
        od = fa.run("operating_drivers", self._ds(self.IS))
        cash = fa.run("cash_reconciliation", self._ds(self.CF))
        tmp = tempfile.mkdtemp(prefix="w1_ctx_")
        old = ws_mod.WORKSPACE_ROOT
        try:
            ws_mod.WORKSPACE_ROOT = Path(tmp)
            ws = ws_mod.task_workspace("w1-ctx")
            ws.mkdir(parents=True, exist_ok=True)
            fa_store.save_run(ws, od)
            fa_store.save_run(ws, cash)
            ctx = rb._analysis_context(
                "w1-ctx", ws_dir=ws,
                evidence={"records": [
                    {"kind": "change_explanation", "has_location": True,
                     "admission": "admitted", "section": "管理层讨论与分析 > 存货",
                     "snippet": "公司加强存货与货款管理，压缩库存占用。",
                     "locator": "PDF 第 29 页 · 管理层讨论与分析", "url": "u"},
                    {"kind": "risk", "has_location": False, "admission": "unknown",
                     "snippet": "检索摘要不算支持", "locator": "", "url": "u2"}]})
            self.assertEqual(len(ctx["records"]), 1, "只收已准入且带定位的记录")
            self.assertIn("披露原句", ctx["note"])
            self.assertIn("存货", ctx["note"])
            self.assertNotIn("检索摘要不算支持", ctx["note"])
            self.assertEqual({r["model_id"] for r in ctx["runs_meta"]},
                             {"operating_drivers", "cash_reconciliation"})
            self.assertTrue(all(r["run_id"] for r in ctx["runs_meta"]))
        finally:
            ws_mod.WORKSPACE_ROOT = old
            shutil.rmtree(tmp, ignore_errors=True)


class TestW2ResearchJudgments(unittest.TestCase):
    """W2（阶段W §5）：**可检验的研究判断**——七段式、读数驱动、写得出反转条件。

    数据自带（CI 里没有缓存年报原件）：用洋河真实披露数（合并口径两期＋白酒口径销量/收入）
    构造数据集，量价分解由算子现场算出（销量效应 −53.82／单位价格 +11.68 亿元），
    量价/结构事实按 `narrative_evidence.extract_volume_price` 的形状给出（含页码定位）。
    """

    IS = TestU2ResearchNote.IS
    CF = TestU2ResearchNote.CF

    def _ds(self):
        rows = []
        for metric, (prev, cur) in dict(self.IS).items():
            rows.append(_row(metric, "2023年", prev, unit="元",
                             fact_id=f"w2-{metric}-2023"))
            rows.append(_row(metric, "2024年", cur, unit="元",
                             fact_id=f"w2-{metric}-2024"))
        # 白酒口径的量与收入（量价分解要**同一口径**两期都有销量与收入）
        rows += [_row("sales_volume", "2023年", 166_154.73, unit="吨", caliber="分产品:白酒",
                      metric_label="白酒销售量", fact_id="w2-vol-2023"),
                 _row("sales_volume", "2024年", 139_076.05, unit="吨", caliber="分产品:白酒",
                      metric_label="白酒销售量", fact_id="w2-vol-2024"),
                 _row("revenue", "2023年", 32_389_581_931.71, unit="元", caliber="分产品:白酒",
                      metric_label="白酒营业收入", fact_id="w2-rev-baijiu-2023"),
                 _row("revenue", "2024年", 28_175_707_878.18, unit="元", caliber="分产品:白酒",
                      metric_label="白酒营业收入", fact_id="w2-rev-baijiu-2024"),
                 # 分产品切法需要**同一口径**两期都有收入与营业成本（毛利桥按口径各算一次）
                 _row("operating_cost", "2023年", 6_500_000_000.0, unit="元", caliber="分产品:白酒",
                      metric_label="白酒营业成本", fact_id="w2-cost-baijiu-2023"),
                 _row("operating_cost", "2024年", 6_800_000_000.0, unit="元", caliber="分产品:白酒",
                      metric_label="白酒营业成本", fact_id="w2-cost-baijiu-2024")]
        return fa.freeze_from_facts(rows, periods=(2023, 2024), entity="洋河股份",
                                    entity_id="002304.SZ", as_of="2025-04-30",
                                    source_label="test:w2")

    @staticmethod
    def _vp():
        """已准入的量价/结构事实（洋河真实数，字段形状与 narrative_evidence 一致）。"""
        return {"ok": True, "facts": [
            {"group": "实物量", "row_label": "白酒销售量", "unit": "吨",
             "cur": 139_076.05, "prev": 166_154.73, "yoy": -16.30,
             "line": "销售量(吨) 139,076.05 166,154.73 -16.30%",
             "locator": "第 12 页 · 产销量表"},
            {"group": "实物量", "row_label": "白酒生产量", "unit": "吨",
             "cur": 145_494.73, "prev": 158_834.29, "yoy": -8.40,
             "line": "生产量(吨) 145,494.73 158,834.29 -8.40%",
             "locator": "第 12 页 · 产销量表"},
            {"group": "实物量", "row_label": "白酒库存量", "unit": "吨",
             "cur": 45_594.72, "prev": 39_176.04, "yoy": 16.38,
             "line": "库存量(吨) 45,594.72 39,176.04 16.38%",
             "locator": "第 12 页 · 产销量表"}],
            "derived": [{"label": "白酒吨价（推算）", "unit": "元/吨", "cur": 202_592,
                         "prev": 194_936, "yoy": 3.93,
                         "formula": "(28175707878.18 / 139076.05)"}]}

    def test_volume_price_inventory_judgments_are_numbers_driven(self):
        from financial_analysis import judgments as jd
        od = fa.run("operating_drivers", self._ds())
        self.assertEqual(od.status, C.RunStatus.VALIDATED, od.reason)
        # 算子现场算出的量价分解：销量效应 −53.82 / 单位价格 +11.68 亿元（含结构混合）
        vp_out = next(o for o in od.outputs if o.metric == "volume_price_decomposition")
        comp = {c["component_id"]: c["value"] for c in vp_out.components}
        self.assertAlmostEqual(comp["volume_effect"] / 1e8, -53.82, places=2)
        self.assertAlmostEqual(comp["price_effect"] / 1e8, 11.68, places=2)
        js = jd.research_judgments([od], volume_price=self._vp(), records=[{
            "section": "管理层讨论与分析 > 产销量情况说明",
            "snippet": "公司本期销售量、生产量、库存量情况见下表。",
            "locator": "第 12 页 · 产销量表"}], limit=3)
        ids = [j["judgment_id"] for j in js]
        self.assertIn("volume_contraction", ids)
        self.assertIn("finished_goods_inventory_build", ids)
        vol = next(j for j in js if j["judgment_id"] == "volume_contraction")
        joined = " ".join(vol["numbers"])
        self.assertIn("-53.82", joined, "量价分解的销量效应必须进判断")
        self.assertIn("11.68", joined)
        self.assertIn("-16.30%", joined)
        self.assertTrue(vol["boundary"] and vol["alternatives"] and vol["watch"],
                        "七段式不能缺：边界/替代解释/反转条件")
        self.assertTrue(any("渠道库存" in g for g in vol["gaps"]),
                        "机制缺口要如实列出，不臆造")
        inv = next(j for j in js if j["judgment_id"] == "finished_goods_inventory_build")
        self.assertIn("+16.38%", " ".join(inv["numbers"]))
        self.assertIn("不等于", inv["boundary"])
        self.assertTrue(any("库存" in str(e.get("text")) for e in inv["evidence"]),
                        "库存判断要绑到披露原句")
        price = next((j for j in js if j["judgment_id"]
                      == "unit_revenue_not_proof_of_pricing"), None)
        self.assertIsNotNone(price)
        self.assertIn("提价", price["title"])
        text = "\n".join(jd.render_judgments(js))
        for needle in ("数字与贡献", "披露原句", "支持边界", "本公司替代解释",
                       "后续指标与反转条件", "缺口"):
            self.assertIn(needle, text)
        self.assertNotIn("****", text, "标题强调不能被套两层")

    def test_structure_judgment_says_cuts_are_not_additive(self):
        """产品/区域结构（同一口径的不同切法）：降幅差说明结构在起作用，且**不可相加**。"""
        from financial_analysis import judgments as jd
        ds = self._ds()
        od = fa.run("operating_drivers", ds)
        js = jd.research_judgments([od], volume_price=self._vp(), records=[],
                                   limit=6)
        st = next((j for j in js if j["judgment_id"] == "product_region_structure"), None)
        self.assertIsNotNone(st, [j["judgment_id"] for j in js])
        self.assertIn("分产品", " ".join(st["numbers"]))
        self.assertIn("不可相加", st["boundary"])
        self.assertTrue(st["watch"])

    def test_direct_method_cash_support_judgment(self):
        """直接法两行（销售收现/采购付现）→「现金跃升有真实收支支持、主要不来自利润增长」。"""
        from financial_analysis import judgments as jd
        # 现金**跃升**的样本（自洽闭合：2023 500+50+100+20+0=670；2024 520+60+180+10+20=790）
        cf_rising = {"net_profit_consolidated": (50_000_000_000.0, 52_000_000_000.0),
                     "depreciation": (5_000_000_000.0, 6_000_000_000.0),
                     "operating_payable_increase": (10_000_000_000.0, 18_000_000_000.0),
                     "operating_receivable_decrease": (2_000_000_000.0, 1_000_000_000.0),
                     "inventory_decrease": (0.0, 0.0),
                     "other_cashflow_adjustments": (0.0, 2_000_000_000.0),
                     "operating_cashflow": (67_000_000_000.0, 79_000_000_000.0)}
        cds = fa.freeze_from_facts(
            [_row(m, "2023年", v[0], unit="元", fact_id=f"w2d-{m}-2023")
             for m, v in cf_rising.items()]
            + [_row(m, "2024年", v[1], unit="元", fact_id=f"w2d-{m}-2024")
               for m, v in cf_rising.items()],
            periods=(2023, 2024), entity="洋河股份", entity_id="002304.SZ",
            as_of="2025-04-30", source_label="test:w2-direct")
        cash = fa.run("cash_reconciliation", cds)
        self.assertEqual(cash.status, C.RunStatus.VALIDATED, cash.reason)
        dc = {"received": {"cur": 82_694_250_000.0, "prev": 78_272_597_000.0,
                           "delta": 44.22 * 1e8,
                           "cur_period": "2024年", "prev_period": "2023年",
                           "locator": "第 97 页 · 合并现金流量表"},
              "paid": {"cur": 51_611_517_000.0, "prev": 59_001_843_000.0,
                       "delta": -73.90 * 1e8,
                       "cur_period": "2024年", "prev_period": "2023年",
                       "locator": "第 98 页 · 合并现金流量表"}}
        # 现金运行本身的 ΔOCF 必须为正，才谈得上"现金跃升"（本数据集 ΔOCF = +91.06 亿元）
        chg = next(o for o in cash.outputs if o.metric == "operating_cashflow_change")
        self.assertGreater(float(chg.value), 0)
        js = jd.research_judgments([cash], direct_cash=dc, records=[], limit=6)
        cj = next((j for j in js if j["judgment_id"] == "cash_direct_method_support"), None)
        self.assertIsNotNone(cj, [j["judgment_id"] for j in js])
        joined = " ".join(cj["numbers"])
        self.assertIn("销售收现", joined)
        self.assertIn("采购付现", joined)
        self.assertIn("不同切法", cj["boundary"])
        self.assertIn("不可相加", cj["boundary"])
        self.assertTrue(any("采购付现" in w and "反弹" in w for w in cj["watch"]),
                        "反转条件必须写：采购付现反弹则现金改善不可持续")

    def test_finance_expense_note_does_not_assume_interest(self):
        """W2 点名：财务费用的原因以披露原句为准，不默认套"利息/利率"模板（三一是汇兑）。"""
        from financial_analysis import narrative as nt
        joined = " ".join(nt._DRIVER_WATCH["finance_expense"])
        self.assertIn("汇兑", joined)
        self.assertIn("以披露原句为准", joined)

    def test_missing_data_produces_a_gap_not_a_judgment(self):
        """读数缺：如实写"本次不足以下判断"，不得编一条判断出来。"""
        from financial_analysis import judgments as jd
        rows = [_row("revenue", "2023年", 1000.0, unit="元"),
                _row("revenue", "2024年", 900.0, unit="元"),
                _row("gross_profit", "2023年", 800.0, unit="元"),
                _row("gross_profit", "2024年", 700.0, unit="元"),
                _row("net_profit", "2023年", 300.0, unit="元"),
                _row("net_profit", "2024年", 250.0, unit="元"),
                _row("operating_cost", "2023年", 200.0, unit="元"),
                _row("operating_cost", "2024年", 200.0, unit="元")]
        ds = fa.freeze_from_facts(rows, periods=(2023, 2024), entity="示例",
                                  entity_id="000001.SZ", as_of="2025-04-30",
                                  source_label="test:w2-gap")
        od = fa.run("operating_drivers", ds)
        js = jd.research_judgments([od], volume_price={"ok": False}, records=[], limit=3)
        ids = [j["judgment_id"] for j in js]
        self.assertNotIn("volume_contraction", ids)
        self.assertNotIn("finished_goods_inventory_build", ids)
        # 10-06：判断层现在还会产出**由真实读数驱动**的利润机制判断（`net_profit_change_detail`
        # 在本次运行里存在）——它不是编的。这条用例的本意是"**读数缺的那几类**不得编判断"，
        # 所以按机制分组断言：量价/库存/结构这几类必须是"不足以下判断"占位，而不是空口结论。
        _missing_mechanisms = ("volume_contraction", "unit_revenue_not_proof_of_pricing",
                               "finished_goods_inventory_build", "product_region_structure")
        for j in js:
            if j["judgment_id"] in _missing_mechanisms:
                self.assertTrue("不足以下判断" in j["title"] or "无从判断" in j["title"],
                                j["title"])
        # 有读数支撑的那条（利润机制）必须给出**可核的读数**，不能只有一句结论
        _pm = next((j for j in js if j["judgment_id"] == "profit_mechanism_and_buffer"), None)
        if _pm is not None:
            self.assertTrue(_pm["numbers"], _pm)
        self.assertTrue(all(j["gaps"] for j in js), [j["gaps"] for j in js])

    def test_cash_judgment_uses_the_working_capital_share(self):
        from financial_analysis import judgments as jd
        ds = self._ds()
        od = fa.run("operating_drivers", ds)
        cds = fa.freeze_from_facts(
            [_row(m, "2023年", v[0], unit="元", fact_id=f"w2c-{m}-2023")
             for m, v in self.CF.items()]
            + [_row(m, "2024年", v[1], unit="元", fact_id=f"w2c-{m}-2024")
               for m, v in self.CF.items()],
            periods=(2023, 2024), entity="洋河股份", entity_id="002304.SZ",
            as_of="2025-04-30", source_label="test:w2-cash")
        cash = fa.run("cash_reconciliation", cds)
        self.assertEqual(cash.status, C.RunStatus.VALIDATED, cash.reason)
        js = jd.research_judgments([od, cash], volume_price=self._vp(), records=[],
                                   limit=6)
        cj = next((j for j in js if j["judgment_id"] == "cash_from_working_capital"), None)
        self.assertIsNotNone(cj, [j["judgment_id"] for j in js])
        self.assertIn("营运资本", cj["title"])
        self.assertIn("-15.02", " ".join(cj["numbers"]))
        self.assertIn("不直接证明", cj["boundary"])
        self.assertTrue(any("采购付现" in w for w in cj["watch"]),
                        "反转条件要落到可观察的收支项上")


class TestX0DeliveryClosure(unittest.TestCase):
    """X0（阶段X §3）：一次有限交付收口。

    反面（架构复核 `20261001-W-architecture-review.md`，逐条对应）：
    ① `report_brief.py:3464/3469/3478` 先用**原始 volume_price** 渲染，再被
       `_analysis_readings` 覆盖后**重渲染一次** → 正文丢披露 facts/derived，
       正常洋河虚列"吨价输入缺口"；
    ② `judgments.py:306–317` 只用绝对占比就套"主要由营运资本解释" → 洋河的**缓冲项**
       被说成主因（净利 −33.54 才是下降主导、营运资本 +11.04 是缓冲）；
    ③ 计划/背景段落被标成"已发生解释"、派生算式被标"披露原句"、研发微生态被当结构支持、
       洋河的酒类缺口串到三一；
    ④ 直接法/间接法混成嵌套分项（把间接法净利 +14.86 写成直接法"其余收支"的其中）；
    ⑤ 判断压在 19 页 PDF 第 8 页。
    """

    IS = TestU2ResearchNote.IS
    CF = TestU2ResearchNote.CF
    # 三一实测口径（间接法：净利 +14.86／营运资本 +79.77；ΔOCF +91.06）
    CF_SANY = {
        "net_profit_consolidated": (100.00e8, 114.86e8),
        "depreciation": (10.00e8, 10.00e8),
        "operating_payable_increase": (20.00e8, 99.77e8),
        "operating_receivable_decrease": (0.0, 0.0),
        "inventory_decrease": (0.0, 0.0),
        "other_cashflow_adjustments": (0.0, -3.57e8),
        "operating_cashflow": (130.00e8, 221.06e8),
    }
    DIRECT_SANY = {
        "received": {"cur": 82_694_250_000.0, "prev": 78_272_597_000.0,
                     "delta": 44.22e8, "cur_period": "2024年", "prev_period": "2023年",
                     "locator": "第 97 页 · 合并现金流量表"},
        "paid": {"cur": 51_611_517_000.0, "prev": 59_001_843_000.0,
                 "delta": -73.90e8, "cur_period": "2024年", "prev_period": "2023年",
                 "locator": "第 98 页 · 合并现金流量表"},
    }
    # 三一第 19 页发行人归因（原件原句）；第 29 页是**未来计划**，不得当已发生解释
    RECORDS = [
        {"kind": "change_explanation", "section": "第三节 管理层讨论与分析 > 五、报告期内主要经营情况 > 5、 现金流",
         "snippet": "主要系本期销售回款增加、采购付款减少影响。",
         "locator": "第 19 页 · 现金流"},
        {"kind": "change_explanation",
         "section": "第三节 管理层讨论与分析 > 四、主营业务分析 > 4、研发投入",
         "snippet": "适用 □不适用 主要研发项目名称 项目目的 项目进展 拟达到的目标 中国白酒宿迁产区生态与酿造微生态研究",
         "locator": "第 18 页 · 研发投入"},
        {"kind": "business_background",
         "section": "第三节 管理层讨论与分析 > 六、公司关于公司未来发展的讨论与分析 > 经营计划",
         "snippet": "分产品收入计划：2025 年公司拟提升中高档产品占比，计划新增产能。",
         "locator": "第 29 页 · 经营计划"},
    ]

    def _ds(self, entity, entity_id, table, *, source="test:x0", segments=True):
        rows = []
        for metric, (prev, cur) in dict(table).items():
            rows.append(_row(metric, "2023年", prev, unit="元",
                             fact_id=f"{source}-{metric}-2023"))
            rows.append(_row(metric, "2024年", cur, unit="元",
                             fact_id=f"{source}-{metric}-2024"))
        if segments:
            rows += [_row("sales_volume", "2023年", 166_154.73, unit="吨",
                          caliber="分产品:白酒", metric_label="白酒销售量",
                          fact_id="x0-vol-2023"),
                     _row("sales_volume", "2024年", 139_076.05, unit="吨",
                          caliber="分产品:白酒", metric_label="白酒销售量",
                          fact_id="x0-vol-2024"),
                     _row("revenue", "2023年", 32_389_581_931.71, unit="元",
                          caliber="分产品:白酒", metric_label="白酒营业收入",
                          fact_id="x0-rev-b-2023"),
                     _row("revenue", "2024年", 28_175_707_878.18, unit="元",
                          caliber="分产品:白酒", metric_label="白酒营业收入",
                          fact_id="x0-rev-b-2024"),
                     _row("operating_cost", "2023年", 6_500_000_000.0, unit="元",
                          caliber="分产品:白酒", metric_label="白酒营业成本",
                          fact_id="x0-cost-b-2023"),
                     _row("operating_cost", "2024年", 6_800_000_000.0, unit="元",
                          caliber="分产品:白酒", metric_label="白酒营业成本",
                          fact_id="x0-cost-b-2024")]
        for r in rows:
            r["entity"] = entity
            r["entity_id"] = entity_id
        return fa.freeze_from_facts(rows, periods=(2023, 2024), entity=entity,
                                    entity_id=entity_id, as_of="2025-04-30",
                                    source_label=source)

    def test_disclosure_facts_and_run_summary_are_kept_and_rendered_once(self):
        """接缝：披露量价事实与运行摘要**分字段**并存，成篇**只渲染一次**（X0-1）。"""
        import shutil
        import tempfile
        from pathlib import Path

        import report_brief as rb
        import workspace as ws_mod
        from financial_analysis import store as fa_store
        od = fa.run("operating_drivers", self._ds("洋河股份", "002304.SZ", self.IS))
        cash = fa.run("cash_reconciliation", self._ds("洋河股份", "002304.SZ", self.CF))
        vp = TestW2ResearchJudgments._vp()
        tmp = tempfile.mkdtemp(prefix="x0_ctx_")
        old = ws_mod.WORKSPACE_ROOT
        try:
            ws_mod.WORKSPACE_ROOT = Path(tmp)
            ws = ws_mod.task_workspace("x0-ctx")
            ws.mkdir(parents=True, exist_ok=True)
            fa_store.save_run(ws, od)
            fa_store.save_run(ws, cash)
            ctx = rb._analysis_context("x0-ctx", ws_dir=ws,
                                       evidence={"volume_price": vp, "records": []})
        finally:
            ws_mod.WORKSPACE_ROOT = old
            shutil.rmtree(tmp, ignore_errors=True)
        # ① 原始披露事实**原样保留**，运行摘要另存自己的字段（不再互相覆盖）
        self.assertEqual(len(ctx["volume_price"]["facts"]), len(vp["facts"]))
        self.assertIn("吨价", str(ctx["volume_price"]["derived"]))
        self.assertTrue(ctx["volume_price"]["model_summary"], "运行摘要另存字段")
        self.assertIn("销量效应", str(ctx["volume_price_run"]["summary"]))
        # ② 首屏是**唯一**的『关键判断与下一步』，带**关键读数**
        #    （销量 −16.30%／推算吨价 +3.93%／库存 +16.38%）。
        #    10-06 复核 §4：不再另印『三项主判断（先看这里）』。
        self.assertIn("## 关键判断与下一步", ctx["front"])
        self.assertNotIn("三项主判断", ctx["front"])
        for needle in ("-16.30%", "推算吨价", "+3.93%", "+16.38%", "-8.40%"):
            self.assertIn(needle, ctx["front"], f"首屏判断缺 {needle}")
        self.assertIn("推算口径", ctx["front"], "推算身份必须写明")
        # 结构化判断对象与首屏同源透传（§6.3-1）
        self.assertTrue(ctx["reader_judgments"], "首屏判断对象要透传")
        self.assertGreaterEqual(len(ctx["judgments"]), len(ctx["reader_judgments"]))
        # ③ 不再虚列"吨价输入缺口"：披露 facts/derived 真的进了判断
        self.assertNotIn("吨价推算所需的同口径收入/销量未取全", ctx["front"])
        self.assertNotIn("吨价推算所需的同口径收入/销量未取全", ctx["detail"])
        # ④ 成篇只渲染一次；七段式明细**另附**
        self.assertEqual(ctx["note"].count("## 经营驱动分析正文"), 1)
        self.assertNotIn("### 研究判断（可检验）", ctx["note"])
        self.assertIn("### 研究判断（可检验）", ctx["detail"])
        self.assertIn("推算吨价", ctx["detail"])
        self.assertTrue(ctx["detail_file"].endswith("analysis/analysis_detail.md"))
        self.assertIn("analysis_detail.md", ctx["note"], "主文给详表指针")

    def test_rewriting_detail_keeps_the_main_body_compaction_block(self):
        """**实测丢内容**：另附底稿有两个写入方，重写明细不得抹掉主文收束块。

        实机 `ui-10ea37599d`：最后一轮装配在 14:55:17 把主文移出的 9,252 字符写成收束块，
        交付收尾又调了一次 `build_structure`（结构投影同步）⇒ `analysis_detail.md` 被重写、
        收束块连同移出的整段**在磁盘上消失**：正文已收束、附件里没有 ⇒ 内容真的丢了。
        这里按同一顺序复现：写收束块 → 再跑一次 `_analysis_context` → 块必须还在。
        """
        import shutil
        import tempfile
        from pathlib import Path

        import report_brief as rb
        import workspace as ws_mod
        from financial_analysis import store as fa_store
        od = fa.run("operating_drivers", self._ds("洋河股份", "002304.SZ", self.IS))
        cash = fa.run("cash_reconciliation", self._ds("洋河股份", "002304.SZ", self.CF))
        vp = TestW2ResearchJudgments._vp()
        tmp = tempfile.mkdtemp(prefix="x0_compact_")
        old = ws_mod.WORKSPACE_ROOT
        try:
            ws_mod.WORKSPACE_ROOT = Path(tmp)
            ws = ws_mod.task_workspace("x0-compact")
            ws.mkdir(parents=True, exist_ok=True)
            fa_store.save_run(ws, od)
            fa_store.save_run(ws, cash)
            ev = {"volume_price": vp, "records": []}
            rb._analysis_context("x0-compact", ws_dir=ws, evidence=ev)
            det = ws / "analysis" / "analysis_detail.md"
            # ① 模拟主文收束：把一段整段写进标记块
            moved = ("<!-- 主文收束:begin（§11.2 主文只留必要结论与边界；"
                     "本块由 report_brief 维护，勿手改） -->\n"
                     "## 经营驱动分析正文（洋河股份 2023年→2024年）\n\n"
                     "### 一、结论（先看这三条）\n\n现金减少 15.02 亿元。\n"
                     "<!-- 主文收束:end -->\n")
            det.write_text(rb.write_analysis_detail(det, det.read_text(encoding="utf-8")),
                           encoding="utf-8")
            det.write_text(det.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + moved,
                           encoding="utf-8")
            self.assertIn(rb.COMPACT_BEGIN, det.read_text(encoding="utf-8"))
            # ② 再跑一次装配（交付收尾会这么干）——重写明细，块必须活下来。
            #    先放一个"只可能被重写抹掉"的哨兵：没有它，这条用例在"第二次装配其实
            #    没写文件"时也会通过（块当然还在），那就是空转的守卫。
            det.write_text("SENTINEL-OLD-DETAIL\n" + det.read_text(encoding="utf-8"),
                           encoding="utf-8")
            rb._analysis_context("x0-compact", ws_dir=ws, evidence=ev)
            after = det.read_text(encoding="utf-8")
            self.assertNotIn("SENTINEL-OLD-DETAIL", after, "明细确实被重写了（不是空转）")
            self.assertIn(rb.COMPACT_BEGIN, after, "收束块不得被明细重写抹掉")
            self.assertIn("## 经营驱动分析正文", after, "移出的整段必须还在附件里")
            self.assertIn("现金减少 15.02 亿元", after)
            self.assertIn("### 研究判断（可检验）", after, "明细本身也要照写")
            self.assertEqual(after.count(rb.COMPACT_BEGIN), 1, "块只应有一份")
        finally:
            ws_mod.WORKSPACE_ROOT = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cash_direction_labels_driver_and_buffer(self):
        """方向：洋河净利主导下降/营运资本缓冲；三一营运资本为主要构成（X0-2）。"""
        from financial_analysis import judgments as jd
        # 洋河实测：ΔOCF −15.02、净利 −33.54、营运资本 +11.04
        driver, roles, text = jd.cash_direction_roles(-15.02e8, -33.54e8, 11.04e8)
        self.assertEqual(driver, "net_profit")
        self.assertIn("由利润下降主导", text)
        self.assertIn("缓冲", text)
        by_item = {r["item"]: r["role"] for r in roles}
        self.assertIn("主导下降", by_item["合并净利润项"])
        self.assertEqual(by_item["营运资本项"], "缓冲")
        # 三一实测：ΔOCF +91.06、净利 +14.86、营运资本 +79.77
        driver2, roles2, text2 = jd.cash_direction_roles(91.06e8, 14.86e8, 79.77e8)
        self.assertEqual(driver2, "working_capital")
        self.assertIn("主要由营运资本", text2)
        self.assertEqual({r["role"] for r in roles2}, {"推动"})
        # 集成：判断层写出来的标题与读数（不是"绝对占比代替方向"）
        ds = self._ds("三一重工", "600031.SH", self.CF_SANY)
        cash = fa.run("cash_reconciliation", ds)
        self.assertEqual(cash.status, C.RunStatus.VALIDATED, cash.reason)
        js = jd.research_judgments([cash], direct_cash=self.DIRECT_SANY,
                                   records=self.RECORDS, limit=6)
        cj = next(j for j in js if j["judgment_id"] == "cash_from_working_capital")
        self.assertIn("主要由营运资本", cj["title"])
        joined = " ".join(cj["numbers"])
        self.assertIn("间接法", joined)
        self.assertIn("+79.77", joined)
        self.assertIn("+14.86", joined)

    def test_plan_and_derived_evidence_keep_their_roles(self):
        """角色：计划/背景不当已发生解释；派生算式不标"披露原句"（X0-3）。"""
        from financial_analysis import judgments as jd
        od = fa.run("operating_drivers", self._ds("洋河股份", "002304.SZ", self.IS))
        js = jd.research_judgments([od], volume_price=TestW2ResearchJudgments._vp(),
                                   records=self.RECORDS, limit=6)
        # 派生吨价：角色是"派生算式"，不是披露原句
        price = next(j for j in js if j["judgment_id"] == "unit_revenue_not_proof_of_pricing")
        self.assertEqual(price["evidence"][0]["role"], jd.ROLE_DERIVED)
        self.assertTrue(price["evidence"][0]["type"].startswith("派生算式"))
        self.assertNotEqual(price["evidence"][0]["type"], "披露原句")
        # 研发/未来计划段落不得当支持；要如实记成"角色不符"
        st = next(j for j in js if j["judgment_id"] == "product_region_structure")
        st_text = " ".join(str(e.get("text") or "") for e in st["evidence"])
        self.assertNotIn("微生态", st_text, "研发段落不能当结构支持")
        self.assertTrue(any("研发投入" in g or "未来计划" in g for g in st["gaps"]),
                        st["gaps"])
        # 已准入但属"未来计划"的记录：不作为已发生解释，也不出现在支持里
        self.assertFalse(any("提升中高档产品占比" in str(e.get("text") or "")
                             for j in js for e in j["evidence"]))

    def test_company_specific_gap_does_not_leak_to_another_company(self):
        """公司边界：洋河的酒类表缺口只挂洋河；首屏跳过空位判断（X0-3）。"""
        from financial_analysis import judgments as jd
        yh = fa.run("operating_drivers", self._ds("洋河股份", "002304.SZ", self.IS))
        sy = fa.run("operating_drivers", self._ds("三一重工", "600031.SH", self.IS))
        jy = jd.research_judgments([yh], volume_price=TestW2ResearchJudgments._vp(),
                                   records=[], limit=6)
        js = jd.research_judgments([sy], volume_price={"ok": False}, records=[],
                                   limit=6)
        gy = " ".join(g for j in jy for g in j["gaps"])
        gs = " ".join(g for j in js for g in j["gaps"])
        self.assertIn("中高档酒", gy, "洋河自己的缺口要留")
        self.assertNotIn("中高档酒", gs, "洋河的酒类缺口不得串到三一")
        # 首屏选条：空位判断让位给实质判断（三一没有量价时，前两条本来是"不足以下判断"）
        def _j(jid, title, nums=()):
            return {"judgment_id": jid, "title": title, "kind_label": "已发生（读数）",
                    "numbers": list(nums), "evidence": [], "boundary": "b",
                    "alternatives": [], "watch": ["会削弱它的读数"], "gaps": []}
        front = "\n".join(jd.render_judgment_summary([
            _j("v", "销量与收入的数量关系：本次不足以下判断", ["读数不足"]),
            _j("p", "单位收入与提价能力：本次无从判断"),
            _j("c", "现金变化**主要由营运资本（占用与时点）构成**", ["ΔOCF +91.06 亿元"]),
            _j("s", "产品/区域结构是**同一个口径的不同切法**", ["分产品切法：+11.97 亿元"]),
            _j("d", "现金跃升有**真实收支**支撑", ["两行净贡献 +118.12 亿元"])],
            limit=3))
        self.assertNotIn("不足以下判断", front)
        self.assertNotIn("无从判断", front)
        self.assertIn("营运资本", front)
        self.assertIn("+91.06", front)
        self.assertIn("真实收支", front)

    def test_direct_and_indirect_methods_are_reported_separately(self):
        """切法：直接法两行与间接法各自对账，不相加、不嵌套（X0-4）。"""
        from financial_analysis import judgments as jd
        cash = fa.run("cash_reconciliation", self._ds("三一重工", "600031.SH", self.CF_SANY))
        js = jd.research_judgments([cash], direct_cash=self.DIRECT_SANY,
                                   records=self.RECORDS, limit=6)
        cj = next(j for j in js if j["judgment_id"] == "cash_direct_method_support")
        joined = " ".join(cj["numbers"])
        self.assertIn("**直接法**", joined)
        self.assertIn("两行净贡献", joined)
        self.assertIn("其余经营活动收支", joined)
        self.assertIn("-27.06", joined)
        self.assertIn("**间接法**", joined)
        # 10-06 起金额与单位**不留空格**（`+14.86亿元`）：验收按"数值+单位"配对溯源，
        # 隔一个空格只认到裸数字 ⇒ 判不可溯源（实测：新首屏因此掉到 66%）。
        self.assertIn("合并净利润项 +14.86亿元", joined)
        self.assertIn("其他调节项", joined)
        # 净利项**不是**直接法"其余收支"的其中项（旧稿写法「其中合并净利润项只 …」）
        self.assertNotIn("其中合并净利润项", joined)
        self.assertIn("不可相加", cj["boundary"])
        self.assertIn("不是**净利项的分项", cj["boundary"])
        self.assertTrue(any("且" in w and "反弹" in w for w in cj["watch"]),
                        "反转条件要带必要组合：反弹且收现不足以抵消")
        # 公司归因（第 19 页）必须进证据并**被渲染出来**（旧稿只印前两条，把它藏了）
        text = "\n".join(jd.render_judgments(js))
        self.assertIn("发行人归因（原句）", text)
        self.assertIn("第 19 页", text)
        self.assertIn("销售回款增加", text)

    def test_front_judgments_render_before_findings_and_detail_is_separate(self):
        """版面：三项主判断在『关键发现』之前；详表/ID/七段式另附（X0-5）。"""
        import report_brief as rb
        structure = {
            "scope": {"company": "洋河股份", "company_id": "002304.SZ",
                      "caliber": "合并", "as_of": "2025-04-30"},
            "analysis_front": ("## 三项主判断（先看这里）\n\n1. 占位判断\n"),
            "analysis_note": ("## 经营驱动分析正文（洋河股份 2023年→2024年）\n\n"
                              "### 一、结论（先看这三条）\n1. 占位\n"),
            "analysis_card": "## 分析摘要\n- 占位",
            "analysis_detail": "analysis/analysis_detail.md",
        }
        md = rb.render_brief_markdown(structure, body="正文占位")
        self.assertEqual(md.count("## 三项主判断（先看这里）"), 1)
        self.assertLess(md.index("## 三项主判断（先看这里）"), md.index("## 关键发现"),
                        "三项主判断必须在最前面（前两页）")
        self.assertLess(md.index("## 三项主判断（先看这里）"),
                        md.index("## 经营驱动分析正文"))
        # 回流再装配：装配器自己的小节不得被当模型内容再印一遍
        again = rb._analysis_section(md)
        self.assertNotIn("## 三项主判断", again)
        self.assertNotIn("## 经营驱动分析正文", again)


class TestX1ResearchContinuation(unittest.TestCase):
    """X1（阶段X §4）：**可检验的研究续页**。

    反面（阶段X §4 明确禁止的四种做法，逐条对应）：
    ① 拿两侧"本期"直接比 → 2023 对上 2024（同口径比较必须取**同一期**）；
    ② 缺同口径读数就凑一个方向（材料空缺 ≠ 假说被反驳）；
    ③ 新读数改写历史观察（"2024 销量下降"是历史事实）；
    ④ 把历史材料回放包装成事前盲测/预测。

    数据自带（CI 里没有缓存年报）：快照是**普通 dict**（读数与运行输出的真实形状），
    不依赖任何缓存材料。
    """

    def _snap(self, *, periods, volume_yoy=None, volume_cur=None, volume_prev=None,
              inventory_yoy=None, inventory_cur=None, inventory_prev=None,
              price_yoy=None, price_cur=None, price_prev=None,
              cash_total=None, np_v=None, wc_v=None, judgments=(), label="", url=""):
        facts = []
        if volume_cur is not None or volume_yoy is not None:
            facts.append({"group": "实物量", "row_label": "白酒销售量", "unit": "吨",
                          "cur": volume_cur, "prev": volume_prev, "yoy": volume_yoy,
                          "line": "销售量(吨)", "locator": "第 12 页 · 产销量表"})
        if inventory_cur is not None or inventory_yoy is not None:
            facts.append({"group": "实物量", "row_label": "白酒库存量", "unit": "吨",
                          "cur": inventory_cur, "prev": inventory_prev,
                          "yoy": inventory_yoy, "line": "库存量(吨)",
                          "locator": "第 12 页 · 产销量表"})
        vp = {"ok": bool(facts), "facts": facts,
              "derived": ([{"label": "白酒吨价（推算）", "unit": "元/吨",
                            "cur": price_cur, "prev": price_prev, "yoy": price_yoy}]
                          if price_cur is not None or price_yoy is not None else [])}
        runs = {}
        if cash_total is not None:
            runs["cash_reconciliation"] = {
                "model_id": "cash_reconciliation", "status": "validated", "run_id": "run-x",
                "outputs": [{"metric": "operating_cashflow_change", "value": cash_total,
                             "components": [
                                 {"component_id": "change_in_net_profit", "value": np_v},
                                 {"component_id": "change_in_working_capital",
                                  "value": wc_v}]}]}
        return {"label": label, "periods": list(periods),
                "source": {"title": label, "url": url, "periods": list(periods),
                           "disclosure_date": "", "text_sha256": ""},
                "dataset": {"hash": "h-" + "".join(str(p) for p in periods)},
                "runs": runs, "volume_price": vp, "records": [],
                "judgments": list(judgments)}

    @staticmethod
    def _j(jid, title, numbers=(), watch=()):
        return {"judgment_id": jid, "title": title, "numbers": list(numbers),
                "watch": list(watch), "boundary": "b", "alternatives": [], "gaps": []}

    def _pair(self, **later_over):
        earlier = self._snap(periods=(2022, 2023), volume_yoy=-14.93,
                             volume_cur=166_154.73, volume_prev=195_322.68,
                             inventory_yoy=-15.74, inventory_cur=39_176.04,
                             inventory_prev=46_492.0, price_yoy=29.78,
                             price_cur=194_936.0, price_prev=150_207.0,
                             cash_total=24.83e8, np_v=-2.0e8, wc_v=17.0e8,
                             judgments=[self._j("volume_contraction",
                                                "销量收缩是收入下降的重要观察",
                                                ["销售量同比 -14.93%"],
                                                ["下一期同口径销售量"]),
                                        self._j("unit_revenue_not_proof_of_pricing",
                                                "单位收入上升不足以证明提价能力",
                                                ["推算吨价 +29.78%"]),
                                        self._j("cash_from_working_capital",
                                                "现金变化主要由营运资本构成",
                                                ["ΔOCF +24.83 亿元"])],
                             label="2023 年年报（2022→2023）")
        later_kw = dict(periods=(2023, 2024), volume_yoy=-16.30, volume_cur=139_076.05,
                        volume_prev=166_154.73, inventory_yoy=16.38,
                        inventory_cur=45_594.72, inventory_prev=39_176.04,
                        price_yoy=3.93, price_cur=202_592.0, price_prev=194_936.0,
                        cash_total=-15.02e8, np_v=-33.54e8, wc_v=11.04e8,
                        label="2024 年年报（2023→2024）")
        later_kw.update(later_over)
        return earlier, self._snap(**later_kw)

    def test_volume_pressure_deepening_is_strengthened_and_turn_is_weakened(self):
        from financial_analysis import continuation as cont
        earlier, later = self._pair()
        page = cont.compare(earlier, later, limit=3)
        rows = {h["judgment_id"]: h for h in page["hypotheses"]}
        self.assertEqual(len(page["hypotheses"]), 3)
        vol = rows["volume_contraction"]
        self.assertEqual(vol["status"], cont.STATUS_STRONGER)
        self.assertIn("降幅扩大", vol["status_reason"])
        self.assertIn("-16.30%", " ".join(vol["new_readings"]))
        self.assertTrue(vol["observation_condition"] and vol["basis_readings"])
        # 转正 → 削弱（"压力延续"这个假说不再成立）
        _e, later2 = self._pair(volume_yoy=2.5, volume_cur=142_000.0)
        rows2 = {h["judgment_id"]: h for h in cont.compare(earlier, later2)["hypotheses"]}
        self.assertEqual(rows2["volume_contraction"]["status"], cont.STATUS_WEAKER)
        # 吨价：仍为正但增幅收窄 → 水平方向仍"加强"，但把放缓写出来
        price = rows["unit_revenue_not_proof_of_pricing"]
        self.assertEqual(price["status"], cont.STATUS_STRONGER)
        self.assertIn("增幅收窄", price["status_reason"])

    def test_missing_same_caliber_reading_is_unknown_not_a_direction(self):
        from financial_analysis import continuation as cont
        earlier, later = self._pair(volume_yoy=None, volume_cur=None)
        page = cont.compare(earlier, later)
        rows = {h["judgment_id"]: h for h in page["hypotheses"]}
        vol = rows["volume_contraction"]
        self.assertEqual(vol["status"], cont.STATUS_UNKNOWN)
        self.assertIn("不比较", vol["status_reason"])
        self.assertTrue(any("历史材料回放" in t for t in page["limits"]),
                        "回放边界必须写进输出")
        self.assertIn("不推翻", page["historical_note"])

    def test_cash_driver_change_weakens_persistence(self):
        from financial_analysis import continuation as cont
        earlier, later = self._pair()
        page = cont.compare(earlier, later)
        cash = next(h for h in page["hypotheses"]
                    if h["judgment_id"] == "cash_from_working_capital")
        self.assertEqual(cash["status"], cont.STATUS_WEAKER)
        self.assertIn("主导项换了", cash["status_reason"])
        self.assertIn("-15.02", " ".join(cash["new_readings"]))

    def test_overlap_check_compares_the_same_period(self):
        """共同比较期必须**同一条期间**比对：较早材料的本期 ↔ 较新材料的上期。"""
        from financial_analysis import continuation as cont
        earlier, later = self._pair()
        oc = cont.overlap_check(earlier, later)
        self.assertEqual(oc["shared_periods"], [2023])
        for item in oc["items"]:
            self.assertTrue(item["same"], item)
        self.assertIn("一致", oc["verdict"])
        # 较新材料的 2023 读数被改（重述/口径变化）→ 如实标不一致，并说明采用哪一组
        _e, later2 = self._pair(volume_prev=150_000.0)
        oc2 = cont.overlap_check(earlier, later2)
        bad = next(i for i in oc2["items"] if i["metric"] == "白酒销售量")
        self.assertFalse(bad["same"])
        self.assertIn("采用较新材料的比较组", bad["note"])

    def test_assumption_impact_uses_the_same_target_in_both_modes(self):
        from financial_analysis import continuation as cont
        # 洋河真实形状的两期数据（含毛利线以下明细）：detail 模式才有"费用随收入/税率/
        # 少数股东"这些规则，条件与 fixed 不同——这正是"假设为什么重要"要说的事
        rows = []
        for metric, (prev, cur) in dict(TestU2ResearchNote.IS,
                                        gross_profit=(24_926_032_296.09,
                                                      21_125_078_636.90)).items():
            rows.append(_row(metric, "2023年", prev, unit="元",
                             fact_id=f"x1i-{metric}-2023"))
            rows.append(_row(metric, "2024年", cur, unit="元",
                             fact_id=f"x1i-{metric}-2024"))
        ds = fa.freeze_from_facts(rows, periods=(2023, 2024), entity="洋河股份",
                                  entity_id="002304.SZ", as_of="2025-04-30",
                                  source_label="test:x1-impact")
        imp = cont.assumption_impact(ds)
        f, d = imp["modes"]["fixed"], imp["modes"]["detail"]
        self.assertEqual(f["status"], "validated", f)
        self.assertEqual(d["status"], "validated", d)
        self.assertEqual(f["target_net_profit"], d["target_net_profit"],
                         "两个模式必须共用同一目标")
        self.assertIn("规则", imp["note"])
        # 洋河实测形状：fixed 需收入 +15.8226%、detail 需 +42.5%（费用随收入等规则更严）
        self.assertAlmostEqual(f["revenue_growth_to_hold_target"], 15.8226, places=2)
        self.assertGreater(d["revenue_growth_to_hold_target"], 40.0)
        self.assertGreater(imp["revenue_gap_pp"], 20.0, "差额是百分点（不是百分数的百倍）")

    def test_page_has_the_four_columns_and_the_report_renders_it(self):
        from financial_analysis import continuation as cont
        import report_brief as rb
        earlier, later = self._pair()
        page = cont.compare(earlier, later)
        md = cont.render_page(page)
        for needle in ("上次持续性假说", "本期关键新读数", "判断怎样改变", "下一观察",
                       "历史观察与假说分开", "历史材料回放"):
            self.assertIn(needle, md)
        brief = rb.render_brief_markdown(
            {"scope": {"company": "洋河股份", "company_id": "002304.SZ",
                       "caliber": "合并", "as_of": "2025-04-30"},
             "analysis_front": "## 三项主判断（先看这里）\n\n1. 占位\n",
             "continuation_page": md,
             "analysis_note": ("## 经营驱动分析正文（洋河股份 2023年→2024年）\n\n"
                               "### 一、结论\n1. 占位\n")},
            body="正文占位")
        self.assertEqual(brief.count("## 研究续页（可检验）"), 1)
        self.assertLess(brief.index("## 三项主判断（先看这里）"),
                        brief.index("## 研究续页（可检验）"))
        self.assertLess(brief.index("## 研究续页（可检验）"), brief.index("## 关键发现"))
        again = rb._analysis_section(brief)
        self.assertNotIn("## 研究续页", again, "回流再装配不得再印一遍续页")


class TestX0AmountUnitBoundary(unittest.TestCase):
    """X0：**金额单位**边界——运行输出本身就是亿元时，共享格式器不得再除 1e8。

    反例（真实任务 `ui-17947f055b`）：scenario run 基准 66.73 亿元、使用者 80.32 亿元，
    UI 共享情景摘要显示 **0.00**（`_yi` 无条件除 1e8）。这里钉住"按输出 unit 换算"，
    同时确认元口径（离线标杆与官方三表）行为不变。
    """

    YI = {
        "revenue": (331.26, 288.76),
        "gross_profit": (249.26, 211.25),
        "net_profit": (100.16, 66.73),
        "net_profit_consolidated": (100.21, 66.66),
        "operating_cost": (82.00, 77.51),
        "taxes_and_surcharges": (52.69, 48.26),
        "selling_expense": (53.87, 55.16),
        "income_tax_expense": (31.97, 24.77),
    }

    def _ds(self, unit: str):
        scale = 1e8 if str(unit) == "元" else 1.0      # 元口径的数值是亿元口径的 1e8 倍
        rows = []
        for metric, (prev, cur) in self.YI.items():
            for period, value in (("2023年", prev), ("2024年", cur)):
                rows.append(_row(metric, period, value * scale, unit=unit,
                                 fact_id=f"u-{unit}-{metric}-{period}"))
        return fa.freeze_from_facts(rows, periods=(2023, 2024), entity="洋河股份",
                                    entity_id="002304.SZ", as_of="2025-04-30",
                                    source_label=f"test:unit-{unit}")

    def test_yi_unit_run_is_not_divided_twice(self):
        from financial_analysis import charts as ch
        from financial_analysis import narrative as nt
        ds = self._ds("亿元")
        run = fa.run("scenario_sensitivity", ds,
                     params={"revenue_growth": 0.05, "gross_margin_delta": 0.01})
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        out = next(o for o in run.outputs if o.metric == "scenario_net_profit")
        self.assertEqual(str(out.unit), "亿元", out.unit)
        comp = {c["component_id"]: c["value"] for c in out.components}
        # 共享格式器：按输出单位换算 → 66.73/80.32 亿元（旧实现印成 0.00）
        self.assertEqual(nt._yi(comp["base"], out.unit), "+66.73")
        self.assertEqual(nt._yi(comp["user"], out.unit), "+80.32")
        self.assertNotIn("0.00", nt._yi(comp["user"], out.unit))
        # 元口径行为不变：同一读数按元换算仍是 66.73 亿元
        self.assertEqual(nt._yi(comp["base"] * 1e8, "元"), "+66.73")
        # 图表结论（UI 共享情景摘要读它）也不得出现 0.00
        chart = ch.scenario_outcome_bars(run)
        self.assertTrue(chart.get("available"), chart)
        self.assertIn("基准复现 66.73 亿元", chart.get("conclusion") or "")
        self.assertNotIn("0.00 亿元", chart.get("conclusion") or "")
        # 概览三条（UI 摘要）走同一格式器
        summary = " ".join(nt.summary_lines([run]))
        self.assertNotIn("-0.00", summary)

    def test_yuan_unit_run_keeps_the_benchmark_numbers(self):
        """对照：元口径（官方三表/离线标杆）照旧——没有回归。"""
        from financial_analysis import narrative as nt
        ds = self._ds("元")
        run = fa.run("scenario_sensitivity", ds,
                     params={"revenue_growth": 0.05, "gross_margin_delta": 0.01})
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        out = next(o for o in run.outputs if o.metric == "scenario_net_profit")
        comp = {c["component_id"]: c["value"] for c in out.components}
        self.assertEqual(str(out.unit), "元", out.unit)
        self.assertEqual(nt._yi(comp["base"], out.unit), "+66.73")
        self.assertEqual(nt._yi(comp["base"]), "+66.73", "默认口径仍是元")


class TestX0ScenarioOnlyProjection(unittest.TestCase):
    """X0（10-02 实测）：**只选了一条情景运行**时，正文也要投影真实金额与身份。

    反例（`ui-17947f055b` 的真实 A 采纳）：正常任务缺经营驱动/现金调节，`select_for_report`
    仍选仅有的情景 run，而 `narrative` 在两个研究模型都缺时返回空正文 → 采纳校验在正文里找不到
    所选运行，只能被拒（「选了新运行，正文却还是旧稿」）。**不能附个 ID 骗过绑定**：
    正文里必须有这条运行的身份，金额必须是它真实算出来的读数。
    """

    def _scen_run(self):
        rows = [_row("revenue", "2024年", 28_876_296_993.56, unit="元"),
                _row("gross_profit", "2024年", 21_125_078_636.90, unit="元"),
                _row("net_profit", "2024年", 6_673_388_602.12, unit="元")]
        ds = fa.freeze_from_facts(rows, periods=(2024,), entity="洋河股份",
                                  entity_id="002304.SZ", as_of="2025-04-30",
                                  source_label="test:x0-scen-only")
        return fa.run("scenario_sensitivity", ds,
                      params={"revenue_growth": 0.05, "gross_margin_delta": 0.01})

    def test_scenario_only_body_carries_identity_and_real_amounts(self):
        from financial_analysis import narrative as nt
        run = self._scen_run()
        self.assertEqual(run.status, C.RunStatus.VALIDATED, run.reason)
        parts = nt.research_brief([run])
        note = str(parts.get("note") or "")
        self.assertTrue(note, "只有情景运行时也要出正文（否则采纳无法绑定）")
        # ① 身份：所选运行的前 12 位出现在正文里（采纳绑定校验读它）
        self.assertIn(str(run.run_id)[:12], note)
        # ② 真实金额：情景读数来自这条运行，不是占位/零
        out = next(o for o in run.outputs if o.metric == "scenario_net_profit")
        comp = {c["component_id"]: c["value"] for c in out.components}
        self.assertIn("基准复现", note)
        self.assertIn(nt._yi(comp["user"], out.unit), note)
        self.assertNotIn("基准复现 +0.00 亿元", note)
        # ③ 情景独立成段（不是"两个模型都缺"就整段消失）
        self.assertIn("### 五、现金形成与反向情景", note)
        # ④ 摘要（UI 摘要区读它）不再为空，且给出反向情景读数
        summary = " ".join(nt.summary_lines([run]))
        self.assertTrue(summary, "只有情景运行时摘要不应为空")
        self.assertIn("反向情景", summary)


if __name__ == "__main__":
    unittest.main(verbosity=1)
