"""C2c 聚焦测试（**不是**全量套件）：量价两层闭合、范围错配拒绝出数、来源固定、时点隔离。

只跑 C2c 这一条链：年报原表行（白酒销量/产量/库存量 × 分产品收入）→ `wind_import` 预览
（单位与期间口径）→ `operating_drivers`（同口径量价 + 两层闭合 + 错配拒出数）→ 报告装配层
（量价表/两层闭合/补证卡五列/毛利线以下固定 vs 实际）→ `study._market_binding`（来源钉住）。

运行：`python -X utf8 -m unittest test_c2c_volume_price_closure`
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import research_report_adapter as rra
import quant_research.study as study
from adapters import wind_import
from financial_analysis import dataset as dataset_mod
from financial_analysis import runner
from financial_analysis.contracts import NotApplicable

PRICE_ID = "3d6b4acb451abcf960c4132deb16c2a09169234b76dcb643958ceee855fcb148"
CAL_ID = "49413c7d1c48ce4c59f8e63706705c28d4b66edc9d047ab381ea60bd600e5435"
CODE = "002304.SZ"
PERIODS = ["2025-12-31", "2024-12-31"]        # 数据集按报告期末取期间（升序见 dataset.freeze）

# 真实年报原表读数（洋河 2025 年报：p13 白酒销量/产量/库存量、p15 分产品收入），
# 单位：吨 / 元。用真实值是为了让"两层闭合"与"错配差多少"有可核对的量级，
# 但断言只依赖关系（同口径、闭合差、拒出数），不依赖某个具体数字。
# 键顺序 = (2024, 2025)。
ROWS = {
    "sales_volume_baijiu": (139076.05, 102931.09),
    "production_volume_baijiu": (145494.73, 88458.93),
    "inventory_volume_baijiu": (45594.72, 31122.56),
    "revenue_product_baijiu": (28175707878.18, 18748174429.79),
    "revenue_product_hongjiu": (72587951.44, 28151831.13),
    "revenue_product_other": (628001163.94, 434731352.13),
}


def _financials(path: Path, *, drop_products: bool = False) -> dict:
    """写一份 wide 行式 financials.json：吨行与元行分开（行级 unit），身份字段写全。"""
    rows = []
    for metric, (prev, cur) in ROWS.items():
        if drop_products and metric.startswith("revenue_product_"):
            continue                      # 错配反例：抽掉分产品行，只剩公司口径
        for period, value in zip(PERIODS, (cur, prev)):     # 与 PERIODS 对齐（2025→cur）
            unit = "吨" if metric.endswith("_baijiu") and "revenue" not in metric else "元"
            dimension = ("分产品:白酒" if metric in ("revenue_product_baijiu", "sales_volume_baijiu",
                                                   "production_volume_baijiu",
                                                   "inventory_volume_baijiu")
                         else "分产品:红酒" if metric == "revenue_product_hongjiu"
                         else "分产品:其他" if metric == "revenue_product_other" else "")
            rows.append({"code": CODE, "entity": "洋河股份", "period": period,
                         "period_type": "年度", "period_basis": "累计",
                         "period_end": period, "period_start": f"{period[:4]}-01-01",
                         "disclosed_at": "2026-04-28", "available_at": "2026-04-28",
                         "retrieved_at": "2026-10-11T00:00:00", "currency": "CNY",
                         "unit": unit, "caliber": "合并", "dimension": dimension,
                         metric: value})
    # 桥必需的三项（revenue/operating_cost/net_profit）＋常见费用行（缺的行进残差）
    core = {"revenue": (28876296993.56, 19211057613.05),
            "operating_cost": (7751218356.66, 5455522936.43),
            "net_profit": (6673388602.12, 2206058926.68),
            "net_profit_consolidated": (6666455819.96, 2190987361.45),
            "gross_profit": (21125078636.90, 13755534676.62),
            "selling_expense": (5516238544.79, 5205631990.16),
            "admin_expense": (1924730302.35, 1789690987.76),
            "rd_expense": (104796407.26, 144986992.32),
            "taxes_and_surcharges": (4826086952.64, 3417617382.91),
            "operating_cashflow": (4628711237.28, -763040326.79),
            "inventory": (19732881051.73, 20375489436.78),
            "accounts_receivable": (45142892.78, 41223344.11),
            "accounts_payable": (1376209527.01, 1211233445.55),
            "total_assets": (67972824646.81, 62112334455.66),
            "total_liabilities": (20448281133.00, 19334455622.11)}
    for metric, (prev, cur) in core.items():
        for period, value in zip(PERIODS, (cur, prev)):
            rows.append({"code": CODE, "entity": "洋河股份", "period": period,
                         "period_type": "年度", "period_basis": "累计",
                         "period_end": period, "period_start": f"{period[:4]}-01-01",
                         "disclosed_at": "2026-04-28", "available_at": "2026-04-28",
                         "retrieved_at": "2026-10-11T00:00:00", "currency": "CNY",
                         "unit": "元", "caliber": "合并", metric: value})
    payload = {"schema": "weavemind.financials/1", "rows": rows}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="c2c-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _ds(self, *, drop_products: bool = False):
        f = self.tmp / "financials.json"
        _financials(f, drop_products=drop_products)
        pv = wind_import.preview_file(f, kind="financial")
        obs = [dict(r) for r in pv["records"]]
        ds = dataset_mod.freeze(obs, entity_id=CODE, as_of="2026-10-09",
                                periods=("2024-12-31", "2025-12-31"),
                                source_label="测试（本地构造）", restrictions="内部研究")
        return pv, ds

    def _run(self, *, drop_products: bool = False) -> tuple[dict, dict]:
        pv, ds = self._ds(drop_products=drop_products)
        self._pv = pv
        run = runner.run("operating_drivers", ds, params={}).as_dict()
        diag = ((run.get("outputs") or [{}])[0] or {}).get("diagnostics") or {}
        self.assertTrue(diag, "outputs[0].diagnostics 为空：断言拿不到读数")
        return run, diag

    @staticmethod
    def _out(run: dict, name: str):
        for rec in run.get("outputs") or []:
            if rec.get("name") == name:
                return rec
        return {}


class UnitAndPeriodCaliber(_Base):
    def test_ton_rows_scale_and_stock_period(self):
        """吨不被当元、期末库存量按时点量（不编期间起点）。"""
        pv, _ = self._ds()
        by = {}
        for r in pv["records"]:
            by.setdefault(r["metric"], {})[str(r["period"])] = r
        vol = by["sales_volume_baijiu"]["2025年"]
        self.assertEqual(vol["unit"], "吨")
        self.assertEqual(float(vol["value"]), 102931.09)      # 吨：不做千/万倍换算
        self.assertEqual(vol["period_kind"], "flow")
        stock = by["inventory_volume_baijiu"]["2025年"]
        self.assertEqual(stock["period_kind"], "stock")
        self.assertEqual(str(stock.get("period_start") or ""), "")
        self.assertTrue(all(r["analysis_eligible"] for r in pv["records"]))

    def test_dimension_survives_as_scope(self):
        pv, _ = self._ds()
        dims = {r["metric"]: r.get("dimension") for r in pv["records"]}
        self.assertEqual(dims["revenue_product_baijiu"], "分产品:白酒")
        self.assertEqual(dims["sales_volume_baijiu"], "分产品:白酒")


class SameScopeTwoLayers(_Base):
    def test_positive_case_two_layer_closure(self):
        """正例：白酒收入÷白酒销量（同口径）；两层各自闭环 ≤0.01 元。"""
        run, dg = self._run()
        self.assertEqual(run.get("status"), "validated")
        self.assertEqual(dg.get("volume_price_revenue_metric"), "revenue_product_baijiu")
        self.assertEqual(dg.get("volume_price_revenue_relation"), "same_scope")
        self.assertFalse([x for x in (dg.get("volume_price_skipped") or []) if "范围错配" in str(x)])
        vp = dg.get("volume_price_inputs") or {}
        # 第一层：ΔR = 数量贡献 + 单位实现收入贡献（对称分解）
        r0, r1 = vp.get("revenue_prev_yuan"), vp.get("revenue_cur_yuan")
        q0, q1 = vp.get("volume_prev"), vp.get("volume_cur")
        self.assertTrue(all(isinstance(x, (int, float)) for x in (r0, r1, q0, q1)))
        self.assertAlmostEqual(r1 - r0, vp["volume_effect_yuan"] + vp["unit_revenue_effect_yuan"],
                               delta=0.01)
        self.assertLess(abs(float(vp["closure_gap_yuan"])), 0.01)
        # 单位实现收入 = 同口径收入/销量（不是公司收入/白酒销量）
        self.assertAlmostEqual(vp["unit_revenue_cur"], r1 / q1, delta=0.01)
        # 第二层：分产品勾回公司合计，残差 ≤0.01 元，结构只有白酒/红酒/其他（不编第三项）
        rec = dg.get("revenue_reconciliation") or {}
        self.assertLess(abs(float(rec["layer2_residual_yuan"])), 0.01)
        metrics = sorted(str(i.get("metric")) for i in rec.get("items") or [])
        self.assertEqual(metrics, ["revenue_product_baijiu", "revenue_product_hongjiu",
                                   "revenue_product_other"])
        others = sum(float(i["delta"]) for i in rec["items"]
                     if str(i.get("metric")) != "revenue_product_baijiu")
        self.assertAlmostEqual(float(rec["company_delta_yuan"]),
                               float(rec["baijiu_delta_yuan"]) + others, delta=0.01)
        # 同表相邻行走**补充观察**通道，不进分解
        ex = dg.get("volume_price_extra") or {}
        self.assertEqual(sorted(ex), ["inventory_volume", "production_volume"])
        self.assertEqual(ex["inventory_volume"]["unit"], "吨")

    def test_scope_mismatch_refuses_to_produce_a_number(self):
        """反例：只有公司收入 + 白酒销量 → **拒绝出数**，不给'公司收入÷白酒销量'的替代吨价。"""
        run, dg = self._run(drop_products=True)
        self.assertEqual(run.get("status"), "validated")      # 桥仍成立，只是量价留缺口
        skips = [str(x) for x in (dg.get("volume_price_skipped") or [])]
        self.assertTrue(any("范围错配" in s and "拒绝出数" in s for s in skips), skips)
        self.assertFalse(dg.get("volume_price_inputs"))
        self.assertIsNone(dg.get("volume_price_revenue_metric") or None)
        self.assertTrue(any("分产品" in str(x)
                            for x in (dg.get("revenue_reconciliation_skipped") or [])))
        # 拒出数不能把数留在残差里冒充"已解释"
        self.assertEqual(rra._num2(None), "读数缺失")

    def test_volume_price_lines_render_gap_honestly(self):
        """装配层：拒出数时写缺口与原因，不写数字。"""
        blk = {"bridge": {"diagnostics": {"volume_price_skipped": ["合并:范围错配拒绝出数——x"],
                                         "revenue_reconciliation_skipped": ["分产品行缺失：白酒"]}}}
        text = "\n".join(rra._volume_price_lines(blk))
        self.assertIn("不给", text)
        self.assertIn("范围错配", text)
        self.assertNotIn("单位实现收入（含产品结构）", text)


class SourceBindingPinned(unittest.TestCase):
    """C2c 唯一来源接缝：spec 显式写 id → 生成路径真加载并显示 source=spec。"""

    def test_spec_id_is_consumed_and_never_replaced_by_default(self):
        spec = {"market_pin": {"price_dataset_id": PRICE_ID, "calendar_id": CAL_ID,
                               "note": "C2c 测试钉住"}}
        b = study._market_binding(spec, "2026-10-09")
        self.assertEqual(b["price_dataset_id"], PRICE_ID)
        self.assertEqual(b["calendar_id"], CAL_ID)
        self.assertEqual(b["price_dataset_id_source"], "spec")
        self.assertEqual(b["calendar_id_source"], "spec")
        if b["status"] == "pinned":
            self.assertEqual(b["price"]["loaded"], PRICE_ID)
            self.assertEqual(b["calendar"]["loaded"], CAL_ID)
            self.assertTrue(b["price"]["rows_file_sha256"])
            self.assertTrue(b["calendar"]["sessions_file_sha256"])
        else:
            # 本机没有离线行情/日历时：只能是 refused（如实报），绝不能记成 pinned
            self.assertEqual(b["status"], "refused")
            self.assertTrue(b["reasons"])

    def test_wrong_id_is_refused_not_substituted(self):
        spec = {"market_pin": {"price_dataset_id": "0" * 64, "calendar_id": CAL_ID}}
        b = study._market_binding(spec, "2026-10-09")
        self.assertEqual(b["price"]["requested"], "0" * 64)
        self.assertNotEqual(b["price"].get("loaded"), "0" * 64)
        if b["status"] == "pinned":
            self.fail("id 不一致却记成 pinned")
        self.assertTrue(b["reasons"])

    def test_no_pin_is_unpinned(self):
        b = study._market_binding({"market": {}}, "2026-10-09")
        self.assertEqual(b["status"], "unpinned")
        self.assertEqual(b["price_dataset_id_source"], "unpinned")

    def test_report_layer_prefers_spec_over_env_default(self):
        os.environ["WEAVEMIND_PRICE_DATASET"] = "e" * 64
        self.addCleanup(os.environ.pop, "WEAVEMIND_PRICE_DATASET", None)
        spec = {"market_pin": {"dataset_id": PRICE_ID, "calendar_id": CAL_ID}}
        self.assertEqual(rra._price_dataset_id(spec), (PRICE_ID, "spec"))
        self.assertEqual(rra._calendar_dataset_id(spec), (CAL_ID, "spec"))
        self.assertEqual(rra._price_dataset_id({}), ("e" * 64, "env"))
        os.environ.pop("WEAVEMIND_PRICE_DATASET", None)
        self.assertEqual(rra._price_dataset_id({}), (rra.PRICE_DATASET_DEFAULT, "default"))
        self.assertEqual(rra._calendar_dataset_id({}), ("", "unpinned"))


class TimePointIsolation(unittest.TestCase):
    """时点隔离：2026-04-28 可用的新比较行不进 2025-04-30 冻结的主卡。"""

    def test_new_rows_available_after_cutoff_are_excluded(self):
        rows = [{"entity_id": CODE, "metric": "revenue_product_baijiu", "period": "2025年",
                 "available_at": "2026-04-28", "disclosed_at": "2026-04-28", "value": 1.0,
                 "analysis_eligible": True},
                {"entity_id": CODE, "metric": "revenue", "period": "2024年",
                 "available_at": "2025-04-30", "disclosed_at": "2025-04-30", "value": 2.0,
                 "analysis_eligible": True}]
        kept, excluded = study._visible(rows, "2025-04-30")
        self.assertEqual([r["period"] for r in kept], ["2024年"])
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["metric"], "revenue_product_baijiu")
        self.assertIn("截止之后", str(excluded[0]["reason"]))

    def test_frozen_manifest_separates_the_two_cutoffs(self):
        """冻结底稿把特征截止与来源绑定分开写（旧研究的 summary 不改）。"""
        self.assertTrue(hasattr(study, "_market_binding"))
        src = Path("quant_research/study.py").read_text(encoding="utf-8")
        self.assertIn("market_binding", src)
        self.assertIn("feature_cutoff", src)


class EvidenceCardShape(unittest.TestCase):
    """补证卡：原待证解释→实际证据→贡献→支持/削弱/仍未知→下一观察（五列）。"""

    def test_missing_readings_card_keeps_five_columns_and_no_invented_numbers(self):
        blk = {"status": "pending", "reason": "续页材料未到"}
        lines = rra._evidence_card(blk, code=CODE, who="洋河股份", q1c={}, jd={}, jg={},
                                   q1f={})
        text = "\n".join(lines)
        header = [ln for ln in lines if ln.startswith("|")]
        self.assertTrue(header)
        self.assertEqual(header[0].count("|"), 6)             # 5 列 → 6 个竖线
        for row in header[1:]:
            self.assertEqual(row.count("|"), 6)
        self.assertIn("仍未知", text)
        self.assertIn("不编数", text)

    def test_pending_realization_shows_volume_price_gap(self):
        lines = rra._realization_body_lines({"status": "pending", "reason": "续页材料未到"},
                                            rows=[], tf={}, t=None, feature_cutoff="2025-04-30")
        text = "\n".join(lines)
        self.assertIn("量价", text)
        self.assertIn("续页材料未到", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
