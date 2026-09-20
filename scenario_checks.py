# -*- coding: utf-8 -*-
"""F3-B 退出关卡：三类**固定离线场景**跑真实装配/渲染/导出路径，逐项确定性核对。

为什么冻结成场景：单测各自钉一处行为，但"整条链路一起改"时没人保证组合起来还对。
三份输入（`evals/scenarios/*.json`）覆盖：①正常增长且证据齐全 ②亏损/现金净流出+跨期
单位不一致 ③缺附注且资料截止不满足；跑法见 `scripts/scenario_run.py`，产物落运行期目录
（`.weavemind/scenarios/`），跨修订可直接比对 manifest。

为什么是模块而不是 `test_*.py`：仓库守卫要求每个 `test_*.py` 都必须出现在 `ci.yml` 里，
而工作流文件需要 `workflow` scope 才能推送；把场景检查做成模块、由 CI 清单内的
`test_offline_delivery.py` 调用，既进了门禁又不新增工作流条目（`run_all()` 供其调用，
本模块也可直接 `python -m unittest scenario_checks` 单独跑）。
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
sys.path.insert(0, str(ROOT / "scripts"))

import scenario_run  # noqa: E402


def run_all(*, out_root: Path | None = None) -> dict:
    """跑三份冻结场景，返回 `{name: manifest}`（产物写 `out_root`，默认运行期目录）。"""
    root = Path(out_root) if out_root else scenario_run.OUT_ROOT
    out: dict = {}
    for f in sorted(scenario_run.SCENARIO_DIR.glob("*.json")):
        spec = json.loads(f.read_text(encoding="utf-8"))
        out[str(spec["name"])] = scenario_run.run_scenario(spec, out_root=root)
    return out


class TestOfflineScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="wm_scen_"))
        cls.manifests = run_all(out_root=cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_three_scenarios_exist(self):
        self.assertEqual(sorted(self.manifests),
                         ["loss_mixed_units", "missing_footnote_asof", "normal_growth"])

    def test_each_scenario_meets_its_frozen_expectations(self):
        for name, m in self.manifests.items():
            checks = {k: v for k, v in (m.get("checks") or {}).items() if k != "all_passed"}
            self.assertTrue(checks, f"{name} 没有核对项")
            failed = [k for k, v in checks.items() if not v]
            self.assertEqual(failed, [], f"{name} 未通过：{failed}；交付={m['delivery']}")

    def test_normal_growth_is_verified_with_full_evidence(self):
        m = self.manifests["normal_growth"]
        self.assertEqual(m["delivery"]["status"], "verified", m["delivery"])
        self.assertEqual(m["evidence"]["missing_labels"], [])
        self.assertGreaterEqual(m["evidence"]["located"], 4)
        self.assertEqual(m["paper"]["problems"], 0)
        self.assertEqual(m["paper"]["gaps"], 0)
        self.assertIn("高于", m["coverage_finding"])
        self.assertEqual(m["acceptance"]["overall"], "pass", m["acceptance"])

    def test_loss_scenario_keeps_honest_gaps_and_no_yoy_on_negative_base(self):
        m = self.manifests["loss_mixed_units"]
        self.assertIn("不表示利润有现金支撑", m["coverage_finding"])
        # 跨期单位不一致 + 基期为负 → 底稿记问题、不硬算同比
        self.assertTrue(any("币种/单位不一致" in p for p in m["paper_problems"]),
                        m["paper_problems"])
        self.assertTrue(any("基期为负" in p for p in m["paper_problems"]),
                        m["paper_problems"])
        self.assertNotIn("revenue_yoy", m["quality_metrics"])
        self.assertNotIn("net_profit_yoy", m["quality_metrics"])
        self.assertEqual(m["delivery"]["status"], "draft")

    def test_missing_footnote_scenario_reports_exclusions_with_reasons(self):
        m = self.manifests["missing_footnote_asof"]
        self.assertGreaterEqual(len(m["evidence"]["missing_labels"]), 1)
        self.assertTrue(any(e.get("validation_status") == "after_as_of"
                            for e in (m["evidence"]["excluded"] or [])),
                        m["evidence"]["excluded"])
        self.assertEqual(m["delivery"]["status"], "draft")
        self.assertIn("低于", m["coverage_finding"])

    def test_artifacts_are_listed_with_hashes_for_diffing(self):
        """每个场景都要有可跨修订比对的产物清单（含 sha256 与版本号）。"""
        for name, m in self.manifests.items():
            arts = m.get("artifacts") or {}
            for required in ("report.md", "report_structure.json",
                             "narrative_evidence.json", "working_paper.json"):
                self.assertIn(required, arts, f"{name} 缺少 {required}")
            self.assertTrue(any(p.startswith("charts/chart_") for p in arts),
                            f"{name} 没有图表产物")
            for path, meta in arts.items():
                self.assertEqual(len(meta["sha256"]), 64, path)
                self.assertGreater(meta["bytes"], 0, path)
            self.assertTrue(m["delivery"]["version_id"], name)

    def test_chart_captions_repeat_no_derived_readings(self):
        """正文图注不得重复派生读数（同比/百分点）：图注单列读数会变成"不可溯源数字"。"""
        for name, m in self.manifests.items():
            report = (self.tmp / name / "report.md").read_text(encoding="utf-8")
            captions = [ln for ln in report.splitlines() if ln.startswith("图 ")]
            self.assertTrue(captions, f"{name} 没有图注")
            for ln in captions:
                self.assertNotIn("%", ln, f"{name} 图注含百分比读数：{ln[:80]}")
                self.assertNotIn("个百分点", ln, f"{name} 图注含百分点读数：{ln[:80]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
