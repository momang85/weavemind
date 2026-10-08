# -*- coding: utf-8 -*-
"""§16 接入：**本地研究试验**入口（工作台端点）的最小验证。

只覆盖本批新增的接入与它的身份/时点反例，不重跑既有套件：

- 入口顺序是硬门槛：**文件预览 → 明确截止/公司 → run_study → 内部产物**；未确认不给运行；
- 公司身份只从文件读，**不按请求里的公司名替换材料**；文件必须真在本次输入里；
- 产物是**内部研究文件**：不采纳、不进交付包、未人工复核（三态照实）；
- verify/replay 复用既有实现：篡改产物要被抓到，实现变了不给旧版背书；
- 时点反例：可用时间早于披露时间的行必须标不可用（不能进模型）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

SAMPLE = ROOT / "examples" / "research_study"

FIN_HEADER = ("证券代码,公司名称,报告期,报告类型,披露日期,下载日期,单位,币种,报表口径,"
              "营业收入,营业成本,归母净利润,合并净利润,经营活动产生的现金流量净额,"
              "应收账款,存货,应付账款,总资产,总负债")


class _Handler:
    """最小 handler 替身：只实现 `_json`（收集响应）与 `_client_ip`。"""

    def __init__(self, path: str):
        self.path = path
        self.responses: list[tuple] = []

    def _json(self, payload, status=200):
        self.responses.append((payload, status))
        return payload

    def _client_ip(self):
        return "127.0.0.1"

    @property
    def last(self):
        return self.responses[-1] if self.responses else (None, None)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_study_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_env = os.environ.get("WEAVEMIND_RESEARCH_STUDY_DIR")
        os.environ["WEAVEMIND_RESEARCH_STUDY_DIR"] = str(self.tmp / "studies")
        self.addCleanup(self._restore_env)
        self._audit = mock.patch.object(__import__("web_ui"), "audit_log",
                                        lambda *a, **k: None)
        self._audit.start()
        self.addCleanup(self._audit.stop)

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop("WEAVEMIND_RESEARCH_STUDY_DIR", None)
        else:
            os.environ["WEAVEMIND_RESEARCH_STUDY_DIR"] = self._old_env

    # ── 便捷调用（全部走端点函数，不起服务）──────────────────────────────
    def _preview(self, body=None):
        import web_ui
        h = _Handler("/api/research-study/preview")
        web_ui._post_research_study_preview(
            h, h.path, body or {"spec": "sample", "input_id": "__sample__"},
            {"user": "admin"})
        return h.last

    def _run(self, **over):
        import web_ui
        body = {"spec": "sample", "input_id": "__sample__", "confirmed": True,
                "confirmed_subjects": ["900001.SH", "900002.SH"]}
        body.update(over)
        h = _Handler("/api/research-study/run")
        web_ui._post_research_study_run(h, h.path, body, {"user": "admin"})
        return h.last

    def _write_input(self, input_id: str, files: dict[str, str]) -> str:
        d = self.tmp / "studies" / "_uploads" / input_id
        d.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
        return str(d)


class TestStudyEntryHappyPath(_Base):
    def test_sample_lists_spec_and_files(self):
        import web_ui
        h = _Handler("/api/research-study/sample")
        web_ui._get_research_study_sample(h, h.path)
        payload, status = h.last
        self.assertEqual(status, 200, payload)
        names = [f["name"] for f in payload["files"]]
        self.assertIn("financials.csv", names)
        self.assertEqual(payload["spec"]["schema"], "weavemind.study_spec/1")
        self.assertTrue(payload["spec"]["synthetic"], "样例必须显式标模拟")
        self.assertEqual(payload["sample_input_id"], "__sample__")

    def test_preview_reports_subjects_cutoffs_and_gaps(self):
        payload, status = self._preview()
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["subjects"], ["900001.SH", "900002.SH"])
        self.assertTrue(payload["synthetic"])
        self.assertEqual(payload["feature_cutoff"], "2025-04-30")
        self.assertEqual(payload["evaluation_as_of"], "2025-06-30")
        self.assertEqual(payload["research_state"], "research_draft")
        self.assertEqual(payload["human_review"], "unreviewed")
        self.assertFalse(payload["external_delivery"]["allowed"])
        self.assertGreater(payload["previews"]["financial"]["rows"], 0)
        for run in payload["financial"]["900001.SH"]["runs"]:
            self.assertEqual(run["status"], "validated", run)
        self.assertIn("cash_pressure", [r["model_id"] for r in
                                        payload["financial"]["900001.SH"]["runs"]])
        self.assertEqual(payload["preview_only"], True)

    def test_run_saves_internal_artifacts_and_never_adopts(self):
        payload, status = self._run()
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["adopted"])
        self.assertEqual(payload["confirmed_subjects"], ["900001.SH", "900002.SH"])
        self.assertFalse(payload["external_delivery"]["allowed"])
        d = Path(payload["dir"])
        self.assertTrue(d.is_dir())
        self.assertTrue(str(d).startswith(str(self.tmp / "studies")))
        paths = {a["path"] for a in payload["artifacts"]}
        for want in ("summary.md", "study_manifest.json", "financial.json",
                     "events.json", "risk.json", "inputs.json", "previews.json"):
            self.assertIn(want, paths, paths)
        man = json.loads((d / "study_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(man["study_id"], payload["study_id"])
        self.assertEqual(man["research_state"], "research_draft")
        self.assertFalse(man["external_delivery"]["allowed"])

    def test_rerun_does_not_overwrite_an_existing_study(self):
        first, _ = self._run()
        d = Path(first["dir"])
        before = (d / "summary.md").read_bytes()
        again, status = self._run()
        self.assertEqual(status, 200, again)
        self.assertEqual(again["status"], "exists")
        self.assertEqual((d / "summary.md").read_bytes(), before,
                         "旧研究目录不得被覆盖")

    def test_verify_and_replay_reuse_the_frozen_record(self):
        import web_ui
        payload, _ = self._run()
        sid = payload["study_id"]
        h = _Handler("/api/research-study/verify")
        web_ui._post_research_study_verify(h, h.path, {"study_id": sid}, {"user": "admin"})
        ver, status = h.last
        self.assertEqual(status, 200, ver)
        self.assertTrue(ver["ok"], ver)
        h = _Handler("/api/research-study/replay")
        web_ui._post_research_study_replay(h, h.path, {"study_id": sid}, {"user": "admin"})
        rep, status = h.last
        self.assertEqual(status, 200, rep)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["expected_hash"], rep["actual_hash"])

    def test_hypothesis_card_is_draft_and_scenario_only(self):
        import web_ui
        payload, _ = self._run()
        h = _Handler("/api/research-study/hypothesis-card")
        web_ui._post_research_study_hypothesis_card(
            h, h.path, {"study_id": payload["study_id"]}, {"user": "admin"})
        card, status = h.last
        self.assertEqual(status, 200, card)
        body = card["card"]
        self.assertEqual(body["state"], "draft_not_human_confirmed")
        self.assertTrue(body["no_credit_score"])
        self.assertTrue(body["no_alpha_claim"])
        self.assertIn("设定情景", body["scenario_not_signal"])
        self.assertEqual([c["entity_id"] for c in body["companies"]],
                         ["900001.SH", "900002.SH"])
        for c in body["companies"]:
            self.assertTrue(c["next_same_caliber_disclosure"])
            self.assertTrue(c["refutation_conditions"])
            self.assertEqual(c["known_as_of"]["feature_cutoff"], "2025-04-30")
            self.assertIn("设定情景", c["scenario_only"]["note"])
        self.assertIn("未经人工确认", card["markdown_text"])

    def test_hypothesis_card_carries_the_five_field_groups(self):
        """§17.5：身份与时点／研究判断／试验约定／后续结果／使用状态五组字段都要在卡里。"""
        import web_ui
        payload, _ = self._run()
        h = _Handler("/api/research-study/hypothesis-card")
        web_ui._post_research_study_hypothesis_card(
            h, h.path, {"study_id": payload["study_id"]}, {"user": "admin"})
        card, status = h.last
        self.assertEqual(status, 200, card)
        body = card["card"]
        # ① 身份与时点
        self.assertEqual(len(body["hypothesis_id"]), 64)
        self.assertTrue(body["frozen"])
        self.assertEqual(body["method"]["version"].startswith("q1-v1"), True)
        self.assertEqual(body["method"]["status"], "fixed_not_computed",
                         "本批只冻结指标与方向规则，不计算 Q1 读数")
        self.assertIn("自定义经营余额", body["method"]["indicator"])
        self.assertIn("sign(O_t)", body["method"]["direction_rule"])
        self.assertIn("归母净利润变化方向延续", body["method"]["baselines"])
        # ② 研究判断（逐公司）
        for c in body["companies"]:
            self.assertTrue(c["mechanism"])
            self.assertTrue(c["expected_direction"])
            self.assertIn("Y_op", c["observation_metric"])
            self.assertIn("年度", c["target_period"])
            self.assertTrue(c["alternatives"])
        # ③ 试验约定
        tc = body["trial_conventions"]
        self.assertEqual(tc["company_pool"]["entities"], ["900001.SH", "900002.SH"])
        self.assertIn("Y_op", tc["primary_endpoint"])
        self.assertEqual(tc["variants"]["count"], 1)
        self.assertFalse(tc["human_confirmed"])
        # ④ 后续结果：只有状态位，不写实际读数
        for c in body["companies"]:
            fu = c["follow_up"]
            self.assertEqual(fu["status"], "待数据")
            self.assertIsNone(fu["actual_reading"])
            self.assertIsNone(fu["difference"])
            self.assertEqual(fu["mechanism_verdict"], "未决")
            self.assertIn("待数据", fu["market_reaction"]["status"])
        # ⑤ 使用状态
        us = body["usage_state"]
        self.assertTrue(us["internal_only"])
        self.assertIn("流程样例", us["provenance_kind"])
        self.assertIn("不是前瞻判断", us["provenance_note"])
        self.assertEqual(us["research_state"], "research_draft")
        self.assertEqual(us["human_review"], "unreviewed")
        self.assertTrue(us["machine_checks"]["verify_study"]["ok"],
                        us["machine_checks"])
        self.assertFalse(us["external_delivery"]["allowed"])
        # 卡正文也要按五组渲染（人工读的那份不能只有摘要）
        for token in ("## 试验约定", "机制（可反证的一句）", "观察指标", "目标期",
                      "后续结果", "假说ID"):
            self.assertIn(token, card["markdown_text"], token)

    def test_card_hash_ignores_generation_time_but_covers_judgement(self):
        """卡 hash：不含 `generated_at`（同一判断重复生成同 hash），但判断变了必须变。"""
        import web_ui
        payload, _ = self._run()
        h = _Handler("/api/research-study/hypothesis-card")
        web_ui._post_research_study_hypothesis_card(
            h, h.path, {"study_id": payload["study_id"]}, {"user": "admin"})
        first, _ = h.last
        import web_ui as _w
        again = _w._freeze_hypothesis_card(payload["dir"])
        self.assertEqual(first["card"]["card_hash"], again["card_hash"])
        tampered = dict(again)
        tampered["companies"] = [dict(again["companies"][0],
                                      mechanism="被改过的机制")] + again["companies"][1:]
        for_hash = {k: v for k, v in tampered.items() if k != "generated_at"}
        import hashlib as _h
        import json as _j
        changed = _h.sha256(_j.dumps(for_hash, ensure_ascii=False,
                                     sort_keys=True).encode("utf-8")).hexdigest()
        self.assertNotEqual(changed, first["card"]["card_hash"])


class TestStudyEntryIdentityAndTimeGuards(_Base):
    def test_run_requires_preview_confirmation(self):
        payload, status = self._run(confirmed=False)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["code"], "preview_required")

    def test_run_refuses_subjects_not_in_the_files(self):
        payload, status = self._run(confirmed_subjects=["900001.SH"])
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["code"], "subjects_unconfirmed")
        self.assertEqual(payload["subjects_in_files"], ["900001.SH", "900002.SH"])

    def test_subject_is_read_from_the_file_not_from_the_request(self):
        """请求里写另一家公司不会改材料身份：只按**文件里读到**的主体运行。

        反例做法：把财务宽表的证券代码/公司名换成别家，请求仍写原主体 → 必须以
        文件为准并 **409 拒绝**（不是把别家材料重贴成请求公司的标签）。
        """
        csv = (FIN_HEADER + "\n"
               + "600000.SH,别家公司,2023-12-31,年度,2024-04-25,2025-04-30,万元,CNY,合并,"
                 "1000,700,150,160,120,100,140,80,2000,1000\n"
               + "600000.SH,别家公司,2024-12-31,年度,2025-04-25,2025-04-30,万元,CNY,合并,"
                 "1200,840,160,170,100,180,240,100,2400,1300\n")
        self._write_input("id-other", {"financials.csv": csv})
        spec = json.loads((SAMPLE / "study.json").read_text(encoding="utf-8-sig"))
        spec = {k: v for k, v in spec.items()
                if k in ("schema", "name", "synthetic", "feature_cutoff",
                         "evaluation_as_of", "financial")}
        spec["feature_cutoff"] = "2025-04-30"
        spec["evaluation_as_of"] = "2025-06-30"
        payload, status = self._run(spec=spec, input_id="id-other")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["subjects_in_files"], ["600000.SH"])
        # 预览里读到的主体同样是文件里的那家
        prev, pstatus = self._preview({"spec": spec, "input_id": "id-other"})
        self.assertEqual(pstatus, 200, prev)
        self.assertEqual(prev["subjects"], ["600000.SH"])

    def test_spec_file_must_exist_in_this_input(self):
        spec = json.loads((SAMPLE / "study.json").read_text(encoding="utf-8-sig"))
        spec["financial"] = dict(spec["financial"], file="not_uploaded.csv")
        payload, status = self._preview({"spec": spec, "input_id": "__sample__"})
        self.assertEqual(status, 400, payload)
        self.assertIn("not_uploaded.csv", payload["error"])

    def test_unknown_input_dir_is_refused(self):
        payload, status = self._preview({"spec": "sample", "input_id": "no-such-dir"})
        self.assertEqual(status, 400, payload)

    def test_upload_rejects_bad_names_and_payloads(self):
        import base64
        import web_ui
        b64 = base64.b64encode(b"a,b\n1,2\n").decode()
        for name in ("../evil.csv", "sub/dir.csv", "..", ".", ""):
            h = _Handler("/api/research-study/upload")
            web_ui._post_research_study_upload(
                h, h.path, {"filename": name, "data": b64}, {"user": "admin"})
            _payload, status = h.last
            self.assertEqual(status, 400, (name, _payload))
        h = _Handler("/api/research-study/upload")
        web_ui._post_research_study_upload(
            h, h.path, {"filename": "ok.csv", "data": "!!!not-base64!!!"}, {"user": "admin"})
        payload, status = h.last
        self.assertIn(status, (400, 200))
        if status == 200:                      # base64 宽松解码：写进去的必须不是原文
            self.assertEqual(payload["files"][0]["name"], "ok.csv")

    def test_availability_before_disclosure_is_marked_unusable(self):
        """时点反例：可用时间早于披露时间 → 该行不可用于模型（不能进当时信息集）。"""
        csv = (FIN_HEADER + "\n"
               + "900009.SH,时点反例公司,2023-12-31,年度,2025-04-25,2025-04-30,万元,CNY,合并,"
                 "1000,700,150,160,120,100,140,80,2000,1000\n"
               + "900009.SH,时点反例公司,2024-12-31,年度,2025-04-25,2025-04-30,万元,CNY,合并,"
                 "1200,840,160,170,100,180,240,100,2400,1300\n")
        self._write_input("id-time", {"financials.csv": csv})
        spec = {"schema": "weavemind.study_spec/1", "name": "时点反例", "synthetic": True,
                "feature_cutoff": "2025-04-30", "evaluation_as_of": "2025-06-30",
                "financial": {"file": "financials.csv", "periods": [2023, 2024],
                              "metadata": {"source": "synthetic_fixture", "synthetic": True},
                              "models": ["cash_quality"]}}
        payload, status = self._preview({"spec": spec, "input_id": "id-time"})
        # 该表没有“可用时间”列：披露日即用日，属正常；这里核的是**没有假数据补齐**
        self.assertEqual(status, 200, payload)
        # 2 行 × 11 个金额列 = 22 条候选观察（一列一个指标，不是一行一条）
        self.assertEqual(payload["previews"]["financial"]["rows"], 22)
        self.assertEqual(payload["previews"]["financial"]["issue_count"], 0)
        # 明确的时点反例：显式给出早于披露的可用时间
        csv_bad = csv.replace("2025-04-25,2025-04-30", "2025-04-25,2025-04-01")
        self._write_input("id-time-bad", {"financials.csv": csv_bad})
        spec2 = dict(spec, financial=dict(spec["financial"],
                                          metadata={"source": "synthetic_fixture",
                                                    "synthetic": True,
                                                    "available_at": "2025-04-01"}))
        payload2, status2 = self._preview({"spec": spec2, "input_id": "id-time-bad"})
        self.assertEqual(status2, 200, payload2)
        codes = [i["code"] for i in payload2["previews"]["financial"]["issues"]]
        self.assertIn("availability_before_disclosure", codes, codes)
        self.assertEqual(payload2["previews"]["financial"]["eligible_records"], 0,
                         "时点矛盾的行不得算作可用")

    def test_replay_refuses_when_implementation_changed(self):
        import quant_research.study as study
        import web_ui
        payload, _ = self._run()
        sid = payload["study_id"]
        with mock.patch.object(study, "_implementations",
                               lambda: {"changed.py": "deadbeef"}):
            h = _Handler("/api/research-study/replay")
            web_ui._post_research_study_replay(h, h.path, {"study_id": sid},
                                               {"user": "admin"})
            rep, status = h.last
        self.assertEqual(status, 200, rep)
        self.assertFalse(rep["ok"])
        self.assertIn("实现已改变", rep["reason"])

    def test_verify_detects_a_tampered_artifact(self):
        import web_ui
        payload, _ = self._run()
        d = Path(payload["dir"])
        (d / "summary.md").write_text("被改过的摘要", encoding="utf-8")
        h = _Handler("/api/research-study/verify")
        web_ui._post_research_study_verify(h, h.path,
                                           {"study_id": payload["study_id"]},
                                           {"user": "admin"})
        ver, status = h.last
        self.assertEqual(status, 200, ver)
        self.assertFalse(ver["ok"])
        self.assertIn("summary.md", ver["failed"])


if __name__ == "__main__":
    unittest.main()
