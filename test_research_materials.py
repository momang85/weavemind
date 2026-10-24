# -*- coding: utf-8 -*-
"""RC2-1024 · A3 回归：研究任务材料投影与显式物化（不起服务、不碰生产库）。

覆盖任务书 §3 A3 与验收 A 的六项：
1. 跨账号隔离：A/B 两个用户互相看不到对方的任务材料（投影 + 物化两条路都验）；
2. 不可用原因显式：无权限 / 任务不存在 / 底稿缺失 / 无准入事实；
3. 无准入事实的任务 → 拒绝物化（不生成空数据集）；
4. 显式 fact_ids → 数据集 provenance 带 parent_task_id / fact_ids，
   official=False（服务端判定）、user_provided=True（非官方投影）；
5. 未选的事实不进数据集；缺期间 / 缺字段 / 缺披露日逐条说明，不补零、不猜；
6. 同内容重复提交复用同一 dataset_id；损坏的数据集不被当作"成功复用"
   （复用 store 的行为——StoreWriteError 原样抛出，不在本层吞掉）。

隔离纪律（与 test_screen_prepare.py 同口径）：临时目录由本脚本 mkdtemp 创建，
`_wipe()` 只在确认路径属于本脚本创建的目录之后才清理；任务库用 AGENTS_DB 指向
自己的临时 SQLite，筛选存储用 WEAVEMIND_SCREEN_DIR 指向自己的临时目录，
任务工作区用 WEAVEMIND_WORKSPACE_ROOT 指向自己的临时目录——
绝不读写仓库 agents.db / workspace/ 下的真实数据。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.environ.setdefault("WEAVEMIND_TEST_MODE", "1")
# RC2-B 纪律：测试目录由本脚本创建（mkdtemp），_wipe() 只清理该目录。
TEST_ROOT = tempfile.mkdtemp(prefix="weavemind-a3-research-")
os.environ["WEAVEMIND_SCREEN_DIR"] = os.path.join(TEST_ROOT, "screen")
os.environ["AGENTS_DB"] = os.path.join(TEST_ROOT, "agents.db")
os.environ["WEAVEMIND_WORKSPACE_ROOT"] = os.path.join(TEST_ROOT, "workspace")

import research_materials as rm          # noqa: E402  (env 已就位后再 import)
import screen_store as st                # noqa: E402
import task_state as ts                  # noqa: E402
import workspace as ws                   # noqa: E402

SCOPE_A = {"user": "a3a", "project": "personal"}
SCOPE_B = {"user": "a3b", "project": "personal"}

T_A = "a3-task-a"            # a3a 的完整任务：有底稿 + 准入事实 + 来源准入留痕
T_B = "a3-task-b"            # a3b 的任务（A 不该看到）
T_PENDING = "a3-task-pending"    # 底稿全是待核验 → 无准入事实
T_NOPAPER = "a3-task-nopaper"    # 任务在库里，但没有工作区/底稿
T_NOOWNER = "a3-task-noowner"    # 没有提交人身份的历史任务
F_REV_2024 = "fact-rev-2024"
F_REV_2023 = "fact-rev-2023"
F_REV_NODISC = "fact-rev-2022-nodisclosure"
F_REV_Q3 = "fact-rev-2024-q3"
F_NP_PENDING = "fact-np-pending"
F_NP_UNRELATED = "fact-np-unrelated"
F_CF_CONFLICT = "fact-cf-conflict"
F_GP_DERIVED = "fact-gp-derived"
F_REV_2024_B = "fact-rev-2024-b"


def _wipe():
    """只清理本脚本创建的临时目录；先验证再动手，绝不递归到真实数据。"""
    root = os.path.abspath(TEST_ROOT)
    for p in (st.screen_base(), str(ws.WORKSPACE_ROOT), ts.DB_PATH):
        ap = os.path.abspath(str(p))
        if ap != root and not ap.startswith(root + os.sep):
            raise RuntimeError(f"拒绝清理非本测试创建的路径：{ap}")
    for sub in ("screen", "workspace"):
        shutil.rmtree(os.path.join(root, sub), ignore_errors=True)
    for name in ("agents.db", "agents.db-wal", "agents.db-shm"):
        try:
            os.remove(os.path.join(root, name))
        except FileNotFoundError:
            pass


def _fact(fid: str, *, metric: str, period: str, value, unit: str = "亿元",
          entity: str = "伊利股份", code: str = "600887.SH", market: str = "cn",
          caliber: str = "合并", currency: str = "CNY", period_end: str = "",
          period_type: str = "年报", disclosed_at: str = "2025-04-29",
          caliber_source: str = "row", caliber_evidence: str = "行内声明口径「合并」",
          source_url: str = "http://static.cninfo.com.cn/finalpage/2025-04-29/1224118570.PDF",
          locator: dict | None = None, verify_state: str = "verified",
          formula: str = "", derived_from: list | None = None) -> dict:
    return {
        "fact_id": fid, "entity": entity, "entity_id": code, "metric": metric,
        "metric_label": metric, "period": period, "period_start": "", "period_end": period_end,
        "period_type": period_type, "period_kind": "", "period_label": "",
        "currency": currency, "unit": unit, "unit_source": "source",
        "value": value, "raw_value": value, "caliber": caliber,
        "caliber_source": caliber_source, "caliber_evidence": caliber_evidence,
        "market": market, "disclosed_at": disclosed_at, "retrieved_at": "2025-04-30",
        "source_url": source_url, "source_hash": "sha-" + fid,
        "source_locator": locator if locator is not None else
        {"kind": "annual_table", "table": "合并利润表", "row": "营业收入"},
        "verify_state": verify_state, "extracted_by": "adapter",
        "formula": formula, "derived_from": derived_from or [],
        "derived": bool(formula or derived_from),
    }


def _selection(selected: list[str], pending: list[dict] | None = None,
               unrelated: list[dict] | None = None, conflicts: list[dict] | None = None) -> dict:
    return {"selected": selected, "pending": pending or [],
            "unrelated": unrelated or [], "conflicts": conflicts or []}


def _paper(rows: list[dict], selection: dict, *, ok: bool = True) -> dict:
    return {"schema": "working_paper/1", "request": {}, "rows": rows, "derived": [],
            "gaps": [], "problems": [], "selection": selection, "ok": ok}


def _task_ws(task_id: str, project: str = "default") -> str:
    """创建任务 project/ 目录（测试夹具，只在本脚本临时根里）。"""
    p = os.path.join(TEST_ROOT, "workspace", "projects", project, task_id, "project")
    os.makedirs(p, exist_ok=True)
    return p


def _write_paper(task_id: str, paper: dict, project: str = "default") -> str:
    d = _task_ws(task_id, project)
    path = os.path.join(d, "working_paper.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(paper, fh, ensure_ascii=False, indent=1)
    return path


def _add_task(task_id: str, user: str, *, project: str = "default",
              goal: str = "", status: str = "SUCCESS_WITH_ISSUES",
              updated_at: str = "2026-10-23 00:35:39",
              created_at: str = "2026-10-23 00:11:27",
              research_request: dict | None = None) -> None:
    con = sqlite3.connect(ts.DB_PATH)
    try:
        con.execute(
            "INSERT INTO task_history (task_id, goal, status, created_at, updated_at,"
            " user, project, research_request_json) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, goal or f"研究伊利股份（600887.SH）2024 年报",
             status, created_at, updated_at, user, project,
             json.dumps(research_request or {}, ensure_ascii=False)))
        con.commit()
    finally:
        con.close()


def _full_paper_rows() -> tuple[list[dict], dict]:
    """T_A 的底稿：2 条准入营收 + 各一条待核验/剔除/冲突/派生/无法定期的记录。"""
    rows = [
        _fact(F_REV_2024, metric="revenue", period="2024年", value=1157.80),
        _fact(F_REV_2023, metric="revenue", period="2023年", value=1261.79,
              disclosed_at="2024-04-30"),
        _fact(F_REV_NODISC, metric="revenue", period="2022年", value=1231.71,
              disclosed_at=""),
        _fact(F_REV_Q3, metric="revenue", period="2024年三季度", value=289.0,
              period_type="三季报"),
        _fact(F_NP_PENDING, metric="net_profit", period="2024年", value=104.29,
              caliber="", caliber_source="", caliber_evidence=""),
        _fact(F_NP_UNRELATED, metric="net_profit", period="2024年", value=42.0,
              entity="蒙牛乳业", code="02319.HK", market="hk", disclosed_at="2025-03-26"),
        _fact(F_CF_CONFLICT, metric="operating_cashflow", period="2024年", value=167.0),
        _fact(F_GP_DERIVED, metric="gross_profit", period="2024年", value=390.0,
              formula="revenue - operating_cost", derived_from=["fact-rev-2024-x"]),
    ]
    sel = _selection(
        [F_REV_2024, F_REV_2023, F_REV_NODISC, F_REV_Q3, F_CF_CONFLICT, F_GP_DERIVED],
        pending=[{"fact_id": F_NP_PENDING, "caliber": "unknown",
                  "reason": "口径未知或与请求不符：仅展示，不用于认证"}],
        unrelated=[{"fact_id": F_NP_UNRELATED, "entity": "蒙牛乳业",
                    "entity_id": "02319.HK", "market": "hk"}],
        conflicts=[{"metric": "operating_cashflow", "period": "2024年",
                    "candidates": [{"fact_id": F_CF_CONFLICT, "value": 167.0},
                                   {"fact_id": F_CF_CONFLICT + "-b", "value": 999.0}],
                    "detail": "同一组合两条互不相容的候选"}])
    return rows, sel


def _paper_for(task_id: str, rows: list[dict], selection: dict, *,
               project: str = "default", ok: bool = True) -> None:
    _write_paper(task_id, _paper(rows, selection, ok=ok), project)


def _prepare_db_and_tasks() -> None:
    ts.ensure_schema(str(ts.DB_PATH))
    rows, sel = _full_paper_rows()
    _paper_for(T_A, rows, sel)
    # T_A 的来源准入留痕：材料摄取 1 准入 1 拒绝 + 检索剔除 1
    d = _task_ws(T_A)
    os.makedirs(os.path.join(d, "materials"), exist_ok=True)
    with open(os.path.join(d, "materials", "index.json"), "w", encoding="utf-8") as fh:
        json.dump([
            {"material_id": "m1", "url": "http://www.cninfo.com.cn/ann/annual-2024.pdf",
             "title": "伊利股份2024年年度报告", "status": "admitted", "reason": "",
             "provenance_label": "官方披露原文", "source_class": "official",
             "period": "2024年", "doc_type": "annual_report",
             "disclosure_date": "2025-04-29", "text_sha256": "aa11"},
            {"material_id": "m2", "url": "http://forum.example.com/post-1.html",
             "title": "网友整理的业绩快报", "status": "rejected",
             "reason": "非官方来源：主体/期间证据不足", "source_class": "community",
             "period": "2024年", "doc_type": "post",
             "disclosure_date": "", "text_sha256": ""},
        ], fh, ensure_ascii=False)
    with open(os.path.join(d, "source_exclusions.json"), "w", encoding="utf-8") as fh:
        json.dump({"task_id": T_A, "count": 1, "excluded": [
            {"url": "http://ads.example.com/promo", "title": "推广页",
             "reason": "经相关性判定与本标的无关"}]}, fh, ensure_ascii=False)
    # T_B：自己的完整任务（A 不该看到）
    _paper_for(T_B, [_fact(F_REV_2024_B, metric="revenue", period="2024年",
                           value=288.76, entity="光明乳业", code="600597.SH",
                           disclosed_at="2025-04-25")], _selection([F_REV_2024_B]))
    # T_PENDING：底稿存在但全是待核验
    _paper_for(T_PENDING, [_fact(F_NP_PENDING, metric="net_profit", period="2024年",
                                 value=104.29, caliber="", caliber_source="",
                                 caliber_evidence="")],
               _selection([], pending=[{"fact_id": F_NP_PENDING, "caliber": "unknown",
                                        "reason": "口径未知：仅展示，不用于认证"}]))
    # T_NOOWNER：历史/系统任务，没有提交人
    _add_task(T_NOOWNER, "", status="FAILED")
    _add_task(T_A, "a3a", updated_at="2026-10-23 00:35:39")
    _add_task(T_B, "a3b", updated_at="2026-10-23 01:00:00")
    _add_task(T_PENDING, "a3a", status="FAILED")
    # T_NOPAPER：工作区在、但没有任何底稿/结构化载荷（"无底稿" ≠ "工作区缺失"）
    _task_ws(T_NOPAPER)
    _add_task(T_NOPAPER, "a3a", status="SUCCESS_WITH_ISSUES")


class Projection(unittest.TestCase):
    """投影：跨账号隔离 + 显式不可用原因。"""

    def setUp(self):
        _wipe()
        _prepare_db_and_tasks()

    def test_cross_account_isolation_both_directions(self):
        """1）A/B 互相看不到对方任务材料：投影列表 + 单任务投影两条路。"""
        proj_a = rm.materials_from_research(SCOPE_A)
        ids_a = [t["task_id"] for t in proj_a["tasks"]]
        self.assertIn(T_A, ids_a)
        self.assertNotIn(T_B, ids_a, "B 的任务材料不能出现在 A 的投影里")
        proj_b = rm.materials_from_research(SCOPE_B)
        ids_b = [t["task_id"] for t in proj_b["tasks"]]
        self.assertIn(T_B, ids_b)
        self.assertNotIn(T_A, ids_b, "A 的任务材料不能出现在 B 的投影里")
        self.assertGreaterEqual(proj_a["totals"]["excluded_other_owner"], 1)
        item_a = rm.research_material(SCOPE_A, T_A)
        self.assertTrue(item_a["usable"])
        item_b_in_a = rm.research_material(SCOPE_A, T_B)
        self.assertFalse(item_b_in_a["usable"])
        self.assertEqual(item_b_in_a["title"], "（不展示他人任务标题）",
                         "无权限时也不泄露他人任务标题")
        # 物化通道同样拒绝
        data, errs = rm.collect_from_research(SCOPE_A, task_id=T_B,
                                              fact_ids=[F_REV_2024_B])
        self.assertIsNone(data)
        self.assertTrue(any("无权限" in e for e in errs), f"要显式说无权限：{errs}")

    def test_unavailable_reasons_explicit(self):
        """2）不可用原因显式：无权限 / 任务不存在 / 底稿缺失 / 无准入事实。"""
        proj = rm.materials_from_research(SCOPE_A)
        by_id = {t["task_id"]: t for t in proj["tasks"]}
        self.assertEqual(by_id[T_NOPAPER]["unavailable"]["code"], "working_paper_missing")
        self.assertIn("底稿缺失", by_id[T_NOPAPER]["unavailable"]["reason"])
        self.assertEqual(by_id[T_PENDING]["unavailable"]["code"], "no_admitted_facts")
        self.assertIn("无准入事实", by_id[T_PENDING]["unavailable"]["reason"])
        miss = rm.research_material(SCOPE_A, "a3-does-not-exist")
        self.assertEqual(miss["unavailable"]["code"], "task_not_found")
        self.assertIn("任务不存在", miss["unavailable"]["reason"])
        for tid in (T_B, T_NOOWNER):
            denied = rm.research_material(SCOPE_A, tid)
            self.assertEqual(denied["unavailable"]["code"], "no_permission")
            self.assertIn("无权限", denied["unavailable"]["reason"],
                          f"{tid} 必须显式说无权限（管理员/系统任务不自动开放）")
        # 历史无主任务不进任何人的投影，但排除计数要透明
        proj_b = rm.materials_from_research(SCOPE_B)
        self.assertEqual(proj_b["totals"]["excluded_unattributed"], 1)
        self.assertNotIn(T_NOOWNER, [t["task_id"] for t in proj_b["tasks"]])

    def test_projection_lists_facts_sources_and_working_paper(self):
        """投影内容：准入事实带来源定位；底稿带哈希；来源准入三态分账。"""
        item = rm.research_material(SCOPE_A, T_A)
        self.assertTrue(item["working_paper"]["present"])
        self.assertTrue(item["working_paper"]["sha256"])
        self.assertEqual(item["facts_source"], "working_paper.json")
        admitted = [f for f in item["facts"] if f["admission"] == "admitted"]
        rev = next(f for f in admitted if f["fact_id"] == F_REV_2024)
        self.assertEqual(rev["metric"], "revenue")
        self.assertEqual(rev["value"], 1157.80)
        self.assertIn("cninfo", rev["source_url"])
        self.assertTrue(rev["locator"], "准入事实要带来源定位")
        self.assertEqual(item["facts_admitted"], 5,
                         "selected 减去被 unrelated/conflicts 改判的 2 条")
        self.assertEqual(item["facts_pending"], 1)
        self.assertEqual(item["facts_excluded"], 2,
                         "主体不符 + 冲突候选都归入剔除，并逐条记原因")
        adm = item["admitted_sources"]
        self.assertTrue(adm["present"])
        self.assertEqual(adm["counts"]["adopted"], 1)
        self.assertEqual(adm["counts"]["excluded"], 2,
                         "材料摄取 1 拒绝 + 检索剔除 1，分开记")
        self.assertEqual(adm["adopted"][0]["title"], "伊利股份2024年年度报告")
        self.assertEqual(adm["adopted"][0]["provenance_label"], "官方披露原文")

    def test_financials_fallback_without_paper(self):
        """没有底稿但有 financials.json + 契约 → 结构化事实投影（只读，不落盘）。"""
        d = _task_ws("a3-task-fin")
        fin = {"metadata": {"company": "伊利股份", "ticker": "600887.SH",
                            "market": "cn", "currency": "CNY", "unit": "亿元"},
               "financials": [{"year": 2024, "report_type": "年报",
                               "report_date": "2024-12-31", "revenue": 1157.80,
                               "net_profit": 104.29, "caliber": "合并",
                               "disclosure_date": "2025-04-29"}]}
        with open(os.path.join(d, "financials.json"), "w", encoding="utf-8") as fh:
            json.dump(fin, fh, ensure_ascii=False)
        _add_task("a3-task-fin", "a3a",
                  research_request={"goal": "研究伊利股份", "company": "伊利股份",
                                    "company_id": "600887.SH", "market": "cn",
                                    "periods": [2024], "caliber": "合并",
                                    "required_metrics": ["revenue", "net_profit"]})
        item = rm.research_material(SCOPE_A, "a3-task-fin")
        self.assertIn("financials.json", item["facts_source"])
        self.assertGreaterEqual(item["facts_admitted"], 1,
                                "契约在库里：结构化事实应按同一条认证路径选出")
        rev = next(f for f in item["facts"] if f["metric"] == "revenue"
                   and f["admission"] == "admitted")
        self.assertEqual(rev["period_end"], "2024-12-31", "report_date 就是期末")


class CollectMaterialization(unittest.TestCase):
    """物化：显式选定 + provenance + 逐项说明 + 复用/损坏。"""

    def setUp(self):
        _wipe()
        _prepare_db_and_tasks()

    def test_requires_explicit_fact_selection(self):
        """不带 fact_ids 拒绝：不把任务里的事实整批自动带入。"""
        data, errs = rm.collect_from_research(SCOPE_A, task_id=T_A, fact_ids=[])
        self.assertIsNone(data)
        self.assertTrue(any("未显式选择" in e or "显式" in e for e in errs), errs)
        self.assertEqual(st.list_datasets(SCOPE_A), [], "拒绝时不能生成数据集")

    def test_task_without_admitted_facts_refused(self):
        """3）无准入事实的任务 → 拒绝物化（不生成空数据集）。"""
        data, errs = rm.collect_from_research(SCOPE_A, task_id=T_PENDING,
                                              fact_ids=[F_NP_PENDING])
        self.assertIsNone(data)
        self.assertTrue(any("无准入事实" in e for e in errs), errs)
        self.assertEqual(st.list_datasets(SCOPE_A), [])

    def test_explicit_facts_provenance_and_official_flags(self):
        """4）显式 fact_ids → provenance 带 parent_task_id/fact_ids；official=False、
        user_provided=True（服务端判定，用户不可自封官方）。"""
        data, errs = rm.collect_from_research(SCOPE_A, task_id=T_A,
                                              fact_ids=[F_REV_2024, F_REV_2023])
        self.assertEqual(errs, [])
        self.assertEqual(data["status"], "ok")
        meta = data["dataset"]
        self.assertFalse(meta["official"], "官方身份只能来自官方缓存，任务事实投影不是官方")
        # T1.2/G5：研究材料有**自己的稳定类型**，不再借 user_provided 冒充
        self.assertEqual(meta["provenance_kind"], "research_material")
        _meta, payload, load_errs = st.load_dataset(SCOPE_A, meta["dataset_id"])
        self.assertEqual(load_errs, "", "load_dataset 复验：数据完整")
        prov = payload["metadata"]["provenance"]
        self.assertEqual(prov["kind"], "research_material")
        self.assertEqual(prov["acquisition"], "task_fact_projection")
        self.assertFalse(prov["independent_verification"], "投影不冒充独立核验")
        self.assertEqual(prov["parent_task_id"], T_A)
        self.assertEqual(sorted(prov["fact_ids"]), sorted([F_REV_2024, F_REV_2023]))
        self.assertTrue(prov["working_paper_ref"]["sha256"])
        listing = st.list_datasets(SCOPE_A)
        entry = next(d for d in listing if d["dataset_id"] == meta["dataset_id"])
        self.assertFalse(entry["official"])
        self.assertTrue(entry["user_provided"], "list 视角：非官方即用户提供")
        rows = payload["rows"]
        self.assertEqual(len(rows), 2)
        by_period = {r["period"]: r for r in rows}
        self.assertAlmostEqual(by_period["2024-12-31"]["revenue"], 1157.80 * 1e8,
                               msg="值统一成元（亿元 × 1e8），不做亿元舍入")
        self.assertEqual(by_period["2024-12-31"]["unit"], "元")
        self.assertEqual(by_period["2024-12-31"]["source_url"],
                         "http://static.cninfo.com.cn/finalpage/2025-04-29/1224118570.PDF")
        self.assertIn(F_REV_2024, by_period["2024-12-31"]["orig_fact_ids"])
        origin = data["origin"]
        self.assertEqual(origin["parent_task_id"], T_A)
        self.assertEqual(origin["fact_ids"], sorted([F_REV_2024, F_REV_2023]))
        counters = data["counters"]
        self.assertEqual((counters["network_attempts"], counters["network_requests"],
                          counters["adapter_calls"]), (0, 0, 0),
                         "本入口不发起任何网络/适配器调用")

    def test_unselected_facts_excluded_and_skips_explained(self):
        """5）未选事实不进数据集；待核验/剔除/冲突/派生/缺期间/缺披露日逐条说明。"""
        data, errs = rm.collect_from_research(
            SCOPE_A, task_id=T_A,
            fact_ids=[F_REV_2024, F_REV_2023, F_REV_NODISC, F_REV_Q3, F_NP_PENDING,
                      F_NP_UNRELATED, F_CF_CONFLICT, F_GP_DERIVED],
            periods=["2024", "2025"])
        self.assertEqual(errs, [])
        _meta, payload, load_errs = st.load_dataset(SCOPE_A, data["dataset"]["dataset_id"])
        self.assertEqual(load_errs, "", "load_dataset 复验：数据完整")
        periods = {r["period"] for r in payload["rows"]}
        self.assertEqual(periods, {"2024-12-31"})
        self.assertNotIn("2023-12-31", periods, "未选期间不物化（2023 未在 periods 里）")
        skipped = {s["fact_id"]: s for s in data["skipped"] if s.get("fact_id")}
        self.assertIn("未选期间", skipped[F_REV_2023]["reason"])
        self.assertIn("披露日", skipped[F_REV_NODISC]["reason"],
                      "缺来源披露日：PIT 无法证明，不补日期")
        self.assertIn("期间无法确定", skipped[F_REV_Q3]["reason"],
                      "季报期末不猜（只物化年报口径期间）")
        self.assertIn("待核验", skipped[F_NP_PENDING]["reason"])
        self.assertIn("剔除", skipped[F_NP_UNRELATED]["reason"],
                      "主体不符的记录不物化")
        self.assertIn("冲突", skipped[F_CF_CONFLICT]["reason"],
                      "同组合冲突候选不替用户选")
        self.assertIn("派生", skipped[F_GP_DERIVED]["reason"],
                      "派生值由筛选侧按同行数据复算，不重复物化")
        gaps = [s for s in data["skipped"] if not s.get("fact_id")]
        self.assertTrue(gaps, "用户显式点名的期间没有材料时要逐期间说明")

    def test_idempotent_reuse_same_content(self):
        """6a）同内容重复提交 → 同一 dataset_id、reused=True，不新增副本。"""
        data1, errs1 = rm.collect_from_research(SCOPE_A, task_id=T_A,
                                                fact_ids=[F_REV_2024, F_REV_2023])
        self.assertEqual(errs1, [])
        data2, errs2 = rm.collect_from_research(SCOPE_A, task_id=T_A,
                                                fact_ids=[F_REV_2023, F_REV_2024])
        self.assertEqual(errs2, [])
        self.assertTrue(data2["reused"])
        self.assertEqual(data1["dataset"]["dataset_id"], data2["dataset"]["dataset_id"])
        self.assertEqual(len(st.list_datasets(SCOPE_A)), 1, "同内容不堆积第二个数据集")
        # 同一内容对不同账号是不同数据集：作用域隔离不能被内容寻址打穿
        data3, errs3 = rm.collect_from_research(SCOPE_B, task_id=T_B,
                                                fact_ids=[F_REV_2024_B])
        self.assertEqual(errs3, [])
        self.assertNotEqual(data3["dataset"]["dataset_id"],
                            data1["dataset"]["dataset_id"])
        self.assertEqual(len(st.list_datasets(SCOPE_B)), 1)

    def test_corrupted_dataset_not_reported_as_reuse(self):
        """6b）数据集被改坏 → 再收一次必须显式失败（StoreWriteError），不冒充复用成功。"""
        data, errs = rm.collect_from_research(SCOPE_A, task_id=T_A,
                                              fact_ids=[F_REV_2024, F_REV_2023])
        self.assertEqual(errs, [])
        did = data["dataset"]["dataset_id"]
        rows_path = os.path.join(st.datasets_root(SCOPE_A), did, "rows.json")
        with open(rows_path, "r+b") as fh:          # 只改自己作用域里的临时数据集
            fh.write(b"CORRUPTED")
        with self.assertRaises(st.StoreWriteError):
            rm.collect_from_research(SCOPE_A, task_id=T_A,
                                     fact_ids=[F_REV_2024, F_REV_2023])
        # 原目录保持原样（不重写、不隐藏），损坏状态可以被用户看到
        listing = st.list_datasets(SCOPE_A)
        self.assertIn(did, [d["dataset_id"] for d in listing])


if __name__ == "__main__":
    unittest.main(verbosity=2)
