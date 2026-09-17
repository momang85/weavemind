# -*- coding: utf-8 -*-
"""S1：事实保真——同一事实在 解析→financials→clean→报告→底稿 各层不变。

架构指令点名的三处 P1（`结构化流水线/验收器`）在这里逐条钉住：

1. **币种单位被改写**：适配器已经在 `metadata` 里声明了币种与单位
   （SEC=`USD`/`亿美元`，东财=`CNY`/`亿元`），而合并到 clean 时**一律写"亿元"**——
   于是 100 亿美元变成"100 亿元"，读起来是人民币口径。修法：单位取自来源声明，
   来源没声明时回落到默认值但把 `unit_source` 标成 `default`（默认值可见）。
2. **单实体丢主体**：单实体预载只把 financials 传进 clean，主体/代码/币种在这一层就没了，
   下游（验收、底稿）无从判断"这个数字属于谁"。修法：主体/代码/币种/期间/来源类型
   随每一行落盘。
3. **clean 命中绕过主体校验**：`check_number_traceability` 里 clean_chart_data 命中
   优先定案，跳过了后面的子句主体筛选。修法：所有来源通道共用同一套归属校验。

夹具是**合成的**（非现实公司事实），只验证"同一事实不被改写、不被错配主体"。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import acceptance_checker  # noqa: E402
import workspace as ws_mod  # noqa: E402
from structured_pipeline import StructuredPipelineMixin  # noqa: E402

# ── 固定夹具：合成的美股 10-K 财务行（**非现实公司事实**）──
FIXTURE_SUBJECT = "Contoso Semiconductors Inc."
FIXTURE_TICKER = "CSTX"
FIXTURE_ROWS = [
    {"year": 2024, "report_type": "10-K", "revenue": 245.8, "net_profit": 36.9,
     "gross_profit": 121.4, "gross_margin": 49.39, "operating_profit": 48.7,
     "total_assets": 351.2, "total_liabilities": 130.5,
     "operating_cashflow": 61.2},
]
SEC_METADATA = {
    "source": "sec_edgar", "company": FIXTURE_SUBJECT, "ticker": FIXTURE_TICKER,
    "currency": "USD", "unit": "亿美元",
}
CN_METADATA = {
    "source": "eastmoney", "company": "示例股份有限公司", "code": "600000",
    "currency": "CNY", "unit": "亿元",
}


def _merge(metadata: dict, rows: list | None = None, entity: str | None = None) -> dict:
    return StructuredPipelineMixin._merge_structured_financials(
        {"market_data": []}, rows if rows is not None else FIXTURE_ROWS,
        "https://example.invalid/facts",
        entity=entity,
        subject=str(metadata.get("company") or ""),
        subject_id=str(metadata.get("ticker") or metadata.get("code") or ""),
        currency=str(metadata.get("currency") or ""),
        amount_unit=str(metadata.get("unit") or ""),
        source_kind=str(metadata.get("source") or ""),
    )


class TestUnitFidelity(unittest.TestCase):
    """金额单位/币种必须与来源声明一致，不能被改写成"亿元"。"""

    def test_sec_amounts_keep_declared_currency_and_unit(self):
        rows = _merge(SEC_METADATA)["market_data"]
        self.assertTrue(rows, "夹具应产出市场数据行")
        amounts = [r for r in rows if r["unit"] != "%"]
        self.assertEqual(len(amounts), 7, rows)   # 营收/净利/毛利/经营利润/总资产/总负债/经营现金流
        for r in amounts:
            self.assertEqual(r["unit"], "亿美元",
                             f"{r['label']} 的单位必须沿用来源声明，不能写亿元")
            self.assertEqual(r["unit_source"], "source")
            self.assertEqual(r["currency"], "USD")
        self.assertFalse(any(r["unit"] == "亿元" for r in amounts),
                         "美股金额不得被标成人民币口径")

    def test_a_share_keeps_declared_unit(self):
        rows = _merge(CN_METADATA, rows=[{"year": 2024, "revenue": 1741.0}])["market_data"]
        self.assertEqual(rows[0]["unit"], "亿元")
        self.assertEqual(rows[0]["currency"], "CNY")
        self.assertEqual(rows[0]["unit_source"], "source")

    def test_missing_declaration_falls_back_and_is_marked(self):
        """来源没声明单位时用默认值，但必须标成 default（默认值不是来源事实）。"""
        md = dict(SEC_METADATA)
        md.pop("unit")
        rows = _merge(md, rows=[{"year": 2024, "revenue": 245.8}])["market_data"]
        self.assertEqual(rows[0]["unit"], "亿元")
        self.assertEqual(rows[0]["unit_source"], "default",
                         "回落的默认单位要显式标出来，不能伪装成来源声明")

    def test_ratio_metric_keeps_percent(self):
        rows = _merge(SEC_METADATA)["market_data"]
        gm = [r for r in rows if r["label"].endswith("毛利率")]
        self.assertEqual(len(gm), 1)
        self.assertEqual(gm[0]["unit"], "%")


class TestSubjectFidelity(unittest.TestCase):
    """主体/期间/来源类型要随事实走，单实体路径也不能丢。"""

    def test_single_entity_records_subject(self):
        rows = _merge(SEC_METADATA)["market_data"]
        for r in rows:
            self.assertEqual(r["entity"], FIXTURE_SUBJECT)
            self.assertEqual(r["entity_id"], FIXTURE_TICKER)
            self.assertEqual(r["source_kind"], "sec_edgar")
            self.assertEqual(r["period"], "2024年")
            self.assertEqual(r["period_type"], "10-K")

    def test_multi_entity_labels_and_subject_coexist(self):
        rows = _merge(SEC_METADATA, entity=FIXTURE_SUBJECT)["market_data"]
        self.assertTrue(all(r["label"].startswith(FIXTURE_SUBJECT) for r in rows),
                        "多实体路径的标签要带实体前缀，避免同年指标互相覆盖")
        self.assertTrue(all(r["entity"] == FIXTURE_SUBJECT for r in rows))


class TestCleanHitSharesSubjectCheck(unittest.TestCase):
    """clean 命中必须与其它来源通道共用归属校验（S1-1 的第三处 P1）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_fact_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", self._old)

    def _sources(self, tid: str, clean_rows: list[dict]) -> dict:
        # `_collect_sources` 读的是 <workspace>/project/clean_chart_data.json
        ws = ws_mod.task_workspace(tid)
        proj = ws / "project"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "clean_chart_data.json").write_text(
            json.dumps({"market_data": clean_rows}, ensure_ascii=False),
            encoding="utf-8")
        return acceptance_checker._collect_sources(ws)

    def _row(self, entity: str, value: float = 1741.0) -> dict:
        return {
            "type": "market_size", "label": f"{entity}2024年营收",
            "value": value, "unit": "亿元", "unit_source": "source",
            "year": 2024, "source": "https://example.invalid/a",
            "caliber": "年报", "entity": entity, "entity_id": "X",
            "currency": "CNY", "period": "2024年", "period_type": "10-K",
            "source_kind": "sec_edgar",
        }

    def test_clean_hit_with_other_subject_is_not_traceable(self):
        """clean 里是 B 公司的数字，报告写 A 公司 → 不得算可溯源。"""
        report = ("# 复核\n\n宁德时代2024年营收 1741 亿元。\n\n"
                  "数据截至 2024-12-31。\n")
        src = self._sources("fact-other", [self._row("比亚迪")])
        res = acceptance_checker.check_number_traceability(
            report, src, domain="financial", goal="分析宁德时代2024年报营收")
        raws = [str(t.get("raw")) for t in (res.get("traceable") or [])]
        self.assertNotIn("1741 亿元", raws,
                         f"clean 命中也不得绕过主体校验：{res.get('details')}")

    def test_clean_hit_with_same_subject_is_traceable(self):
        """主体一致时照常可溯源（修完不能把正常路径也否掉）。"""
        report = ("# 复核\n\n宁德时代2024年营收 1741 亿元。\n\n"
                  "数据截至 2024-12-31。\n")
        src = self._sources("fact-same", [self._row("宁德时代")])
        res = acceptance_checker.check_number_traceability(
            report, src, domain="financial", goal="分析宁德时代2024年报营收")
        raws = [str(t.get("raw")) for t in (res.get("traceable") or [])]
        self.assertIn("1741 亿元", raws,
                      f"同一主体的 clean 命中应照常可溯源：{res.get('details')}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
