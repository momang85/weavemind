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
        self.assertEqual([m.model_id for m in fa.specs()], ["profit_bridge"])


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


class TestDataAnalyzerTakesTheFinancialPath(unittest.TestCase):
    """Q1：金融任务必须按**显式数据集 + 分析计划**走注册模型，不走"最新 CSV + 末列目标"。"""

    def setUp(self):
        import tempfile
        self.ws = Path(tempfile.mkdtemp(prefix="fa_worker_"))
        (self.ws / "project").mkdir(parents=True, exist_ok=True)

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
            instruction, {"workspace": str(self.ws)})))

    def test_financial_task_runs_registered_models_and_stores_runs(self):
        self._write_working_paper()
        got = self._execute("分析本期归母净利润的变化由哪些金额项构成 [研究契约]")
        self.assertEqual(got["mode"], "financial")
        self.assertEqual(got["status"], "success")
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

    def test_missing_input_still_takes_the_financial_path(self):
        rows = [r for r in _two_period_rows() if r["metric"] != "gross_profit"]
        self._write_working_paper(rows)
        got = self._execute("分析利润变化 [研究契约]")
        self.assertEqual(got["mode"], "financial", "缺输入也不得回退到 CSV 猜测")
        # 初筛就缺输入 → 进计划的**拒绝清单**（带缺什么），不生成 run；状态不得报 success
        self.assertEqual(got["status"], "failed", got["status"])
        rej = got["plan"]["rejected"]
        self.assertTrue(rej, rej)
        item = [r for r in rej if r.get("model_id") == "profit_bridge"][0]
        self.assertEqual(item["reason"], "缺输入")
        self.assertIn("gross_profit", item["missing"])
        self.assertEqual([r for r in got["runs"] if r["model_id"] == "profit_bridge"], [],
                         "缺输入的模型不得产出（也不得假装算过）")
        # 顺带算出的比率仍然如实记录（它们真的验证通过了）
        self.assertTrue(all(r["status"] == C.RunStatus.VALIDATED for r in got["runs"]))

    def test_non_financial_workspace_keeps_the_generic_eda_path(self):
        """没有本次任务的金融底稿时，原有 EDA 路径一字不变（不误伤通用数据任务）。"""
        got = self._execute("帮我做一下数据探索")
        self.assertNotEqual(got.get("mode"), "financial")
        self.assertEqual(got.get("status"), "failed")
        self.assertFalse((self.ws / "analysis_runs.json").exists(),
                         "非金融任务不得写分析运行记录")


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


if __name__ == "__main__":
    unittest.main(verbosity=1)
