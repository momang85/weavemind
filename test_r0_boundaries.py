# -*- coding: utf-8 -*-
"""R0 边界回归（《M0完成复核与下一步修订_20260916》§2）。

覆盖两件事，都走**真实验收入口**（`run_acceptance`），不是断言 helper 返回值：
- R0.4 计算反例：常量角色按**操作数位置**判定；每个财务输入必须绑定 指标/期间/币种/单位；
- R0.1（本文件先落"目标达成 vs 诚实披露"的判定面，后续批次继续扩）。

反例来源是架构复核给出的最小复现；正向对照必须一起保留，避免"收紧阈值把真话否掉"。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import acceptance_checker as ac  # noqa: E402
import workspace as ws_mod  # noqa: E402

DISCLAIMER = (
    "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，不构成任何投资建议；"
    "数据来源于公开渠道，可能存在延迟或误差；据此操作风险自担。\n"
)


def _run(tid: str, goal: str, report: str) -> dict:
    """建临时工作区跑真实验收。

    用户材料的通道就是 **goal 文本**（`run_acceptance` 把 goal 注入为
    `user_material` 来源）——夹具必须按同一接线走，否则材料根本没进判定。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wm_r0_"))
    old_root = ws_mod.WORKSPACE_ROOT
    ws_mod.configure_workspace_root(str(tmp))
    try:
        ws = ws_mod.task_workspace(tid)
        ws.mkdir(parents=True, exist_ok=True)
        return ac.run_acceptance(tid, goal, report, ws, capabilities=["web_search"])
    finally:
        ws_mod.WORKSPACE_ROOT = old_root
        shutil.rmtree(tmp, ignore_errors=True)


def _goal_with_material(base: str, material: str) -> str:
    """把材料并进目标文本（= 生产的 user_material 通道）。"""
    return f"{base}（材料：{material}）" if material else base


def _num(report: str, tid: str, goal: str, user_material: str = "") -> dict:
    """取验收里数字可信度检查的原始结果（含每条数字的四类结论）。"""
    res = _run(tid, _goal_with_material(goal, user_material), report)
    return (res.get("checks") or {}).get("number_traceability") or {}


class TestR04ConstantRoles(unittest.TestCase):
    """常量豁免按位置判定：分母位置的 1 不是换算常量。"""

    def test_operand_roles_flags_denominator_one_as_input(self):
        roles = ac._operand_roles("1000/1-1")
        self.assertEqual(roles, [("1000", "input"), ("1", "input"), ("1", "conversion")],
                         "分母的 1 必须按输入处理，只有末尾 -1 是换算角色")

    def test_operand_roles_allows_growth_and_percent_conversion(self):
        self.assertEqual(ac._operand_roles("1200/1000-1")[-1], ("1", "conversion"))
        self.assertEqual(ac._operand_roles("1200/1000*100")[-1], ("100", "conversion"))

    def test_missing_base_period_cannot_use_one_as_denominator(self):
        """反例 1：材料只有 2025 年收入时，`1000/1-1` 不得认证为增长 99900%。"""
        goal = "分析公司2025年经营表现"
        user_material = "2025年营业收入1000万元"
        report = (
            "# 公司2025年经营分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 营业收入增长 | 99900%（1000/1-1） | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-c1", goal, user_material)
        bad = [u for u in (nt.get("untraceable") or []) if "99900" in str(u.get("raw"))]
        ok_rows = [t for t in (nt.get("traceable") or [])
                   if "99900" in str(t.get("raw")) and t.get("indicator_period") == "ok"]
        self.assertEqual(ok_rows, [], f"缺基期的伪增长不得认证：{ok_rows}")
        self.assertTrue(bad or nt.get("covered_ratio", 1) < 1.0,
                        "该伪增长应计入不可溯源/未绑定，而不是通过")

    def test_correct_growth_with_bound_base_period_still_passes(self):
        """正向对照：材料给出两期收入时，20% 必须仍然通过。"""
        goal = "分析公司2025年经营表现"
        user_material = "2024年营业收入1000万元；2025年营业收入1200万元"
        report = (
            "# 公司2025年经营分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 2025年营业收入增长 | 20%（1200/1000-1） | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-c1p", goal, user_material)
        rows = [t for t in (nt.get("traceable") or []) if "20" in str(t.get("raw"))]
        self.assertTrue(rows, f"正确计算必须可溯源：{nt.get('untraceable')}")
        self.assertEqual(rows[0].get("indicator_period"), "ok",
                         f"指标/期间应绑定为 ok：{rows[0]}")
        self.assertTrue(rows[0].get("arithmetic_ok"))


class TestR04InputRoleBinding(unittest.TestCase):
    """每个必需输入都要绑定 指标/期间/币种——分子对不等于整式对。"""

    def test_indicator_period_mismatch_on_denominator_is_rejected(self):
        """反例 2：2024 收入 + 2025 净利润，写"2025 净利润增长20%（1200/1000-1）"。"""
        goal = "分析公司2025年经营表现"
        user_material = "2024年营业收入1000万元；2025年净利润1200万元"
        report = (
            "# 公司2025年经营分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 2025年净利润增长 | 20%（1200/1000-1） | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-c2", goal, user_material)
        ok_rows = [t for t in (nt.get("traceable") or [])
                   if "20" in str(t.get("raw")) and t.get("indicator_period") == "ok"]
        self.assertEqual(ok_rows, [], f"指标/期间错配不得判 ok：{ok_rows}")

    def test_unrelated_same_value_source_does_not_break_correct_math(self):
        """无关同值来源不得破坏正确计算（联合匹配 + 顺序不敏感）。"""
        goal = "分析公司2025年经营表现"
        user_material = (
            "2024年营业收入1000万元；2025年营业收入1200万元；"
            "2025年资本支出1000亿元"
        )
        report = (
            "# 公司2025年经营分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 2025年营业收入增长 | 20%（1200/1000-1） | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-c2p", goal, user_material)
        rows = [t for t in (nt.get("traceable") or []) if "20" in str(t.get("raw"))]
        self.assertTrue(rows, f"无关同值来源不得破坏正确计算：{nt.get('untraceable')}")
        self.assertEqual(rows[0].get("indicator_period"), "ok")

    def test_currency_mixing_is_rejected(self):
        """币种混用：万元 与 万美元 相除不是增速。"""
        goal = "分析公司2025年经营表现"
        user_material = "2024年营业收入1000万元；2025年营业收入1200万美元"
        report = (
            "# 公司2025年经营分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 2025年营业收入增长 | 20%（1200/1000-1） | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-c3", goal, user_material)
        ok_rows = [t for t in (nt.get("traceable") or [])
                   if "20" in str(t.get("raw")) and t.get("indicator_period") == "ok"]
        self.assertEqual(ok_rows, [], f"币种混用不得判 ok：{ok_rows}")

    def test_currency_helper(self):
        self.assertEqual(ac._currency_of("万元"), "CNY")
        self.assertEqual(ac._currency_of("万美元"), "USD")
        self.assertEqual(ac._currency_of("亿港元"), "HKD")
        self.assertEqual(ac._currency_of("%"), "")


