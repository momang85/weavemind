# -*- coding: utf-8 -*-
"""BETA-CLOSE-1024 · C1：数据准备入口回归（store/prepare 层，不起服务）。

覆盖：
1. materials_projection 只见本作用域：B 作用域的数据集对 A 不可见；
2. prepare_plan：covered / missing 逐主体×逐期间，缺的不冒充已覆盖；
3. official_cache 物化：dataset.official=True（服务端判定）、行带 source_url、
   reused 语义、计数 network_* 全 0（本地来源不联网）；
4. collect 拒绝未知来源；adapter 通道策略拒绝一次即停（probe/批量都不逐个重试）；
5. 被拒（denied）与未尝试（skipped）分得清；成本账本 denied/attempts 分开记。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.abspath(__file__))   # 仓库根 = 本测试文件所在目录
sys.path.insert(0, REPO)
# RC2-B：测试目录由本脚本创建（mkdtemp），_wipe() 只清理该目录；
# 不再 setdefault 到 REPO/tmp 后递归删除（继承任意 SCREEN_DIR 会误删真实数据）。
os.environ.setdefault("WEAVEMIND_TEST_MODE", "1")
TEST_ROOT = os.environ.setdefault(
    "WEAVEMIND_SCREEN_DIR", os.path.join(tempfile.mkdtemp(prefix="weavemind-c1-prepare-"),
                                         "screen"))

import screen_prepare as sp  # noqa: E402
import screen_store as st  # noqa: E402

SCOPE_A = {"user": "c1a", "project": "personal"}
SCOPE_B = {"user": "c1b", "project": "personal"}

_WIDE_COLUMNS = ["entity", "code", "period", "unit", "currency", "caliber",
                 "period_type", "available_at", "disclosed_at", "revenue",
                 "gross_profit", "total_assets", "total_liabilities",
                 "operating_cashflow", "net_profit"]
_WIDE_HEAD = ",".join(_WIDE_COLUMNS) + "\n"


def _csv_line(entity, code, period, revenue, *, avail="2025-04-20", **kw):
    """一行列齐全的宽表 CSV（T0-G2：unit/currency/caliber 缺失在执行侧是 unknown）。"""
    row = {"entity": entity, "code": code, "period": period, "unit": "元",
           "currency": "CNY", "caliber": "合并", "period_type": "年报",
           "available_at": avail, "disclosed_at": avail, "revenue": revenue}
    row.update(kw)
    return ",".join(str(row.get(c, "")) for c in _WIDE_COLUMNS) + "\n"


def _wipe():
    base = os.path.abspath(st.screen_base())
    if not base.startswith(os.path.abspath(TEST_ROOT)):
        raise RuntimeError(f"拒绝清理非本测试创建的目录：{base}")
    if os.path.isdir(base):
        shutil.rmtree(base, ignore_errors=True)


class MaterialsAndPlan(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_materials_scoped_and_gaps_marked(self):
        """跨账号隔离：B 传的数据集 A 看不到；A 清单里没材料的公司进缺口。"""
        csv_a = ("entity,code,period,revenue\n"
                 "甲公司,600519.SH,2024-12-31,10000000000\n")
        meta, reused, errs = st.save_dataset(scope=SCOPE_A, name="我的材料",
                                             filename="a.csv", text=csv_a)
        self.assertEqual(errs, [])
        csv_b = ("entity,code,period,revenue\n"
                 "乙公司,002304.SZ,2024-12-31,20000000000\n")
        _meta_b, _r, errs_b = st.save_dataset(scope=SCOPE_B, name="乙的材料",
                                              filename="b.csv", text=csv_b)
        self.assertEqual(errs_b, [])
        st.watchlist_mutate(scope=SCOPE_A, action="add", code="600519.SH", name="甲公司")
        st.watchlist_mutate(scope=SCOPE_A, action="add", code="002304.SZ", name="乙公司")

        proj = sp.materials_projection(SCOPE_A)
        ds_ids = [d["dataset_id"] for d in proj["datasets"]]
        self.assertIn(meta["dataset_id"], ds_ids)
        self.assertNotIn(_meta_b["dataset_id"], ds_ids, "跨作用域数据集不可见")
        self.assertEqual(proj["watchlist"]["missing_material"], ["002304.SZ"],
                         "没有材料的清单项必须显式列为缺口")

    def test_plan_covers_and_marks_missing(self):
        """预览：覆盖按**绑定输入**的实际 (主体,期间) 行计算，可计算性与执行同判据。

        T0-G3：预览必须绑定到执行会用到的那一份输入（`dataset_id`），不再把本作用域
        所有数据集 + 官方缓存拼成一张表（那会借别的数据集的年度算同比）。
        T0-G2：字段"非空"不等于可算——unit/currency/caliber 缺失在执行侧是 unknown，
        预览侧同样判不可算（所以下面两行都补齐元数据）。
        """
        csv_a = ("entity,code,period,unit,currency,caliber,period_type,revenue,gross_profit,"
                 "total_assets,total_liabilities,operating_cashflow,net_profit\n"
                 "甲公司,600519.SH,2023-12-31,元,CNY,合并,年报,10000000000,3000000000,"
                 "15000000000,7000000000,2000000000,8000000000\n"
                 "甲公司,600519.SH,2024-12-31,元,CNY,合并,年报,12000000000,3500000000,"
                 "16000000000,7200000000,2500000000,9000000000\n")
        meta, _reused, errs = st.save_dataset(scope=SCOPE_A, name="我的材料",
                                             filename="a.csv", text=csv_a)
        self.assertEqual(errs, [])
        plan = sp.prepare_plan(SCOPE_A, ["600519.SH", "000001.SZ"], ["2024"],
                              dataset_id=meta["dataset_id"])
        self.assertEqual(plan["errors"], [])
        self.assertEqual(plan["input_binding"]["dataset_id"], meta["dataset_id"],
                         "预览绑定到所选数据集（不跨数据集拼行）")
        by_code = {i["code"]: i for i in plan["items"]}
        rows_519 = {p["period"]: p for p in by_code["600519.SH"]["periods"]}
        self.assertEqual(rows_519["2024-12-31"]["status"], "covered",
                         "两个相邻年度的完整行：五字段全部可算（含同比）→ covered")
        self.assertEqual(rows_519["2024-12-31"]["fields"]["revenue_growth"]["previous_period"],
                         "2023-12-31")
        rows_1 = {p["period"]: p for p in by_code["000001.SZ"]["periods"]}
        self.assertEqual(rows_1["2024-12-31"]["status"], "missing")
        self.assertEqual(plan["totals"]["covered"], 1)
        self.assertEqual(plan["totals"]["missing"], 1)
        self.assertIn("不等于取数", plan["reading"])

    def test_plan_does_not_splice_other_datasets(self):
        """T0-G3 反例：2023 在数据集 A、2024 在数据集 B → 绑定 B 时同比**不可算**。

        旧实现在这里给出 status ok + previous_period 2023-12-31，而执行（只选 B）
        给出资料不足——预览与执行两套判据正是复核 G3。
        """
        cols = dict(gross_profit=3_000_000_000, total_assets=15_000_000_000,
                    total_liabilities=7_000_000_000, operating_cashflow=2_000_000_000,
                    net_profit=8_000_000_000)
        csv_2023 = _WIDE_HEAD + _csv_line("甲公司", "600519.SH", "2023-12-31",
                                          10_000_000_000, **cols)
        csv_2024 = _WIDE_HEAD + _csv_line("甲公司", "600519.SH", "2024-12-31",
                                          12_000_000_000, **cols)
        m23, _r, e23 = st.save_dataset(scope=SCOPE_A, name="只有2023", filename="y23.csv",
                                       text=csv_2023)
        m24, _r, e24 = st.save_dataset(scope=SCOPE_A, name="只有2024", filename="y24.csv",
                                       text=csv_2024)
        self.assertEqual(e23 + e24, [])

        plan = sp.prepare_plan(SCOPE_A, ["600519.SH"], ["2024"],
                              dataset_id=m24["dataset_id"])
        cell = plan["items"][0]["periods"][0]
        growth = cell["fields"]["revenue_growth"]
        self.assertEqual(growth["status"], "missing")
        self.assertIn("缺上一年度", growth["reason"])
        self.assertNotIn("previous_period", growth)
        self.assertNotEqual(cell["status"], "covered")
        # 与执行同一结论：只选 2024 数据集跑同比 → 资料不足
        run, errs_run = st.execute(scope=SCOPE_A, pool=["600519.SH"], as_of="2026-10-24",
                                   rules_raw=[{"field": "revenue_growth", "op": ">",
                                               "value": 0}],
                                   period_policy_raw={"mode": "pinned", "report_period": "2024"},
                                   dataset_id=m24["dataset_id"], store=False)
        self.assertEqual(errs_run, [])
        self.assertEqual(run["counts"]["insufficient"], 1)
        self.assertEqual(run["counts"]["pass"], 0)
        # 把两年都放进同一份数据集 → 预览与执行同时转为可算/通过
        both, _r, eb = st.save_dataset(scope=SCOPE_A, name="两年", filename="both.csv",
                                       text=csv_2023 + csv_2024.split("\n", 1)[1])
        self.assertEqual(eb, [])
        plan2 = sp.prepare_plan(SCOPE_A, ["600519.SH"], ["2024"],
                               dataset_id=both["dataset_id"])
        self.assertEqual(plan2["items"][0]["periods"][0]["fields"]["revenue_growth"]["status"],
                         "ok")
        run2, errs2 = st.execute(scope=SCOPE_A, pool=["600519.SH"], as_of="2026-10-24",
                                 rules_raw=[{"field": "revenue_growth", "op": ">", "value": 0}],
                                 period_policy_raw={"mode": "pinned",
                                                    "report_period": "2024"},
                                 dataset_id=both["dataset_id"], store=False)
        self.assertEqual(errs2, [])
        self.assertEqual(run2["counts"]["pass"], 1)

    def test_unbound_plan_binds_official_cache(self):
        """未指定 dataset_id → 预览绑定内置官方缓存（与不传 dataset_id 的执行同一输入）。"""
        csv_a = ("entity,code,period,unit,currency,caliber,period_type,revenue\n"
                 "甲公司,600519.SH,2024-12-31,元,CNY,合并,年报,12000000000\n")
        st.save_dataset(scope=SCOPE_A, name="我的材料", filename="a.csv", text=csv_a)
        # 官方缓存指向测试自写的合成缓存（只有另一家公司）→ 600519 不在其中
        cache_rows = [{"entity": "乙公司", "code": "000009.SH", "period": "2024-12-31",
                       "unit": "元", "currency": "CNY", "caliber": "合并",
                       "period_type": "年报", "revenue": 1.0}]
        old = os.environ.get("WEAVEMIND_SCREEN_CACHE")
        cache_path = os.path.join(tempfile.mkdtemp(prefix="weavemind-c1-cache-"),
                                  "official_disclosure_cache.json")
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump({"metadata": {"synthetic": True}, "rows": cache_rows}, fh)
        os.environ["WEAVEMIND_SCREEN_CACHE"] = cache_path
        try:
            plan = sp.prepare_plan(SCOPE_A, ["600519.SH"], ["2024"])
        finally:
            if old is None:
                os.environ.pop("WEAVEMIND_SCREEN_CACHE", None)
            else:
                os.environ["WEAVEMIND_SCREEN_CACHE"] = old
        self.assertEqual(plan["errors"], [])
        self.assertEqual(plan["input_binding"]["dataset_id"], "")
        self.assertEqual(plan["input_binding"]["source"], "official_cache")
        cell = plan["items"][0]["periods"][0]
        # 缓存里没有这家 → 缺失；**不**因为"本作用域有个数据集含它"就宣称覆盖（G3）
        self.assertEqual(cell["status"], "missing")
        self.assertFalse(cell["material"])

    def test_zero_assets_debt_ratio_not_computable_in_preview(self):
        """T0-G2 验收反例：资产总计=0 → 资产负债率不可算，预览与执行一致。"""
        cols = dict(avail="2025-04-20", gross_profit=3_500_000_000,
                    total_liabilities=7_200_000_000, operating_cashflow=2_500_000_000,
                    net_profit=9_000_000_000, total_assets=0)
        csv_a = _WIDE_HEAD + _csv_line("甲公司", "600519.SH", "2024-12-31",
                                       12_000_000_000, **cols)
        meta, _r, errs = st.save_dataset(scope=SCOPE_A, name="资产为零", filename="z.csv",
                                         text=csv_a)
        self.assertEqual(errs, [])
        plan = sp.prepare_plan(SCOPE_A, ["600519.SH"], ["2024"],
                              dataset_id=meta["dataset_id"])
        cell = plan["items"][0]["periods"][0]
        debt = cell["fields"]["debt_ratio"]
        self.assertNotEqual(debt["status"], "ok")
        self.assertIn("分母为 0", debt["reason"])
        self.assertNotEqual(cell["status"], "covered")
        # 执行同判据：debt_ratio 规则 → 资料不足（不是通过、不是 fail）
        run, errs_run = st.execute(scope=SCOPE_A, pool=["600519.SH"], as_of="2026-10-24",
                                   rules_raw=[{"field": "debt_ratio", "op": "<=", "value": 60}],
                                   period_policy_raw={"mode": "pinned",
                                                      "report_period": "2024"},
                                   dataset_id=meta["dataset_id"], store=False)
        self.assertEqual(errs_run, [])
        self.assertEqual(run["counts"]["insufficient"], 1)


class CollectLocal(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_official_cache_materialization(self):
        """official_cache 物化：数据集 official=True（服务端判定）、行带原文链接、零网络。"""
        summary = sp.official_cache_summary()
        if not summary.get("available"):
            self.skipTest("本机没有官方缓存样本")
        code = (summary.get("codes") or ["002304.SZ"])[0]
        data, errs = sp.collect(scope=SCOPE_A, source_id="official_cache",
                                codes=[code], periods=[])
        self.assertEqual(errs, [])
        self.assertEqual(data["status"], "ok")
        ds = data["dataset"]
        self.assertTrue(ds["official"], "官方身份由服务端判定并物化进 meta")
        self.assertFalse(ds["user_provided"])
        self.assertTrue(ds["source_urls"], "行带原文链接")
        counters = data["counters"]
        self.assertEqual((counters["network_attempts"], counters["network_requests"],
                          counters["network_denied"]), (0, 0, 0),
                         "本地来源没有发起任何网络请求")
        # 物化后 material 投影里能看到它，且多年度完整行的最新期间可全算（covered）
        proj = sp.materials_projection(SCOPE_A)
        self.assertIn(ds["dataset_id"], [d["dataset_id"] for d in proj["datasets"]])
        plan = sp.prepare_plan(SCOPE_A, [code])
        periods = plan["items"][0]["periods"]
        self.assertTrue(all(p["material"] for p in periods),
                        "物化后的每个期间都有材料")
        self.assertTrue(any(p["status"] == "covered" for p in periods),
                        f"多年度完整行至少最新期间可全算：{[p['status'] for p in periods]}")
        # 再取一次同内容 → 复用同一 dataset_id（不重复堆积）
        data2, errs2 = sp.collect(scope=SCOPE_A, source_id="official_cache",
                                  codes=[code], periods=[])
        self.assertEqual(errs2, [])
        self.assertTrue(data2["reused"])
        self.assertEqual(data2["dataset"]["dataset_id"], ds["dataset_id"])

    def test_official_cache_missing_company_reported(self):
        """缓存里没有的公司：不冒充已取数——要逐公司说明 missing（或明确拒绝空生成）。"""
        data, errs = sp.collect(scope=SCOPE_A, source_id="official_cache",
                                codes=["000001.SZ"], periods=[])
        if errs:
            self.assertTrue(all("没有" in e or "缺口" in e for e in errs),
                            f"拒绝空生成时要说清没有材料：{errs}")
            return
        statuses = {c["code"]: c["status"] for c in data["companies"]}
        self.assertEqual(statuses.get("000001.SZ"), "missing")


class CollectNetwork(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_unknown_source_rejected(self):
        data, errs = sp.collect(scope=SCOPE_A, source_id="adapter:nonexistent",
                                codes=["600519.SH"])
        self.assertIsNone(data)
        self.assertTrue(errs and "未知来源" in errs[0])

    def test_real_probe_single_company_honest(self):
        """真实网络可用时：只探一家，结果如实（成功带行/失败带类别），不伪造。"""
        data, errs = sp.collect(scope=SCOPE_A, source_id="adapter:eastmoney",
                                codes=["600519.SH"], periods=["2024"], probe_only=True)
        self.assertEqual(errs, [])
        first = data["companies"][0]
        self.assertIn(first["status"], ("collected", "failed", "denied_by_policy"))
        if first["status"] == "collected":
            self.assertGreaterEqual(first.get("rows", 0), 1)
            self.assertIsNotNone(data["dataset"], "取到行必须物化成数据集，含来源与原文链接")
            self.assertFalse(data["dataset"]["official"], "适配器取数不冒充官方数据")
        else:
            self.assertTrue(first.get("category") or first.get("detail"))

    def _deny_policy(self):
        """把出站校验换成固定拒绝（模拟 2026-10-23 fake-IP 环境），不碰真实网络。"""
        import net_policy
        real = net_policy.validate_public_url

        def deny(url):
            return net_policy.Decision(False, "解析到非公网地址：198.18.0.10（固定拒绝）",
                                       redacted="", category="reserved_address")
        net_policy.validate_public_url = deny
        self.addCleanup(setattr, net_policy, "validate_public_url", real)

    def test_policy_denied_stops_batch_no_retry_storm(self):
        """固定策略拒绝：一次拒绝 → 本批其余公司标未尝试，不再重复 35 次相同失败。"""
        self._deny_policy()
        codes = ["600519.SH", "002304.SZ", "600031.SH", "601318.SH"]
        data, errs = sp.collect(scope=SCOPE_A, source_id="adapter:eastmoney",
                                codes=codes, periods=["2024"])
        self.assertEqual(errs, [])
        self.assertEqual(data["status"], "blocked")
        counters = data["counters"]
        self.assertLessEqual(counters["network_attempts"], 2,
                             f"同源被拒后必须停批：attempts={counters}")
        self.assertGreaterEqual(counters["network_denied"], 1)
        self.assertEqual(counters["network_requests"], None,
                         "传输层不暴露连接事件：真实请求数未知（RC2-F6），"
                         "不能拿适配器调用数冒充真实请求数")
        self.assertGreaterEqual(counters["adapter_calls"], 1)
        self.assertGreaterEqual(counters["pre_connect_failures"], 1)
        statuses = [c["status"] for c in data["companies"]]
        self.assertIn("denied_by_policy", statuses)
        self.assertIn("skipped_policy_blocked", statuses)
        self.assertNotIn("collected", statuses)
        self.assertIsNone(data["dataset"], "一行都没取到时不生成空数据集")
        # 成本账本：denied 单独成账，不与真实请求混记
        cost, _ = st.load_cost(SCOPE_A)
        self.assertGreaterEqual(cost["network_denied"], 1)
        self.assertEqual(cost["network_ok"], 0)
        self.assertEqual(cost["external_fee"], "unknown")

    def test_probe_only_tries_first_company_only(self):
        """probe_only：只试第一家，其余标 not_attempted（给用户先看一次再决定）。"""
        self._deny_policy()
        data, errs = sp.collect(scope=SCOPE_A, source_id="adapter:eastmoney",
                                codes=["600519.SH", "002304.SZ"], periods=["2024"],
                                probe_only=True)
        self.assertEqual(errs, [])
        statuses = {c["code"]: c["status"] for c in data["companies"]}
        self.assertNotEqual(statuses.get("002304.SZ"), "collected")
        self.assertIn(statuses.get("002304.SZ"),
                      ("not_attempted", "skipped_policy_blocked", "failed"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
