# -*- coding: utf-8 -*-
"""TRIAL-READY-1024 · T1 回归：研究材料入口（G5 来源契约 / G6 权限先于分页）。

覆盖任务书 §四 T1 六条中本模块可闭环的五条：
1. G6 权限先于分页：800+ 条他人新增任务**不能**把本人旧任务挤没
   （owner 等值限定在查询层做，先限定再排序/分页；排除计数透明）；
2. G5 来源契约：研究材料有稳定类型 `research_material` + 四个独立布尔
   （user_provided/official/synthetic/independent_verification）；
   数据集来源身份 `origin_identity` 是消费方的**规范答案**——存储元信息与
   来源身份不一致时显式列出（不静默），历史 `user_provided` + parent_task_id
   读取时归一成 research_material（只读归一，**不改写**已冻结的数据集文件）；
3. T1.3 接口缺口：材料条目带「能回到哪份原版」（origin/working_paper_version）
   与「哪些当前字段能算」（fields/usable_fields，与筛选同一份 FIELD_SPECS 判据）；
4. T1.4：无底稿 / 底稿损坏 / 结构化载荷损坏 / 无准入事实 / 不可计算 /
   工作区缺失 / 无权限——**每种不可用都显式给码**，损坏 ≠ 没有材料；
5. T1.5：选择绑定底稿内容哈希（working_paper sha256）——底稿在选择与提交之间
   被更正时拒绝按旧选择物化；
6. T1.6：真实导出链路（eastmoney 适配器（仅传输层 mock）→ financials.json
   （生产写盘形状）→ working_paper_export.write_working_paper → 投影 → 物化）。

隔离纪律：临时目录由本脚本 mkdtemp 创建，`_wipe()` 只在确认路径属于本脚本创建的
目录之后才清理；任务库 AGENTS_DB / 筛选存储 WEAVEMIND_SCREEN_DIR / 任务工作区
WEAVEMIND_WORKSPACE_ROOT / 审计 AUDIT_FILE / 日志 WEAVEMIND_LOG_DIR 全部指向
自己的临时目录——**绝不**读写仓库 agents.db / workspace/ / logs/audit.jsonl。
不起服务、不 commit、不联网（唯一的"网络"是 mock 掉的 eastmoney._get）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.environ.setdefault("WEAVEMIND_TEST_MODE", "1")
TEST_ROOT = tempfile.mkdtemp(prefix="weavemind-t1-")
os.environ["WEAVEMIND_SCREEN_DIR"] = os.path.join(TEST_ROOT, "screen")
os.environ["AGENTS_DB"] = os.path.join(TEST_ROOT, "agents.db")
os.environ["WEAVEMIND_WORKSPACE_ROOT"] = os.path.join(TEST_ROOT, "workspace")
# 上次 A3 的教训：smoke 曾往仓库 logs/audit.jsonl 写了 2 行——这次审计/日志全部重定向
os.environ["AUDIT_FILE"] = os.path.join(TEST_ROOT, "audit.jsonl")
os.environ["WEAVEMIND_LOG_DIR"] = os.path.join(TEST_ROOT, "logs")

import adapters.cninfo as cninfo          # noqa: E402
import adapters.eastmoney as eastmoney    # noqa: E402
import research_materials as rm           # noqa: E402
import screen_store as st                 # noqa: E402
import task_state as ts                   # noqa: E402
import working_paper_export as wpe        # noqa: E402
import workspace as ws                    # noqa: E402

SCOPE = {"user": "t1u", "project": "personal"}
OTHER = {"user": "t1other", "project": "personal"}

# ---------------- 夹具助手（只在本脚本临时根里写） ----------------


def _wipe():
    """只清理本脚本创建的临时目录；先验证再动手。"""
    root = os.path.abspath(TEST_ROOT)
    for p in (st.screen_base(), str(ws.WORKSPACE_ROOT), ts.DB_PATH):
        ap = os.path.abspath(str(p))
        if ap != root and not ap.startswith(root + os.sep):
            raise RuntimeError(f"拒绝清理非本测试创建的路径：{ap}")
    for sub in ("screen", "workspace", "logs"):
        shutil.rmtree(os.path.join(root, sub), ignore_errors=True)
    for name in ("agents.db", "agents.db-wal", "agents.db-shm", "audit.jsonl"):
        try:
            os.remove(os.path.join(root, name))
        except FileNotFoundError:
            pass


def _ws(task_id: str, project: str = "default") -> str:
    p = os.path.join(TEST_ROOT, "workspace", "projects", project, task_id, "project")
    os.makedirs(p, exist_ok=True)
    return p


def _add_task(task_id: str, user: str, *, project: str = "default", goal: str = "",
              status: str = "SUCCESS_WITH_ISSUES",
              created_at: str = "2026-10-23 00:11:27",
              updated_at: str = "2026-10-23 00:35:39",
              research_request: dict | None = None) -> None:
    con = sqlite3.connect(ts.DB_PATH)
    try:
        con.execute(
            "INSERT INTO task_history (task_id, goal, status, created_at, updated_at,"
            " user, project, research_request_json) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, goal or "研究伊利股份（600887.SH）2024 年报", status,
             created_at, updated_at, user, project,
             json.dumps(research_request or {}, ensure_ascii=False)))
        con.commit()
    finally:
        con.close()


def _fact(fid: str, *, metric: str, period: str, value, unit: str = "亿元",
          entity: str = "伊利股份", code: str = "600887.SH", market: str = "cn",
          caliber: str = "合并", currency: str = "CNY", period_end: str = "",
          period_type: str = "年报", disclosed_at: str = "2025-04-29",
          caliber_source: str = "row", caliber_evidence: str = "行内声明口径「合并」",
          verify_state: str = "verified") -> dict:
    return {"fact_id": fid, "entity": entity, "entity_id": code, "metric": metric,
            "metric_label": metric, "period": period, "period_start": "",
            "period_end": period_end, "period_type": period_type, "period_kind": "",
            "period_label": "", "currency": currency, "unit": unit,
            "unit_source": "source", "value": value, "raw_value": value,
            "caliber": caliber, "caliber_source": caliber_source,
            "caliber_evidence": caliber_evidence, "market": market,
            "disclosed_at": disclosed_at, "retrieved_at": "2025-04-30",
            "source_url": "http://static.cninfo.com.cn/finalpage/2025-04-29/1224118570.PDF",
            "source_hash": "sha-" + fid,
            "source_locator": {"kind": "annual_table", "table": "合并利润表",
                               "row": "营业收入"},
            "verify_state": verify_state, "extracted_by": "adapter",
            "formula": "", "derived_from": [], "derived": False}


def _paper(rows: list, selection: dict) -> dict:
    return {"schema": "working_paper/1", "request": {}, "rows": rows, "derived": [],
            "gaps": [], "problems": [], "selection": selection, "ok": True}


def _sel(selected: list, pending=None, unrelated=None, conflicts=None) -> dict:
    return {"selected": selected, "pending": pending or [], "unrelated": unrelated or [],
            "conflicts": conflicts or []}


def _sha_of(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _snapshot_tree(root: str) -> dict:
    """目录里每个文件的 (字节数, sha256, mtime_ns)——用于"未改写"断言。"""
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(base, name)
            st_ = os.stat(p)
            out[os.path.relpath(p, root)] = (st_.st_size, _sha_of(p), st_.st_mtime_ns)
    return out


# ---------------- G6：权限先于分页 ----------------


def _prepare_g6_db() -> None:
    """架构复核 G6 场景：本人 1 条**旧**任务 + 800 条**更新**的他人任务 + 3 条无主任务。"""
    ts.ensure_schema(str(ts.DB_PATH))
    con = sqlite3.connect(ts.DB_PATH)
    try:
        # 本人旧任务：created 2020-01-01（被 800 条 2026-01-01 的新任务压在最后）
        con.execute(
            "INSERT INTO task_history (task_id, goal, status, created_at, updated_at,"
            " user, project, research_request_json) VALUES (?,?,?,?,?,?,?,?)",
            ("t1-owned-old", "旧任务：研究伊利股份 2024 年报", "SUCCESS_WITH_ISSUES",
             "2020-01-01 00:00:00", "2020-01-01 00:00:00", "t1u", "personal", "{}"))
        # 800 条他人新增任务（比旧任务新 → 旧扫描"created_at DESC 后分页"会把它挤掉）
        rows = [("t1-other-%04d" % i, "他人任务 %d" % i, "SUCCESS_WITH_ISSUES",
                 "2026-01-01 00:00:00", "2026-01-01 00:00:00", "t1other", "personal", "{}")
                for i in range(800)]
        # 本人另外 4 条较新的任务（验证分页不丢不重）
        rows += [("t1-owned-%d" % i, "本人任务 %d" % i, "SUCCESS_WITH_ISSUES",
                  "2026-0%d-01 00:00:00" % (5 - i), "2026-0%d-01 00:00:00" % (5 - i),
                  "t1u", "personal", "{}") for i in range(1, 5)]
        # 3 条没有提交人身份的历史任务（不属于任何人，永远不进投影）
        rows += [("t1-noowner-%d" % i, "历史系统任务 %d" % i, "FAILED",
                  "2026-01-02 00:00:00", "2026-01-02 00:00:00", "", "personal", "{}")
                 for i in range(3)]
        con.executemany(
            "INSERT INTO task_history (task_id, goal, status, created_at, updated_at,"
            " user, project, research_request_json) VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()


class G6OwnershipBeforePagination(unittest.TestCase):
    """G6：owner 限定必须在**查询层**做——他人新增任务挤不掉本人旧任务。"""

    def setUp(self):
        _wipe()
        _prepare_g6_db()

    def test_owned_old_task_not_hidden_by_800_newer_other_tasks(self):
        """本人旧任务 + 800 条他人新任务：旧扫描会挤掉它，现在必须还在。"""
        ids, err, counts = rm._scoped_task_rows(SCOPE, 200)
        self.assertEqual(err, "", f"任务库读取失败：{err}")
        ids = [r["task_id"] for r in ids]
        self.assertIn("t1-owned-old", ids,
                      "G6 回归：800 条他人任务不能把本人旧任务挤没")
        self.assertNotIn("t1-other-0799", ids, "他人任务不能出现在本人投影里")
        self.assertEqual(counts["owned_total"], 5)
        self.assertEqual(counts["other_owner"], 800,
                         "排除计数透明：800=他人（无主 3 条单独计 unattributed）")
        self.assertEqual(counts["unattributed"], 3)
        self.assertEqual(counts["scanned"], 5, "扫描的只是本人名下的行")
        self.assertEqual(counts.get("scope_filter"), "owner_equality(sql)",
                         "限定必须在 SQL 层（先限定再排序分页）")
        # 单任务投影也还在
        item = rm.research_material(SCOPE, "t1-owned-old")
        self.assertEqual(item["task_id"], "t1-owned-old")
        self.assertTrue(item["unavailable"], "没底稿就显式给原因，不是消失")

    def test_pagination_no_overlap_no_loss(self):
        """分页只作用在本人任务上：limit=2 三页不重不漏，has_more 逐页诚实。"""
        pages = []
        for off in (0, 2, 4):
            rows, err, counts = rm._scoped_task_rows(SCOPE, 2, off)
            self.assertEqual(err, "")
            pages.append(([r["task_id"] for r in rows], counts))
        self.assertEqual([p[0] for p in pages],
                         [["t1-owned-1", "t1-owned-2"], ["t1-owned-3", "t1-owned-4"],
                          ["t1-owned-old"]], "排序：created_at DESC（最新在前）")
        self.assertTrue(pages[0][1]["has_more"] and pages[1][1]["has_more"])
        self.assertFalse(pages[2][1]["has_more"])
        union = [i for ids, _c in pages for i in ids]
        self.assertEqual(len(union), 5, "三页正好覆盖本人全部 5 条任务")
        self.assertEqual(len(set(union)), 5, "页与页不重叠")
        # 语义层：offset=4 正好拿到旧任务（老代码会拿到他人任务的第 5 条）
        proj = rm.materials_from_research(SCOPE, limit=2, offset=4)
        self.assertEqual([t["task_id"] for t in proj["tasks"]], ["t1-owned-old"])
        self.assertEqual(proj["paging"]["filter"], "先按提交人等值限定，再排序/分页（G6）")

    def test_unattributed_and_empty_scope_never_returned(self):
        """无主任务（user=''）不属于任何人；空用户作用域什么都不能拿到。"""
        empty = {"user": "", "project": "personal"}
        rows, _err, counts = rm._scoped_task_rows(empty, 10)
        self.assertEqual(rows, [], "空用户作用域不能把无主任务当自己的")
        self.assertEqual(counts["owned_total"], 0)
        for tid in ("t1-noowner-0", "t1-noowner-2"):
            item = rm.research_material(SCOPE, tid)
            self.assertEqual(item["unavailable"]["code"], "no_permission",
                             "历史无主任务对任何账号都显式无权限")

    def test_projection_totals_are_honest(self):
        """语义层 totals：本人 5 条、排除计数透明、不泄露他人任务 id。"""
        proj = rm.materials_from_research(SCOPE)
        ids = [t["task_id"] for t in proj["tasks"]]
        self.assertEqual(len(ids), 5)
        self.assertFalse([i for i in ids if i.startswith("t1-other-")],
                         "任何他人任务 id 都不能出现")
        self.assertEqual(proj["totals"]["owned_total"], 5)
        self.assertEqual(proj["totals"]["excluded_other_owner"], 800)
        self.assertEqual(proj["totals"]["excluded_unattributed"], 3)


# ---------------- G5：来源契约 ----------------


def _prepare_g5_paper() -> str:
    """一个带底稿+准入事实的任务（G5 类共用）。"""
    ts.ensure_schema(str(ts.DB_PATH))
    rows = [_fact("t1-f-rev-2024", metric="revenue", period="2024年", value=1157.80,
                  period_end="2024-12-31"),
            _fact("t1-f-rev-2023", metric="revenue", period="2023年", value=1261.79,
                  period_end="2023-12-31", disclosed_at="2024-04-30")]
    _ws("t1-g5-task")
    with open(os.path.join(TEST_ROOT, "workspace", "projects", "default", "t1-g5-task",
                           "project", "working_paper.json"), "w", encoding="utf-8") as fh:
        json.dump(_paper(rows, _sel(["t1-f-rev-2024", "t1-f-rev-2023"])), fh,
                  ensure_ascii=False)
    _add_task("t1-g5-task", "t1u")
    return os.path.join(TEST_ROOT, "workspace", "projects", "default", "t1-g5-task",
                        "project", "working_paper.json")


class G5OriginContract(unittest.TestCase):
    """G5：research_material 稳定类型 + 四布尔；origin_identity 是规范答案。"""

    def setUp(self):
        _wipe()
        self.paper_path = _prepare_g5_paper()

    def test_canonical_research_origin_flags(self):
        """研究材料的来源身份：kind=research_material + 四个独立布尔。"""
        o = rm._research_origin(parent_task_id="t1-g5-task",
                                working_paper_ref={"sha256": "abc", "rows": 2})
        self.assertEqual(o["kind"], "research_material")
        self.assertEqual(o["kind"], rm.RESEARCH_ORIGIN)
        self.assertIn("底稿", o["origin"], "origin 字段要说明能回到哪份原版")
        self.assertEqual(o["acquisition"], "task_fact_projection")
        self.assertTrue(o["user_provided"])
        self.assertFalse(o["official"])
        self.assertFalse(o["synthetic"])
        self.assertFalse(o["independent_verification"], "投影不冒充独立核验")
        self.assertEqual(o["working_paper_ref"]["sha256"], "abc")
        # kind 枚举是稳定的（G5：不能让消费方靠猜）
        self.assertIn("research_material", rm.ORIGIN_KINDS)
        self.assertIn("user_provided", rm.ORIGIN_KINDS)
        self.assertIn("official_cache_materialized", rm.ORIGIN_KINDS)
        self.assertIn("synthetic_demo", rm.ORIGIN_KINDS)
        self.assertIn("unknown", rm.ORIGIN_KINDS)

    def test_collect_writes_research_kind_and_flags(self):
        """物化出的数据集：provenance.kind=research_material + 四布尔贯穿到落盘。"""
        data, errs = rm.collect_from_research(SCOPE, task_id="t1-g5-task",
                                              fact_ids=["t1-f-rev-2024"])
        self.assertEqual(errs, [])
        meta = data["dataset"]
        self.assertEqual(meta["provenance_kind"], "research_material")
        _m, payload, load_err = st.load_dataset(SCOPE, meta["dataset_id"])
        self.assertEqual(load_err, "")
        prov = payload["metadata"]["provenance"]
        self.assertEqual(prov["kind"], "research_material")
        self.assertEqual(prov["acquisition"], "task_fact_projection")
        self.assertTrue(prov["user_provided"])
        self.assertFalse(prov["official"])
        self.assertFalse(prov["synthetic"])
        self.assertFalse(prov["independent_verification"])
        self.assertEqual(prov["working_paper_ref"]["sha256"], _sha_of(self.paper_path))

    def test_origin_identity_is_the_canonical_answer(self):
        """数据集来源身份的规范答案：与存储元信息不一致时显式列出，不静默。"""
        data, errs = rm.collect_from_research(SCOPE, task_id="t1-g5-task",
                                              fact_ids=["t1-f-rev-2024"])
        self.assertEqual(errs, [])
        origin = data["dataset_origin"]
        self.assertEqual(origin["kind"], "research_material")
        self.assertTrue(origin["user_provided"], "规范值：研究材料是本人提供的")
        self.assertFalse(origin["official"])
        self.assertFalse(origin["synthetic"])
        self.assertFalse(origin["independent_verification"])
        meta = data["dataset"] or {}
        if meta.get("user_provided") is False or meta.get("provenance_kind") != "research_material":
            # 现状（screen_store 写死 user_provided=False、不认识 research_material）：
            # 不一致必须可见——不能静默让消费方拿到矛盾的标志
            self.assertFalse(origin["consistent"],
                             "存储元信息与来源身份不一致时必须显式列出")
            self.assertTrue(origin["mismatch"])
            self.assertIn("screen_store", origin["store_fix_required"])
            # 但数据集自身已把规范答案带出来了：消费方读 dataset_origin 不被误导
            self.assertEqual(data["dataset_origin"]["kind"], "research_material")
        else:
            self.assertTrue(origin["consistent"])
            self.assertEqual(origin["mismatch"], [])

    def test_legacy_user_provided_dataset_reads_as_research_material(self):
        """历史数据（A3 写的 kind=user_provided + parent_task_id）读取时归一成
        research_material——**只读归一，不改写已冻结的数据集文件**。"""
        legacy_prov = {
            "kind": "user_provided", "kind_label": "本人研究材料（任务事实投影）",
            "user_provided": True, "official": False, "synthetic": False,
            "label": "本人研究材料（任务 t1-g5-task 事实投影）",
            "parent_task_id": "t1-g5-task", "parent_task_version": "2026-10-23 00:35:39",
            "fact_ids": ["t1-f-rev-2024"], "facts_source": "working_paper.json",
            "module_version": "a3-1",
        }
        meta, reused, errors = st.save_dataset_from_source(
            scope=SCOPE, name="历史投影数据集",
            rows=[{"period": "2024-12-31", "revenue": 1157.80 * 1e8, "unit": "元",
                   "entity": "伊利股份", "code": "600887.SH"}],
            provenance=legacy_prov)
        self.assertEqual(errors, [])
        self.assertFalse(reused)
        _m, payload, load_err = st.load_dataset(SCOPE, meta["dataset_id"])
        self.assertEqual(load_err, "")
        root = st.datasets_root(SCOPE)
        before = _snapshot_tree(root)
        ident = rm.origin_identity(meta=meta,
                                   provenance=payload["metadata"]["provenance"])
        self.assertEqual(ident["kind"], "research_material",
                         "历史 user_provided + parent_task_id → research_material")
        self.assertTrue(ident["legacy"])
        self.assertEqual(ident["legacy_kind"], "user_provided")
        self.assertTrue(ident["user_provided"])
        self.assertFalse(ident["official"])
        self.assertFalse(ident["synthetic"])
        self.assertFalse(ident["independent_verification"])
        self.assertTrue(ident["user_provided"], "规范值：研究材料是本人提供的")
        meta_up = bool(meta.get("user_provided"))
        if meta_up:
            self.assertTrue(ident["consistent"])
        else:
            # 现状（screen_store 写死 user_provided=False）：与规范值不一致必须显式列出
            self.assertFalse(ident["consistent"], "不一致要显式列出，不静默")
            self.assertIn("meta.user_provided",
                          [m.get("field") for m in ident["mismatch"]])
        after = _snapshot_tree(root)
        self.assertEqual(before, after, "归一是读取视角：绝不改写已冻结的数据集文件")

    def test_store_gap_is_visible(self):
        """T1.3 缺口留痕：screen_store 还不认识 research_material 时必须**可见**
        （降级成 unknown 不能静默冒充正确）。修复后这条自动转为正向断言。"""
        out = st.dataset_source_meta({"provenance_kind": "research_material",
                                      "official": False})
        got_kind = (out.get("provenance") or {}).get("kind")
        if "research_material" in getattr(st, "_PROVENANCE_KINDS", set()):
            self.assertEqual(got_kind, "research_material",
                             "存储层已认识 research_material，不得降级")
        else:
            self.assertEqual(got_kind, "unknown",
                             "现状留痕：screen_store 不认识 research_material，"
                             "被降级成 unknown——已列入给 lead 的修改清单")


# ---------------- T1.3 + T1.4：条目形状与显式不可用 ----------------


def _prepare_shape_db() -> None:
    """各种可用/不可用形态的任务（T1.3/T1.4 共用）。"""
    ts.ensure_schema(str(ts.DB_PATH))
    ok_rows = [_fact("t1-f-rev-2024", metric="revenue", period="2024年", value=1157.80,
                     period_end="2024-12-31"),
               _fact("t1-f-rev-2023", metric="revenue", period="2023年", value=1261.79,
                     period_end="2023-12-31", disclosed_at="2024-04-30"),
               _fact("t1-f-gp-2024", metric="gross_profit", period="2024年",
                     value=390.0, period_end="2024-12-31")]
    _ws("t1-shape-ok")
    with open(os.path.join(TEST_ROOT, "workspace", "projects", "default", "t1-shape-ok",
                           "project", "working_paper.json"), "w", encoding="utf-8") as fh:
        json.dump(_paper(ok_rows, _sel(["t1-f-rev-2024", "t1-f-rev-2023",
                                        "t1-f-gp-2024"])), fh, ensure_ascii=False)
    _add_task("t1-shape-ok", "t1u")

    pending = [_fact("t1-f-np-pend", metric="net_profit", period="2024年", value=104.29,
                     caliber="", caliber_source="", caliber_evidence="")]
    _ws("t1-shape-pending")
    with open(os.path.join(TEST_ROOT, "workspace", "projects", "default",
                           "t1-shape-pending", "project", "working_paper.json"),
              "w", encoding="utf-8") as fh:
        json.dump(_paper(pending, _sel([], pending=[{
            "fact_id": "t1-f-np-pend", "caliber": "unknown",
            "reason": "口径未知：仅展示，不用于认证"}])), fh, ensure_ascii=False)
    _add_task("t1-shape-pending", "t1u")

    _ws("t1-shape-corrupt")
    with open(os.path.join(TEST_ROOT, "workspace", "projects", "default",
                           "t1-shape-corrupt", "project", "working_paper.json"),
              "w", encoding="utf-8") as fh:
        fh.write("{这 不是 JSON")                       # 底稿损坏
    _add_task("t1-shape-corrupt", "t1u")

    _ws("t1-shape-fincorrupt")
    with open(os.path.join(TEST_ROOT, "workspace", "projects", "default",
                           "t1-shape-fincorrupt", "project", "financials.json"),
              "w", encoding="utf-8") as fh:
        fh.write("[坏掉的载荷")                          # 结构化载荷损坏（无底稿）
    _add_task("t1-shape-fincorrupt", "t1u")

    # 准入了但**算不出**：roe 不在筛选宽表白名单（单位 %，不是可换算金额）
    notcomp = [_fact("t1-f-roe", metric="roe", period="2024年", value=23.5,
                     unit="%", period_end="2024-12-31")]
    _ws("t1-shape-notcomp")
    with open(os.path.join(TEST_ROOT, "workspace", "projects", "default",
                           "t1-shape-notcomp", "project", "working_paper.json"),
              "w", encoding="utf-8") as fh:
        json.dump(_paper(notcomp, _sel(["t1-f-roe"])), fh, ensure_ascii=False)
    _add_task("t1-shape-notcomp", "t1u")

    _ws("t1-shape-nopaper")                            # 工作区在、无底稿
    _add_task("t1-shape-nopaper", "t1u")
    _add_task("t1-shape-nodir", "t1u")                 # 工作区目录不在
    _add_task("t1-shape-other", "t1other")             # 他人的任务


class T1ProjectionShape(unittest.TestCase):
    """T1.3 接口缺口 + T1.4 显式不可用码。"""

    def setUp(self):
        _wipe()
        _prepare_shape_db()

    def test_item_answers_which_fields_are_computable(self):
        """T1.3：条目要回答「能回到哪份原版」与「哪些当前字段能算」。"""
        item = rm.research_material(SCOPE, "t1-shape-ok")
        self.assertTrue(item["usable"])
        # 回到原版：origin + working_paper_version（哈希绑定文件）
        self.assertEqual(item["origin"]["kind"], "research_material")
        self.assertEqual(item["origin"]["parent_task_id"], "t1-shape-ok")
        self.assertTrue(item["working_paper"]["sha256"])
        self.assertEqual(item["working_paper_version"]["short"],
                         item["working_paper"]["sha256"][:12])
        self.assertEqual(item["working_paper_version"]["sha256"],
                         item["working_paper"]["sha256"])
        # 哪些当前字段能算：与筛选同一份 FIELD_SPECS 判据，不另写映射
        fields = item["fields"]
        self.assertTrue(fields, "字段支撑表不能为空")
        self.assertEqual(fields["revenue_growth"]["status"], "ok",
                         "2024+2023 两期营收都在：revenue_growth 能算")
        self.assertEqual(fields["gross_margin"]["status"], "ok")
        self.assertEqual(fields["cashflow_coverage"]["status"], "missing",
                         "没有经营现金流/净利润事实就显式 missing，不猜")
        self.assertEqual(fields["debt_ratio"]["status"], "missing")
        self.assertIn("revenue_growth", item["usable_fields"])
        self.assertIn("gross_margin", item["usable_fields"])
        self.assertNotIn("cashflow_coverage", item["usable_fields"])
        self.assertNotIn("debt_ratio", item["usable_fields"])
        self.assertTrue(fields["revenue_growth"]["periods"], "字段支撑要带可用期间")
        # 事实级 origin：贯穿到每一条事实
        rev = next(f for f in item["facts"] if f["fact_id"] == "t1-f-rev-2024")
        self.assertEqual(rev["origin"]["kind"], "research_material")
        self.assertEqual(rev["origin"]["parent_task_id"], "t1-shape-ok")

    def test_unavailable_codes_are_distinct(self):
        """T1.4：损坏 ≠ 没有材料 ≠ 不能算 ≠ 没权限——每种都给显式码。"""
        cases = {
            "t1-shape-nodir": "workspace_missing",
            "t1-shape-nopaper": "working_paper_missing",
            "t1-shape-corrupt": "working_paper_corrupt",
            "t1-shape-fincorrupt": "financials_corrupt",
            "t1-shape-notcomp": "not_computable",
            "t1-shape-pending": "no_admitted_facts",
        }
        for tid, code in cases.items():
            item = rm.research_material(SCOPE, tid)
            un = item["unavailable"] or {}
            self.assertEqual(un.get("code"), code,
                             f"{tid} 应显式报 {code}，实际 {un.get('code')}")
            self.assertTrue(un.get("reason"))
            self.assertTrue(un.get("hint"), f"{code} 要带用户能照着做的提示")
        other = rm.research_material(SCOPE, "t1-shape-other")
        self.assertEqual(other["unavailable"]["code"], "no_permission")

    def test_corrupt_is_not_reported_as_missing(self):
        """底稿存在但读不了：不是"没有材料"，损坏状态要可见。"""
        item = rm.research_material(SCOPE, "t1-shape-corrupt")
        self.assertEqual(item["unavailable"]["code"], "working_paper_corrupt")
        self.assertIn("损坏", item["unavailable"]["reason"])
        self.assertTrue(item["working_paper"]["present"], "文件在：present 不能说 False")
        self.assertFalse(item["working_paper"]["readable"])
        self.assertTrue(item["warnings"], "损坏要有警告，不能伪装成干净数据")
        fin = rm.research_material(SCOPE, "t1-shape-fincorrupt")
        self.assertEqual(fin["unavailable"]["code"], "financials_corrupt")
        self.assertTrue(fin["warnings"])

    def test_not_computable_keeps_facts_visible(self):
        """"不能算" ≠ "没有材料"：事实还在，只是当前字段算不出。"""
        item = rm.research_material(SCOPE, "t1-shape-notcomp")
        self.assertFalse(item["usable"])
        self.assertEqual(item["unavailable"]["code"], "not_computable")
        self.assertGreaterEqual(item["facts_admitted"], 1,
                                "准入事实还在：不能把不可算说成没有材料")
        self.assertEqual(item["facts"][0]["admission"], "admitted")
        self.assertTrue(item["unavailable"]["detail"])

    def test_projection_status_is_unavailable_without_usable(self):
        """列表语义：没有可物化材料时 status=unavailable（不是 ok），但原因显式。"""
        proj = rm.materials_from_research(SCOPE, task_ids=["t1-shape-nopaper"])
        self.assertEqual(proj["status"], "unavailable")
        self.assertEqual(proj["totals"]["facts_admitted"], 0,
                         "facts_admitted=0 不能被报成 ok/有准入")
        self.assertEqual(proj["unavailable"][0]["code"], "working_paper_missing")
        ok_proj = rm.materials_from_research(SCOPE, task_ids=["t1-shape-ok"])
        self.assertEqual(ok_proj["status"], "ok")


# ---------------- T1.5：选择绑定底稿哈希 ----------------


class T1PaperHashBinding(unittest.TestCase):
    """T1.5：expect_paper_sha256——底稿在"看"与"选"之间被更正时拒绝按旧选择物化。"""

    def setUp(self):
        _wipe()
        self.paper_path = _prepare_g5_paper()

    def test_changed_paper_rejects_old_selection(self):
        """用户看着哈希 A 选了事实，提交时底稿已是哈希 B → 拒绝，不按旧选择写库。"""
        item = rm.research_material(SCOPE, "t1-g5-task")
        seen_sha = item["working_paper"]["sha256"]
        self.assertTrue(seen_sha)
        # 模拟"选择与提交之间底稿被更正"：改写底稿内容（哈希随之变化）
        with open(self.paper_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["rows"][0]["value"] = 999.99
        with open(self.paper_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        data, errs = rm.collect_from_research(SCOPE, task_id="t1-g5-task",
                                              fact_ids=["t1-f-rev-2024"],
                                              expect_paper_sha256=seen_sha)
        self.assertIsNone(data, "底稿已变更：不能按旧选择物化")
        self.assertTrue(any("底稿已变更" in e for e in errs), f"要显式说底稿变更：{errs}")
        self.assertEqual(st.list_datasets(SCOPE), [], "拒绝时不能生成数据集")
        # 重新确认后（按新哈希）可以物化
        item2 = rm.research_material(SCOPE, "t1-g5-task")
        data2, errs2 = rm.collect_from_research(
            SCOPE, task_id="t1-g5-task", fact_ids=["t1-f-rev-2024"],
            expect_paper_sha256=item2["working_paper"]["sha256"])
        self.assertEqual(errs2, [])
        self.assertEqual(data2["dataset"]["provenance_kind"], "research_material")

    def test_correct_sha_binds_paper_in_provenance(self):
        """哈希一致 → 物化成功，provenance 带文件哈希（能回到原版）。"""
        sha = _sha_of(self.paper_path)
        data, errs = rm.collect_from_research(SCOPE, task_id="t1-g5-task",
                                              fact_ids=["t1-f-rev-2024"],
                                              expect_paper_sha256=sha)
        self.assertEqual(errs, [])
        _m2, payload2, load_err2 = st.load_dataset(SCOPE, data["dataset"]["dataset_id"])
        self.assertEqual(load_err2, "")
        prov2 = payload2["metadata"]["provenance"]
        self.assertEqual(prov2["paper_sha256"], sha)
        self.assertEqual(prov2["working_paper_ref"]["sha256"], sha)
        self.assertEqual(data["origin"]["paper_sha256"], sha)

    def test_missing_paper_cannot_bind(self):
        """底稿没了还想按旧哈希物化 → 显式拒绝（无法确认版本），不装作一致。"""
        os.remove(self.paper_path)
        data, errs = rm.collect_from_research(SCOPE, task_id="t1-g5-task",
                                              fact_ids=["t1-f-rev-2024"],
                                              expect_paper_sha256="a" * 64)
        self.assertIsNone(data)
        self.assertTrue(any("底稿" in e and ("没有" in e or "不在" in e) for e in errs), errs)


# ---------------- T1.6：真实导出链路（传输层 mock） ----------------

_RAW_2023 = {
    "REPORT_DATE": "2023-12-31 00:00:00", "REPORT_TYPE": "年报",
    "NOTICE_DATE": "2024-04-30 00:00:00", "SECUCODE": "600887.SH",
    "SECURITY_CODE": "600887", "SECURITY_NAME_ABBR": "伊利股份",
    "CURRENCY": "CNY", "TOTALOPERATEREVE": 126179000000.0, "PARENTNETPROFIT": 10429000000.0,
    "MLR": 39150000000.0, "XSMLL": 31.03, "OPERATE_PROFIT_PK": 17960000000.0,
    "TOTAL_ASSETS_PK": 151200000000.0, "LIABILITY": 79500000000.0,
    "NETCASH_OPERATE_PK": 16600000000.0, "RDEXPEND": 8100000000.0, "EPSJB": 1.63,
    "ROEJQ": 19.45,
}
_RAW_2024 = {
    "REPORT_DATE": "2024-12-31 00:00:00", "REPORT_TYPE": "年报",
    "NOTICE_DATE": "2025-04-29 00:00:00", "SECUCODE": "600887.SH",
    "SECURITY_CODE": "600887", "SECURITY_NAME_ABBR": "伊利股份",
    "CURRENCY": "CNY", "TOTALOPERATEREVE": 115780000000.0, "PARENTNETPROFIT": 8450000000.0,
    "MLR": 36800000000.0, "XSMLL": 31.79, "OPERATE_PROFIT_PK": 15200000000.0,
    "TOTAL_ASSETS_PK": 156000000000.0, "LIABILITY": 81000000000.0,
    "NETCASH_OPERATE_PK": 13300000000.0, "RDEXPEND": 7000000000.0, "EPSJB": 1.32,
    "ROEJQ": 15.20,
}


class T1RealExportChain(unittest.TestCase):
    """T1.6：真实导出链路——适配器（仅 mock 传输层 _get）→ financials.json
    （与 structured_pipeline 同写盘形状）→ write_working_paper → 投影 → 物化。"""

    def setUp(self):
        _wipe()
        ts.ensure_schema(str(ts.DB_PATH))
        _ws("t1-real")
        _add_task("t1-real", "t1u",
                  research_request={"goal": "研究伊利股份（600887.SH）2024 年报",
                                    "company": "伊利股份", "company_id": "600887.SH",
                                    "market": "cn", "periods": [2023, 2024],
                                    "caliber": "合并", "as_of": ""})

    def test_export_then_project_then_collect(self):
        from adapters import router
        raw = json.dumps({"success": True,
                          "result": {"pages": {"page": 1}, "count": 2,
                                     "data": [_RAW_2023, _RAW_2024]}},
                         ensure_ascii=False)
        request, cands, src = wpe.resolve_request("t1-real", "研究伊利股份", {}, None)
        self.assertEqual(src, "stored", "契约应来自落库的 research_request")
        self.assertEqual(request.company_id, "600887.SH")
        # 巨潮默认关闭；仍显式关掉，保证走东财且绝不触网
        with patch.object(cninfo, "enabled", return_value=False), \
                patch.object(eastmoney, "_get", return_value=raw):
            data = router.route_structured_for_request(request)
        self.assertTrue(data, "契约驱动抓取（mock 传输层）应命中")
        self.assertEqual(data.get("contract", {}).get("company_id"), "600887.SH")
        # 生产写盘形状：json.dumps(data, ensure_ascii=False, indent=1)（同 structured_pipeline）
        proj = ws.task_project_dir("t1-real", "default")
        fin_path = os.path.join(str(proj), "financials.json")
        with open(fin_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        # 真实导出器：读结构化财务 → 事实 → 底稿两件套
        res = wpe.write_working_paper("t1-real", "研究伊利股份（600887.SH）2024 年报",
                                      project="default")
        self.assertTrue(res.get("ok"), f"真实导出器应产出底稿：{res}")
        paper_path = os.path.join(str(proj), "working_paper.json")
        self.assertTrue(os.path.exists(paper_path), f"底稿要落盘：{res.get('files')}")
        sha = _sha_of(paper_path)
        # 投影读的是**真实导出器**的底稿
        item = rm.research_material(SCOPE, "t1-real")
        self.assertEqual(item["facts_source"], "working_paper.json")
        self.assertEqual(item["working_paper"]["sha256"], sha)
        self.assertGreaterEqual(item["facts_admitted"], 2, "两期营收都应被准入")
        rev = next(f for f in item["facts"]
                   if f["metric"] == "revenue" and f["admission"] == "admitted"
                   and f["period_end"] == "2024-12-31")
        self.assertEqual(rev["origin"]["kind"], "research_material")
        # 物化：provenance 绑定真实底稿文件
        data2, errs = rm.collect_from_research(SCOPE, task_id="t1-real",
                                               fact_ids=[rev["fact_id"]],
                                               expect_paper_sha256=sha)
        self.assertEqual(errs, [])
        rows = data2["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["period"], "2024-12-31")
        self.assertEqual(rows[0]["code"], "600887",
                         "事实层 entity_id 来自 financials.json 的 stock_code（本地裸码）；"
                         "带后缀的 SECUCODE 在 source_subject.code 里（留给 lead 的观察）")
        self.assertEqual(rows[0]["unit"], "元")
        self.assertAlmostEqual(rows[0]["revenue"], 1157.80 * 1e8,
                               msg="亿元 × 1e8 回到原始元金额")
        self.assertEqual(data2["origin"]["parent_task_id"], "t1-real")
        _m3, payload3, load_err3 = st.load_dataset(SCOPE, data2["dataset"]["dataset_id"])
        self.assertEqual(load_err3, "")
        self.assertEqual(payload3["metadata"]["provenance"]["working_paper_ref"]["sha256"],
                         sha, "物化出的数据集绑定真实底稿文件哈希")
        # 投影/物化全程只读：底稿文件字节未变
        self.assertEqual(_sha_of(paper_path), sha, "投影不写底稿")


if __name__ == "__main__":
    unittest.main(verbosity=2)