class TestR02VersionIdentity(unittest.TestCase):
    """R0.2：版本身份必须按**完整身份**绑定，不按正文借 PASS。

    反例来自架构复核：sourceA 的正文拿到 pass，再登记同正文的 sourceB 并采纳，
    选中却仍是 sourceA/pass；随后绑定 rules2/fail 又把两份来源记录一起覆盖。
    """

    def setUp(self):
        from report_version import VersionStore

        self.tmp = Path(tempfile.mkdtemp(prefix="wm_r02_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = VersionStore(self.tmp, "t-r02")

    def _acc(self, overall: str, body_hash: str, *, fingerprint: str = "") -> dict:
        return {"overall": overall, "gaps": [],
                "report_sha256": body_hash,
                "report_sha256_short": body_hash[:16],
                "rules_version": "2026.09.12",
                "rules_fingerprint": fingerprint or "rules-1"}

    def test_short_hash_cannot_bind(self):
        """调用方给短 hash → 不绑（该版保持未知）。"""
        from report_version import body_hash

        body = "研究正文"
        v = self.store.record(body)
        short = dict(self._acc("pass", body_hash(body)))
        short["report_sha256"] = body_hash(body)[:16]
        self.assertIsNone(self.store.bind_acceptance(short), "短 hash 不得绑定")
        self.store.adopt(v, reason="t")
        got = self.store.adopted()
        self.assertFalse(bool(got.acceptance), "被拒的绑定不得写入")
        self.assertFalse(got.acceptance_for_this_body())

    def test_identity_does_not_drift_after_bind(self):
        """身份是**入库时**算出的那个，不随事后回填 `rules_fingerprint` 改变。

        实测（真实任务 `ui-01efce721a`）：同一份 `report_versions.json` 里，
        条目键是 `7faeb4d9…`，而重算身份是 `2033fae7…`——因为 `bind_acceptance`
        会把 `rules_fingerprint` 覆盖成验收里的值。页面/清单里的 `report_version_id`
        用的是重算值，与版本库的键对不上，审计无法对账。
        """
        from report_version import VERSIONS_FILE, body_hash

        body = "研究正文（身份稳定性）"
        v = self.store.record(body, sources_fingerprint="src-A",
                              rules_version="r1", rules_fingerprint="fp-1")
        self.store.adopt(v, reason="t")
        identity = v.identity_id()
        raw = json.loads((self.tmp / VERSIONS_FILE).read_text(encoding="utf-8"))
        self.assertIn(identity, raw["versions"], "身份就是入库时的键")

        self.store.bind_acceptance(
            self._acc("fail", body_hash(body), fingerprint="fp-9"),
            sources_fingerprint="src-A")
        got = self.store.adopted()
        self.assertEqual(got.rules_fingerprint, "fp-9", "回填本身要生效")
        self.assertEqual(got.identity_id(), identity,
                         "身份不得因回填而漂移（否则键/身份无法对账）")
        self.assertIn(got.identity_id(), raw and json.loads(
            (self.tmp / VERSIONS_FILE).read_text(encoding="utf-8"))["versions"])
        self.assertTrue(got.identity_drifted(),
                        "底层字段确实变了——这个信号要能被审计看到")

    def test_legacy_short_hash_record_is_needs_reverify(self):
        """磁盘上遗留的短 hash 验收：按"待重验"处理，不得当已验证。"""
        from report_version import VERSIONS_FILE, body_hash

        body = "历史正文"
        v = self.store.record(body)
        data = json.loads((self.tmp / VERSIONS_FILE).read_text(encoding="utf-8"))
        key = v.identity_id()
        data["versions"][key]["acceptance"] = {
            "overall": "pass", "gaps": [], "report_sha256": body_hash(body)[:16]}
        data["selected"] = key
        (self.tmp / VERSIONS_FILE).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        got = self.store.adopted()
        self.assertFalse(got.acceptance_for_this_body(), "短 hash 不得算已验证")
        self.assertTrue(got.acceptance_needs_reverify(), "应标为待重验")

    def test_same_body_two_sources_are_isolated(self):
        """同正文换来源 = 两版证据，绑定互不影响、不互相借 PASS。"""
        from report_version import body_hash

        body = "同一份正文"
        a = self.store.record(body, sources_fingerprint="src-A")
        self.store.bind_acceptance(self._acc("pass", body_hash(body)),
                                   sources_fingerprint="src-A")
        b = self.store.record(body, sources_fingerprint="src-B")
        self.assertNotEqual(a.identity_id(), b.identity_id(), "来源不同 → 身份不同")
        self.store.adopt(b, reason="采纳 sourceB")
        got = self.store.adopted()
        self.assertEqual(str(got.sources_fingerprint), "src-B")
        self.assertFalse(bool(got.acceptance), "sourceB 不得继承 sourceA 的验收")
        # 用正确的指纹绑 fail → 只改 sourceB 那条
        bound = self.store.bind_acceptance(self._acc("fail", body_hash(body), fingerprint="rules-2"),
                                           sources_fingerprint="src-B")
        self.assertIsNotNone(bound)
        self.assertEqual(bound.acceptance_overall(), "fail")
        a_after = self.store.find_identity(a.identity_id())
        self.assertIsNotNone(a_after)
        self.assertEqual(a_after.acceptance_overall(), "pass",
                         "sourceA 的验收不得被 sourceB 的绑定覆盖")
        self.assertEqual(str(a_after.sources_fingerprint), "src-A")

    def test_bind_refuses_when_source_identity_mismatches(self):
        """库里只有 sourceB 时，拿 sourceA 的来源指纹来绑 → 拒绝（不借）。"""
        from report_version import body_hash

        body = "只有一条来源的正文"
        self.store.record(body, sources_fingerprint="src-B")
        self.assertIsNone(
            self.store.bind_acceptance(self._acc("pass", body_hash(body)),
                                       sources_fingerprint="src-A"),
            "来源指纹不符时不得绑定")
        self.assertIsNone(self.store.find_by_body(body).acceptance or None,
                          "被拒的绑定不得写入")

    def test_adopted_does_not_borrow_sibling_acceptance(self):
        """选中条目没有验收时，不得从同正文兄弟条目借。"""
        from report_version import body_hash

        body = "正文"
        a = self.store.record(body, sources_fingerprint="src-A")
        self.store.bind_acceptance(self._acc("pass", body_hash(body)),
                                   sources_fingerprint="src-A")
        b = self.store.record(body, sources_fingerprint="src-B")
        self.store.adopt(b, reason="选 sourceB")
        got = self.store.adopted()
        self.assertEqual(str(got.sources_fingerprint), "src-B")
        self.assertFalse(got.acceptance_for_this_body())
        self.assertNotEqual(got.acceptance_overall(), "pass")

    def test_final_status_summary_uses_selected_and_marks_unbound(self):
        """终态摘要读选中版本自身验收；未绑定时 overall 记空（未知）。"""
        from orchestrator_v2 import OrchestratorV2
        from report_version import body_hash

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._version_stores = {}
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: self.tmp):
            body = "正文"
            v = self.store.record(body, sources_fingerprint="src-A")
            self.store.adopt(v, reason="t")
            summary = o._acceptance_summary_for_status("t-r02")
            self.assertEqual(str(summary.get("overall") or ""), "",
                             "没有验收 → 终态不得读成通过")
            self.assertFalse(summary.get("version_bound"))
            self.store.bind_acceptance(self._acc("pass", body_hash(body)),
                                       sources_fingerprint="src-A")
            summary2 = o._acceptance_summary_for_status("t-r02")
            self.assertEqual(str(summary2.get("overall")), "pass")
            self.assertTrue(summary2.get("version_bound"))


class TestR02Wiring(unittest.TestCase):
    """生产接线必须真的传身份：登记/绑定两处都不能再用默认空指纹。"""

    def test_record_and_bind_sites_pass_identity(self):
        """生产接线必须真的传身份：登记/绑定两处都不能再用默认空指纹。

        B 批把"验收→登记→绑定"收敛到 `delivery_pipeline`（唯一实现）：本守卫跟着
        看那个文件，并要求编排器侧只**委托**它——不许再长出第二份本地实现。
        """
        src = (ROOT / "delivery_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("sources_fingerprint=", src)
        self.assertIn("rules_fingerprint=", src)
        self.assertIn("sources_fingerprint(task_id", src)
        self.assertIn("bind_acceptance(", src)
        # 绑定必须带来源指纹，否则同正文不同来源会被混用
        idx = src.index(".bind_acceptance(")
        self.assertIn("sources_fingerprint=", src[idx:idx + 400])
        orch = (ROOT / "orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertIn("from delivery_pipeline import", orch)
        self.assertNotIn("_store.bind_acceptance(", orch,
                         "绑定只能有一处实现（共享模块），不得在编排器里再写一份")

    def test_acceptance_emits_full_hash(self):
        src = (ROOT / "acceptance_checker.py").read_text(encoding="utf-8")
        self.assertIn('"report_sha256_short"', src)
        # 旧的"截断成 16 位再写成 report_sha256"不得再出现
        self.assertNotIn('"report_sha256": sha256(str(report_text or "").encode("utf-8")).hexdigest()[:16]',
                         src)


class TestR03VerifiedDelivery(unittest.TestCase):
    """R0.3：绑定验收 ≠ 通过验收；六处（谓词/页面/清单/路由/打印件/准入）共用同一状态。

    离线复现（架构给）：登记正文 → 绑 `overall=fail` → adopt → 导出，
    此前得到 `acceptance_overall=fail` 却 `draft=False`。
    """

    def setUp(self):
        from report_version import VersionStore

        self.tmp = Path(tempfile.mkdtemp(prefix="wm_r03_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tid = "t-r03"
        self.store = VersionStore(self.tmp, self.tid)

    def _seed(self, overall: str, *, sources: str = "src-A") -> str:
        from report_version import body_hash

        body = (
            "# 研究正文\n\n营业收入 1000 亿元。\n"
        )
        v = self.store.record(body, sources_fingerprint=sources)
        self.store.bind_acceptance(
            {"overall": overall, "gaps": [], "report_sha256": body_hash(body),
             "rules_version": "2026.09.12", "rules_fingerprint": "fp"},
            sources_fingerprint=sources)
        self.store.adopt(self.store.find_by_body(body), reason="交付")
        return body

    def test_bound_fail_is_not_verified(self):
        from report_version import DELIVERY_DRAFT, verified_delivery

        body = self._seed("fail")
        status, why = verified_delivery(self.store.adopted(), body)
        self.assertEqual(status, DELIVERY_DRAFT, why)
        self.assertIn("验收未通过", why)

    def test_pass_and_bound_is_verified(self):
        from report_version import DELIVERY_VERIFIED, verified_delivery

        body = self._seed("pass")
        status, why = verified_delivery(self.store.adopted(), body)
        self.assertEqual(status, DELIVERY_VERIFIED, why)

    def test_missing_or_unbound_acceptance_is_unknown(self):
        from report_version import DELIVERY_UNKNOWN, VersionStore, body_hash, verified_delivery

        st = VersionStore(self.tmp / "u", "t-u")
        body = "没有验收的正文"
        v = st.record(body, sources_fingerprint="src-A")
        st.adopt(v, reason="t")
        status, _ = verified_delivery(st.adopted(), body)
        self.assertEqual(status, DELIVERY_UNKNOWN)
        # 只有短 hash 的旧记录同样不得算已验证
        st2 = VersionStore(self.tmp / "u2", "t-u2")
        v2 = st2.record(body, sources_fingerprint="src-A")
        st2.bind_acceptance({"overall": "pass", "report_sha256": body_hash(body)[:16]},
                            sources_fingerprint="src-A")
        st2.adopt(st2.find_by_body(body) or v2, reason="t")
        got = st2.adopted()
        if got.acceptance is not None:
            st2.bind_acceptance({"overall": "pass",
                                 "report_sha256": body_hash(body)[:16]},
                                sources_fingerprint="src-A")

    def test_export_manifest_draft_and_route_headers_agree(self):
        """清单/路由/谓词三处状态一致：绑 fail 导出必须是草稿。"""
        from unittest import mock

        import web_ui

        body = self._seed("fail")
        with mock.patch("web_ui.task_workspace", lambda tid: self.tmp):
            manifest = web_ui._write_export_manifest(self.tid, body, b"%PDF-1.4 x")
        self.assertTrue(manifest["draft"], manifest)
        self.assertEqual(manifest["acceptance_overall"], "fail")
        self.assertIn("验收未通过", str(manifest.get("draft_reason") or ""))
        # 路由响应头与清单一致
        from test_report_version import _FakeHandler

        h = _FakeHandler()
        with mock.patch("web_ui.task_workspace", lambda tid: self.tmp),                 mock.patch("web_ui._get_task_report_data",
                           lambda tid: {"report": body, "goal": "目标"}),                 mock.patch("web_ui._task_pdf_bytes", lambda tid: b"%PDF-1.4 x"):
            web_ui._get_task_pdf(h, f"/api/task/{self.tid}/pdf")
        headers = dict(h.headers)
        self.assertEqual(headers.get("X-Report-Draft"), "1",
                         "未验收草稿的 PDF 必须带草稿标记")
        self.assertTrue(headers.get("X-Report-Version-Id"))

    def test_manifest_write_failure_is_visible(self):
        """写不进清单时不得静默：manifest 里要有 manifest_write_error。"""
        from unittest import mock

        import web_ui

        body = self._seed("pass")
        with mock.patch("web_ui.task_workspace", lambda tid: self.tmp),                 mock.patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            manifest = web_ui._write_export_manifest(self.tid, body, b"%PDF-1.4 x")
        self.assertIn("manifest_write_error", manifest)
        self.assertIn("disk full", manifest["manifest_write_error"])


class TestR03FrontendExportGuards(unittest.TestCase):
    """前端源码级守卫：401/403 不自动下载；本地缓存稿必须标注未验证。"""

    def setUp(self):
        self.text = (ROOT / "frontend" / "src" / "components"
                     / "ReportViewer.tsx").read_text(encoding="utf-8")

    def test_auth_errors_are_distinguished(self):
        self.assertIn("exportBlocked", self.text)
        self.assertIn("401", self.text)
        self.assertIn("403", self.text)
        self.assertIn("需登录", self.text)
        self.assertIn("无权限", self.text)

    def test_local_fallback_is_labelled(self):
        self.assertIn("本地未验证副本", self.text)
        self.assertIn("版本未知", self.text)


class TestR04NoRegressionOnExistingFixtures(unittest.TestCase):
    """既有 V1 反例仍然成立（本轮只收紧"常量位置"和"逐操作数绑定"）。"""

    def test_full_formula_with_unit_scale_mismatch_still_rejected(self):
        goal = "分析公司2025年经营表现"
        user_material = "2024年营业收入1000万元；2025年营业收入1200万元"
        report = (
            "# 公司2025年经营分析\n\n营业收入合计 2200亿元（1200+1000），"
            "同比增长 20%（1200/1000-1）。\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-reg1", goal, user_material)
        bad = [t for t in (nt.get("traceable") or []) if "2200" in str(t.get("raw"))]
        self.assertEqual(bad, [], "万元相加结果写成亿元不得通过（量纲错一万倍）")

    def test_substring_boundary_still_enforced(self):
        goal = "分析公司2025年经营表现"
        user_material = "2025年营业收入1200万元"
        report = (
            "# 公司2025年经营分析\n\n| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 收入 | 12亿元 | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-reg2", goal, user_material)
        hit = [t for t in (nt.get("traceable") or []) if str(t.get("raw")).startswith("12")]
        self.assertEqual(hit, [], "12 不得命中 1200（按值匹配，不是子串）")


class TestR01GoalRequirementBoundary(unittest.TestCase):
    """R0.1：目标达成与诚实披露分开，缺必需数据/来源不得整体 pass。

    反例来自架构复核：目标"研究公司2025年经营表现，提供数据并引用官方来源"，
    正文写"营业收入未披露。归母收益未披露。经营净流入未披露。"、来源为空时，
    此前 `run_acceptance` 仍给 `overall=pass`（零数字/无来源反而更容易过）。
    """

    GOAL = "研究公司2025年经营表现，提供数据并引用官方来源"
    BODY = (
        "# 公司2025年经营表现\n\n营业收入未披露。归母收益未披露。经营净流入未披露。\n\n"
        "## 免责声明\n\n本文基于定性判断，不构成任何投资建议。\n"
    )

    def _cov(self, goal: str, body: str) -> dict:
        res = _run("r0-01", goal, body)
        return {"overall": res.get("overall"), "gaps": res.get("gaps") or [],
                "cov": (res.get("checks") or {}).get("requirement_coverage") or {}}

    def test_repro_no_longer_passes(self):
        out = self._cov(self.GOAL, self.BODY)
        self.assertNotEqual(out["overall"], "pass", "缺必需数据与来源不得整体 pass")
        self.assertIn(out["overall"], ("partial", "unknown", "fail"))
        self.assertTrue(out["cov"].get("needs_data"))
        self.assertTrue(out["cov"].get("needs_sources"))
        self.assertTrue(out["cov"].get("honest_disclosure"), "写了'未披露'应识别为诚实披露")
        self.assertTrue(out["gaps"], "必须给出可操作缺口")
        self.assertIn("来源", " ".join(out["gaps"]))

    def test_undisclosed_missing_data_is_fail(self):
        """缺数据且**不披露** → fail（不是 partial）。"""
        body = (
            "# 公司2025年经营表现\n\n公司经营稳健，渠道结构持续优化，"
            "产品结构向高端集中，品牌势能延续。\n\n"
            "## 免责声明\n\n本文基于定性判断，不构成任何投资建议。\n"
        )
        out = self._cov(self.GOAL, body)
        self.assertEqual(out["overall"], "fail", out)
        self.assertFalse(out["cov"].get("honest_disclosure"))

    def test_qualitative_goal_is_not_forced_to_produce_numbers(self):
        """正向对照：明确"定性即可、不需要数字"的研究不得被要求造数字。"""
        goal = "梳理公司渠道与产品结构的主要变化方向（定性分析即可，不需要具体财务数字）"
        body = (
            "# 渠道与产品结构变化\n\n## 渠道\n\n直营与经销比重持续调整，"
            "线上自营平台占比提升。\n\n## 产品结构\n\n高端产品比重上升，系列酒结构优化。\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为公司公开披露与主流财经报道。\n\n"
            "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，"
            "不构成任何投资建议；数据来源于公开渠道，可能存在延迟或误差。\n"
        )
        out = self._cov(goal, body)
        self.assertFalse(out["cov"].get("needs_data"),
                         "定性要求不应被判定为'必须给数字'")
        self.assertNotIn("目标要求给出", " ".join(out["gaps"]))

    def test_requirement_met_passes(self):
        """正向对照：目标要数据与来源，报告给足可溯源数据与来源清单 → pass。"""
        goal = ("分析公司2025年营业收入，给出数据与来源链接"
                "（材料：2025年营业收入1200亿元）")
        body = (
            "# 公司2025年营业收入分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 2025年营业收入 | 1200亿元 | [1] |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31 年报披露日，日终更新。\n\n"
            "## 参考来源\n\n1. [公司2025年年报](https://www.example.com/annual-2025)\n\n"
            "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，"
            "不构成任何投资建议；数据来源于公开渠道，可能存在延迟或误差。\n"
        )
        out = self._cov(goal, body)
        self.assertTrue(out["cov"].get("goal_met"), out)
        self.assertEqual(out["overall"], "pass", out)


class TestFinalBodyAcceptanceBinding(unittest.TestCase):
    """收尾装配正文必须取得**它自己**的验收。

    实测（真实运行 ui-24a59d1c7b，2026-09-17）：验收绑在报告步骤写出的中间正文
    （4565 字节，`fail` + 4 条缺口）上，收尾把交付说明/评审注记/底稿缺口拼成 12031
    字节的正文并采纳——两者字节不同，于是交付状态落回"该版本没有对应它自身的验收
    （未知）"，用户看不到磁盘上已有的 fail 结论。安全方向没错（不是假绿），但结论
    在最后一公里丢了。
    """

    def setUp(self):
        from report_version import VersionStore

        self.tmp = Path(tempfile.mkdtemp(prefix="wm_r02f_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = VersionStore(self.tmp, "t-fin")
        self.detail = "交付说明 + 装配后的正文（这一份才是被采纳的）"

    def _orch(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._version_stores = {}
        return o

    def _patched_ws(self):
        """把版本库/报告目录指到临时工作区（helper 从 workspace 模块导入）。"""
        return mock.patch("workspace.task_workspace", lambda tid: self.tmp)

    def _acc(self, overall: str, sha: str) -> dict:
        return {"overall": overall, "report_sha256": sha, "gaps": ["数字溯源率 0%"],
                "rules_version": "2026.09.12", "rules_fingerprint": "2f00aa11"}

    def test_skips_when_adopted_version_has_its_own_acceptance(self):
        from report_version import body_hash

        v = self.store.record(self.detail, sources_fingerprint="src-A")
        self.store.adopt(v, reason="t")
        self.store.bind_acceptance(self._acc("fail", body_hash(self.detail)),
                                   sources_fingerprint="src-A")
        o = self._orch()
        with self._patched_ws(), \
                mock.patch.object(o, "_run_acceptance_check") as m:
            got, body = o._ensure_final_body_accepted("t-fin", "目标", self.detail)
        self.assertEqual(got, "already")
        self.assertEqual(body, self.detail, "已有本版验收时交付正文不变")
        m.assert_not_called()

    def test_binds_verdict_to_the_adopted_body(self):
        from report_version import body_hash

        v = self.store.record(self.detail, sources_fingerprint="src-A")
        self.store.adopt(v, reason="收尾补齐")
        o = self._orch()

        def _fake_accept(tid, goal, trigger="报告步骤", report_body="", prefer_body=False):
            # 验收器真实行为：对给定正文出结论，并按完整身份绑回该版
            self.assertEqual(report_body, self.detail, "必须验收被采纳的那份正文")
            self.assertTrue(prefer_body, "装配正文不能被磁盘上的旧 report.md 顶掉")
            self.assertEqual(trigger, "最终装配")
            self.store.bind_acceptance(self._acc("fail", body_hash(report_body)),
                                       sources_fingerprint="src-A")
            return {"overall": "fail"}

        with self._patched_ws(), \
                mock.patch.object(o, "_run_acceptance_check", side_effect=_fake_accept):
            got, body = o._ensure_final_body_accepted("t-fin", "目标", self.detail)
        self.assertEqual(got, "bound")
        self.assertEqual(body, self.detail)
        after = self.store.adopted()
        self.assertEqual(after.acceptance_overall(), "fail",
                         "结论要落在被采纳的那一版上，而不是只写在日志里")
        self.assertTrue(after.acceptance_for_this_body())

    def test_unbound_stays_unknown_and_says_so(self):
        """绑定没落地时不得改判：仍是"未知"，并留下可见告警。"""
        v = self.store.record(self.detail, sources_fingerprint="src-A")
        self.store.adopt(v, reason="收尾补齐")
        o = self._orch()
        with self._patched_ws(), \
                mock.patch.object(o, "_run_acceptance_check", return_value={"overall": "pass"}), \
                self.assertLogs("delivery_pipeline", level="WARNING") as lg:
            got, body = o._ensure_final_body_accepted("t-fin", "目标", self.detail)
        self.assertEqual(got, "mismatch")
        self.assertEqual(body, self.detail)
        self.assertTrue(any("未取得本版验收" in m for m in lg.output), lg.output)
        self.assertEqual(self.store.adopted().acceptance_overall(), "")

    def test_repaired_body_becomes_the_delivery(self):
        """验收器修过的正文就是交付正文——否则绑定必然落空、状态又回到"未知"。

        实测（真实运行 `ui-2def3b4241`）：收尾把 13302 字节的 `detail` 送去验收，
        验收器的来源标注修复把它改写成 13458 字节的诚实披露版并绑定了那一版；
        而交付用的还是修复前的 `detail` → 交付状态显示"该版本没有对应它自身的验收"。
        """
        from report_version import body_hash

        repaired = self.detail + "\n（验收器修正：把叙述片段当来源的句子降级为诚实披露）"
        v = self.store.record(self.detail, sources_fingerprint="src-A")
        self.store.adopt(v, reason="收尾补齐")
        o = self._orch()

        def _fake_accept(tid, goal, trigger="报告步骤", report_body="", prefer_body=False):
            self.store.record(repaired, sources_fingerprint="src-A")
            self.store.bind_acceptance(self._acc("fail", body_hash(repaired)),
                                       sources_fingerprint="src-A")
            return {"overall": "fail", "_accepted_body": repaired}

        with self._patched_ws(), \
                mock.patch.object(o, "_run_acceptance_check", side_effect=_fake_accept):
            got, body = o._ensure_final_body_accepted("t-fin", "目标", self.detail)
        self.assertEqual(got, "bound")
        self.assertEqual(body, repaired, "交付正文必须是被验收的那一份（修复版）")
        after = self.store.adopted()
        self.assertEqual(after.acceptance_overall(), "fail")
        self.assertTrue(after.acceptance_for_this_body(),
                        "修复版要成为被采纳版本，交付状态才判得出来")

    def test_prefer_body_beats_stale_report_md(self):
        """`prefer_body=True` 验收调用方给定的正文；默认仍读磁盘 report.md。"""
        from workspace import task_reports_dir

        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old_root)
        rd = task_reports_dir("t-fin")
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "report.md").write_text("磁盘上的中间正文", encoding="utf-8")
        o = self._orch()
        seen = []

        def _fake_run(tid, goal, report, ws, **kw):
            seen.append(report)
            return {"overall": "fail", "gaps": [], "checks": {}}

        with mock.patch("acceptance_checker.run_acceptance", side_effect=_fake_run), \
                mock.patch.dict("os.environ", {"URL_HEALTH_CHECK": "0"}):
            o._run_acceptance_check("t-fin", "目标", trigger="最终装配",
                                    report_body=self.detail, prefer_body=True)
            o._run_acceptance_check("t-fin", "目标", trigger="报告步骤")
        self.assertEqual(seen[0], self.detail)
        self.assertEqual(seen[1], "磁盘上的中间正文")


class TestResearchHardGate(unittest.TestCase):
    """A 批：底稿/必需事实/文档主体不达标 → 交付**硬约束**必须被触发。

    背景：`_delivery(task_id)["hard_fail"]` 此前全仓无人赋值，`verified_delivery` 的
    `hard_ok` 分支永远不可达——底稿写着"未知口径/缺口"，交付却仍可能被判已验证。
    """

    def setUp(self):
        from orchestrator_v2 import OrchestratorV2

        self.o = OrchestratorV2.__new__(OrchestratorV2)
        self.tid = "t-gate"

    def _contract(self):
        from facts import parse_research_request
        return parse_research_request(
            "研究贵州茅台 2023 与 2024 年营业收入、归母净利润、经营活动现金流净额",
            company="贵州茅台", company_id="600519.SH", market="cn",
            periods=[2023, 2024], caliber="合并", as_of="2025-04-30",
            identity_source="form").to_payload()

    def _run_gate(self, wp, report="", contract="default"):
        payload = self._contract() if contract == "default" else (contract or {})
        with mock.patch("task_state.read_task",
                        return_value={"research_request": payload}):
            return self.o._apply_research_hard_gate(self.tid, "目标", wp, report)

    def test_paper_not_ok_triggers_hard_fail(self):
        wp = {"ok": True, "paper_ok": False,
              "problems": [{"kind": "caliber_mismatch",
                            "detail": "营业收入 2024年 的口径是「unknown」"}],
              "gaps": [], "rows_detail": []}
        note = self._run_gate(wp)
        self.assertIn("研究交付硬门槛未通过", note)
        self.assertTrue(self.o._delivery(self.tid)["hard_fail"],
                        "硬约束必须被写进交付状态（否则谓词那一分支永远不可达）")
        # 已有"验收通过"的版本，但硬约束不满足 → 交付只能是草稿
        from report_version import VersionStore, body_hash, verified_delivery
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="wm_gate_"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        store = VersionStore(tmp, self.tid)
        v = store.record("正文", sources_fingerprint="src-A")
        store.adopt(v, reason="t")
        store.bind_acceptance({"overall": "pass", "gaps": [],
                               "report_sha256": body_hash("正文")},
                              sources_fingerprint="src-A")
        self.assertEqual(store.adopted().acceptance_overall(), "pass")
        status, _why = verified_delivery(
            store.adopted(), "正文",
            hard_ok=not bool(self.o._delivery(self.tid).get("hard_fail")),
            hard_reason=str(self.o._delivery(self.tid).get("hard_fail") or ""))
        self.assertEqual(status, "draft", "硬约束不满足时交付只能是草稿")

    def test_missing_working_paper_triggers_gate(self):
        note = self._run_gate({"ok": False, "skipped": True,
                               "reason": "没有结构化财务，不产出底稿"})
        self.assertIn("底稿缺失", note)
        self.assertTrue(self.o._delivery(self.tid)["hard_fail"])

    def test_non_research_task_is_not_gated(self):
        """普通任务：既没有研究契约、也没有底稿 → 不进门槛（不强制要底稿）。"""
        note = self._run_gate({"ok": False, "skipped": True}, contract=None)
        self.assertEqual(note, "")
        self.assertFalse(self.o._delivery(self.tid)["hard_fail"])

    def test_document_scope_unbound_triggers_gate(self):
        """文档作用域没绑定请求主体（标题是别家公司）→ 触发门槛。"""
        wp = {"ok": True, "paper_ok": True, "problems": [], "gaps": [],
              "rows_detail": [{"metric": "revenue", "metric_label": "营业收入",
                               "period": "2024年", "value": 9999.0}]}
        report = ("# 比亚迪 2024 年度研究\n\n| 指标 | 2024 |\n|---|---|\n"
                  "| 营业收入（亿元） | 9999.0 |\n")
        note = self._run_gate(wp, report=report)
        self.assertIn("文档主体作用域", note)
        self.assertTrue(self.o._delivery(self.tid)["hard_fail"])

    def test_scoped_report_does_not_trigger(self):
        wp = {"ok": True, "paper_ok": True, "problems": [], "gaps": [],
              "rows_detail": [{"metric": "revenue", "metric_label": "营业收入",
                               "period": "2024年", "value": 1741.44}]}
        report = ("# 贵州茅台 2024 年度研究\n\n| 指标 | 2024 |\n|---|---|\n"
                  "| 营业收入（亿元） | 1741.44 |\n")
        self.assertEqual(self._run_gate(wp, report=report), "")
        self.assertFalse(self.o._delivery(self.tid)["hard_fail"])

    def test_scope_check_exception_fails_closed(self):
        """校验异常必须**明确失败**：不能只记日志、留空 hard_fail（A′3 故障注入确认）。

        此前 `document_subject_scope` 抛异常被吞掉 → 门槛返回空说明、hard_fail 仍为空，
        等于"我们没算出来"就放行。研究必需校验异常要按证据未知/草稿处理。
        """
        wp = {"ok": True, "paper_ok": True, "problems": [], "gaps": [],
              "rows_detail": [{"metric": "revenue", "metric_label": "营业收入",
                               "period": "2024年", "value": 1741.44}]}
        with mock.patch("working_paper.document_subject_scope",
                        side_effect=RuntimeError("注入：作用域判定失败")):
            note = self._run_gate(wp, report="# 贵州茅台\n\n营业收入 1741.44 亿元\n")
        self.assertIn("研究交付硬门槛未通过", note)
        self.assertTrue(self.o._delivery(self.tid)["hard_fail"],
                        "校验异常不得留空 hard_fail")

    def test_contract_read_exception_fails_closed(self):
        """契约读取异常同样不得跳过关卡：研究任务身份不能因异常丢失。"""
        wp = {"ok": False, "skipped": True, "reason": "没有结构化财务"}
        with mock.patch("task_state.read_task", side_effect=RuntimeError("注入：库不可读")):
            note = self.o._apply_research_hard_gate(self.tid, "目标", wp, "")
        self.assertTrue(self.o._delivery(self.tid)["hard_fail"])
        self.assertIn("研究契约读取异常", self.o._delivery(self.tid)["hard_fail"])


class TestAnalysisCompleteness(unittest.TestCase):
    """分析完整性（研究任务）：正文有没有把必需事实讲全。

    实机 `ui-2084c2c9cc` 的教训：底稿 6/6 事实 + 3 条同比齐全、硬门槛通过，正文却只写了
    2 个数字、没有任何同比/质量/结构分析——既有检查一个都拦不住。本检查**只提示**：
    `counted=False`，缺口进 `checks.analysis_completeness`，不进 overall 的 gaps 汇总。
    """

    GOAL = ("研究示例制造 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30")
    ROWS = [
        {"year": 2023, "report_type": "年报", "revenue": 1200.0, "net_profit": 150.0,
         "operating_cashflow": 210.0, "total_assets": 900.0, "total_liabilities": 300.0,
         "disclosure_date": "2024-04-03"},
        {"year": 2024, "report_type": "年报", "revenue": 1380.0, "net_profit": 174.0,
         "operating_cashflow": 231.0, "total_assets": 1000.0, "total_liabilities": 350.0,
         "disclosure_date": "2025-04-03"},
    ]
    THIN = ("# 示例制造财务研究\n\n归母净利润 174 亿元（2024）、150 亿元（2023）。\n\n"
            "## 免责声明\n\n本文基于公开数据整理，不构成任何投资建议。\n")
    FULL = (
        "# 示例制造财务研究\n\n"
        "## 关键数据一览\n\n"
        "| 指标 | 2023 | 2024 | 同比 |\n|---|---|---|---|\n"
        "| 营业收入 | 1200 | 1380 | 15% |\n| 归母净利润 | 150 | 174 | 16% |\n"
        "| 经营活动现金流净额 | 210 | 231 | 10% |\n\n"
        "营业收入 1200 / 1380 亿元；归母净利润 150 / 174 亿元；"
        "经营活动现金流净额 210 / 231 亿元。\n"
        "同比：营业收入 15%、归母净利润 16%、经营现金流 10%。\n"
        "归母净利率 2023 年 12.5%、2024 年 12.61%；"
        "经营现金流对归母净利润的覆盖 2023 年 140%、2024 年 132.76%；"
        "资产负债率 2023 年 33.33%、2024 年 35%。\n\n"
        "## 风险提示\n\n需求与价格波动可能影响收入与毛利，需结合行业数据判断。\n\n"
        "## 结论\n\n两期收入与归母净利润均增长，经营现金流同步改善。\n\n"
        "## 免责声明\n\n本文基于公开数据整理，不构成任何投资建议。\n"
    )

    def _run_with_paper(self, report: str) -> dict:
        import facts as F
        import task_state
        tmp = Path(tempfile.mkdtemp(prefix="wm_ana_"))
        old_root = ws_mod.WORKSPACE_ROOT
        old_db = task_state.DB_PATH
        ws_mod.configure_workspace_root(str(tmp))
        task_state.DB_PATH = str(tmp / "ana.db")
        try:
            tid = "ana-01"
            req = F.parse_research_request(
                self.GOAL, company="示例制造", company_id="000001.SZ", market="cn",
                periods=[2023, 2024], caliber="合并", as_of="2025-04-30",
                identity_source="form")
            task_state.mark_queued(tid, goal=self.GOAL,
                                   research_request=req.to_payload(),
                                   db_path=task_state.DB_PATH)
            proj = ws_mod.task_project_dir(tid, "default")
            proj.mkdir(parents=True, exist_ok=True)
            (proj / "financials.json").write_text(
                json.dumps({"financials": self.ROWS,
                            "metadata": {"source": "eastmoney_ashare",
                                         "company": "示例制造", "currency": "CNY",
                                         "unit": "亿元", "caliber": "合并",
                                         "caliber_evidence": "含 PARENTNETPROFIT"},
                            "raw": {"url": "https://example.invalid/a", "text": "{}"}},
                           ensure_ascii=False), encoding="utf-8")
            ws = ws_mod.task_workspace(tid)
            ws.mkdir(parents=True, exist_ok=True)
            return ac.run_acceptance(tid, self.GOAL, report, ws,
                                     capabilities=["web_search"])
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            task_state.DB_PATH = old_db
            shutil.rmtree(tmp, ignore_errors=True)

    def test_thin_report_is_flagged_but_does_not_drive_overall(self):
        res = self._run_with_paper(self.THIN)
        chk = (res.get("checks") or {}).get("analysis_completeness") or {}
        self.assertTrue(chk.get("applicable"), chk)
        self.assertFalse(chk.get("pass"), chk)
        self.assertFalse(chk.get("counted"), "只提示：不得计入 overall")
        gaps = chk.get("analysis_gaps") or []
        blob = " ".join(gaps)
        self.assertIn("营业收入", blob, "缺口要点名缺失的指标")
        self.assertIn("经营活动现金流净额", blob)
        self.assertTrue(any("同比" in g for g in gaps), gaps)
        # 只提示：这些缺口不得混进 overall 的 gaps 汇总
        self.assertNotIn("分析要素", " ".join(res.get("gaps") or []))

    def test_complete_report_passes(self):
        res = self._run_with_paper(self.FULL)
        chk = (res.get("checks") or {}).get("analysis_completeness") or {}
        self.assertTrue(chk.get("pass"), chk)
        self.assertEqual(chk.get("facts_missing"), 0)
        self.assertEqual(chk.get("derived_missing"), 0)

    def test_bare_numbers_with_placeholders_do_not_pass(self):
        """架构复核 P2 反例：六个裸数字 + "尚未分析/待写" 不得算作分析完整。

        此前"概念词一出现就算分析"（写了"同比、净利率"就满足派生覆盖），于是
        "六个数字 + 风险：待写 + 结论：待写" 也能 pass——检查必须诚实。
        """
        bare = (
            "# 示例制造财务研究\n\n"
            "营业收入 1200 / 1380；归母净利润 150 / 174；经营活动现金流净额 210 / 231。\n"
            "同比、归母净利率、现金流覆盖等指标尚未分析。\n\n"
            "## 风险提示\n\n风险：待写。\n\n## 结论\n\n结论：待写。\n"
        )
        res = self._run_with_paper(bare)
        chk = (res.get("checks") or {}).get("analysis_completeness") or {}
        self.assertFalse(chk.get("pass"), chk)
        self.assertFalse(chk.get("counted"), "仍只提示、不计入 overall")
        self.assertTrue(chk.get("facts_missing", 0) >= 1
                        or chk.get("facts_unexplained", 0) >= 1,
                        f"裸数字不得算已解释：{chk}")
        self.assertGreater(chk.get("derived_missing", 0), 0,
                           "只写'同比/净利率'这类词不算给出读数")
        self.assertFalse(chk.get("risk_ok"), "占位风险段落不算覆盖")
        self.assertFalse(chk.get("conclusion_ok"), "占位结论段落不算覆盖")
        blob = " ".join(chk.get("analysis_gaps") or [])
        self.assertIn("占位", blob)

    def test_numbers_without_metric_binding_are_not_explanations(self):
        """数字出现但没跟指标名绑定 → 不算解释（避免"数字散落各处"冒充分析）。"""
        body = ("# 示例制造财务研究\n\n本期若干科目出现变化：1200、1380、150、174、"
                "210、231 亿元。\n\n## 风险提示\n\n需求与价格波动会影响毛利。\n\n"
                "## 结论\n\n经营保持增长。\n\n## 免责声明\n\n不构成任何投资建议。\n")
        res = self._run_with_paper(body)
        chk = (res.get("checks") or {}).get("analysis_completeness") or {}
        self.assertGreater(chk.get("facts_unexplained", 0), 0, chk)
        self.assertIn("未与该指标绑定", " ".join(chk.get("analysis_gaps") or []))

    def test_non_research_task_is_not_applicable(self):
        """没有底稿的任务不适用（不误报）。"""
        res = _run("ana-02", "用三句话说明毛利率与净利率的区别", "毛利率与净利率的区别如下。")
        chk = (res.get("checks") or {}).get("analysis_completeness") or {}
        self.assertFalse(chk.get("applicable"), chk)
        self.assertTrue(chk.get("pass"))


if __name__ == "__main__":
    unittest.main()
