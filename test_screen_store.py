# -*- coding: utf-8 -*-
"""R1-SCREEN-1023 R1.1（R1-B）：筛选结果持久化、身份哈希、作用域隔离与证据绑定测试。

覆盖（架构师复核 `docs/evidence/20261023-screen-next/architect_review.json` 的三条探针
+ R1.1 任务单的执行身份/损坏防护/作用域要求）：

- 执行身份：`run_id` 含快照/规则/期间政策/**作用域**；`sort` 是展示态，不进身份。
- 复用：同一身份重复执行复用同一结果（不覆盖、不新增）；历史**损坏**时 409 拒绝，
  绝不自动重写；恢复走显式隔离（quarantine），隔离后重跑新建目录。
- 完整 ID：32 位完整 id 只做整名精确匹配（探针 ③ 回归：`…2222` 不得命中 `…1111`）；
  短 id 唯一前缀才接受，歧义明确报错。
- 作用域：结果/预设按服务端推导的 scope 分仓；跨用户列举/读取/删除一律拒绝；
  手工搬文件跨作用域（scope 不匹配）也不可见。
- legacy：R1 原型实例级目录只读保留，`legacy/owner=None/usable=False`，不迁移、不公开。
- 证据：绑定 (run_id, entity_id)；损坏结果的证据标 `usable=False`。
- 隔离：全程 WEAVEMIND_SCREEN_DIR/WEAVEMIND_SCREEN_CACHE 指向临时目录，
  不碰生产持久根与真实缓存文件。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import screen_store as st
import stock_screen as ss


def _row(entity, code, period, *, avail, url="http://example.com/report.pdf",
         unit="元", currency="CNY", caliber="合并", **metrics):
    base = {"entity": entity, "code": code, "period": period, "unit": unit,
            "currency": currency, "caliber": caliber, "period_type": "年度",
            "source_url": url, "source": "synthetic_test"}
    if avail is not None:
        base["available_at"] = avail
        base["disclosed_at"] = avail
    base.update(metrics)
    return base


# 合成边界样例（手算：收入 200→220 = +10%；毛利 50/200 = 25%；
# 负债 80/资产 200 = 40%；现金流 30 / 归母 25 = 120%）
_A = _row("甲公司", "000001.SZ", "2023-12-31", avail="2024-04-20",
          revenue=200_000_000, gross_profit=50_000_000, net_profit=25_000_000,
          operating_cashflow=30_000_000, total_assets=200_000_000,
          total_liabilities=80_000_000)
_B = _row("甲公司", "000001.SZ", "2024-12-31", avail="2025-04-18",
          revenue=220_000_000, gross_profit=55_000_000, net_profit=30_000_000,
          operating_cashflow=36_000_000, total_assets=210_000_000,
          total_liabilities=84_000_000)
_FUTURE = _row("甲公司", "000001.SZ", "2025-12-31", avail="2026-04-30",
               revenue=500_000_000)
_NOPIT = {**_row("甲公司", "000001.SZ", "2025-12-31", avail=None,
                 revenue=400_000_000)}
_A = {**_A}
_B = {**_B}   # 复制避免测试间共享可变对象


def _write_cache(path: str) -> str:
    payload = {"metadata": {"source": "synthetic_test", "synthetic": True,
                            "user_provided": False},
               "rows": [_A, _B, _FUTURE, _NOPIT]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path


def _write_cache_multi(path: str) -> str:
    """两家公司：甲多一个 2025 年度（2026-04 披露），乙只到 2024 →
    池内最新可用年度（2025年）≠ 最大共同年度（2024年）。"""
    yi = [_row("乙公司", "000002.SZ", f"{y}-12-31", avail=f"{int(y) + 1}-04-10",
               revenue=300_000_000 + i * 10_000_000, gross_profit=60_000_000,
               net_profit=20_000_000, operating_cashflow=24_000_000,
               total_assets=400_000_000, total_liabilities=160_000_000)
          for i, y in enumerate(("2023", "2024"))]
    jia_2025 = _row("甲公司", "000001.SZ", "2025-12-31", avail="2026-04-30",
                    revenue=500_000_000, gross_profit=150_000_000,
                    net_profit=60_000_000, operating_cashflow=90_000_000,
                    total_assets=600_000_000, total_liabilities=240_000_000)
    payload = {"metadata": {"source": "synthetic_test", "synthetic": True,
                            "user_provided": False},
               "rows": [_A, _B, yi[0], yi[1], jia_2025]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path


RULES = [{"field": "gross_margin", "op": ">=", "value": 20},
         {"field": "cashflow_coverage", "op": ">=", "value": 100}]
SCOPE = {"user": "alice", "project": "personal"}
SCOPE_B = {"user": "bob", "project": "personal"}


def errs_ok(run, errors):
    assert errors == [], errors
    return run


class StoreCase(unittest.TestCase):
    """公共夹具：临时基根 + 临时缓存，restore 还原环境变量。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="weavemind_screen_test_")
        self._saved = {k: os.environ.get(k) for k in
                       ("WEAVEMIND_SCREEN_DIR", "WEAVEMIND_SCREEN_CACHE",
                        "WEAVEMIND_SCREEN_LEGACY_RUNS", "WEAVEMIND_SCREEN_LEGACY_PRESETS")}
        os.environ["WEAVEMIND_SCREEN_DIR"] = os.path.join(self._tmp, "store")
        os.environ["WEAVEMIND_SCREEN_CACHE"] = _write_cache(
            os.path.join(self._tmp, "cache.json"))
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, **kw):
        args = dict(scope=SCOPE, pool=["000001.SZ"], as_of="2026-01-31",
                    rules_raw=RULES)
        args.update(kw)
        return st.execute(**args)

    def _run_path(self, rid: str) -> str:
        return os.path.join(st.runs_root(SCOPE), rid)


class TestIdentityAndReuse(StoreCase):
    """执行身份与复用：同身份复用；变更即新结果；排序不进身份。"""

    def test_execute_persists_and_reuses_same_identity(self):
        run1, errs1 = self._run()
        self.assertEqual(errs1, [])
        run2, errs2 = self._run()
        self.assertEqual(errs2, [])
        self.assertEqual(run1["run_id"], run2["run_id"])
        self.assertFalse(run1.get("reused"))
        self.assertTrue(run2.get("reused"))
        with open(os.path.join(self._run_path(run1["run_id"]), "run.json"),
                  encoding="utf-8") as fh:
            again = json.loads(fh.read())
        self.assertEqual(again["result_hash"], run1["result_hash"])
        self.assertEqual(again["created_at"], run1["created_at"])

    def test_sort_is_view_only_same_run_id_reused(self):
        # 架构师探针 ① 回归（store 层）：同一身份、不同 sort → 同 run_id + 复用，
        # 行内容一致，只有展示顺序不同；run.json 里存的是规范序（按代码）。
        run1, _ = self._run()
        run2, errs = self._run(sort_raw={"field": "gross_margin", "direction": "desc"})
        self.assertEqual(errs, [])
        self.assertEqual(run1["run_id"], run2["run_id"])
        self.assertTrue(run2.get("reused"))
        self.assertEqual([r["entity_id"] for r in run2["rows"]["pass"]],
                         [r["entity_id"] for r in run1["rows"]["pass"]])
        self.assertEqual(run2["view"]["sort"]["direction"], "desc")
        self.assertIn("不改变结果身份", run2["view"]["note"])
        with open(os.path.join(self._run_path(run1["run_id"]), "run.json"),
                  encoding="utf-8") as fh:
            stored = json.loads(fh.read())
        self.assertNotIn("view", stored)
        self.assertNotIn("sort", stored)

    def test_invalid_sort_is_explicit_error_not_500(self):
        run, errors = self._run(sort_raw={"field": "net_margin", "direction": "desc"})
        self.assertIsNone(run)
        self.assertTrue(any("排序字段" in e for e in errors), errors)
        run, errors = self._run(sort_raw={"field": "gross_margin", "direction": "up"})
        self.assertIsNone(run)
        self.assertTrue(any("排序方向" in e for e in errors), errors)

    def test_rule_change_makes_new_run_and_keeps_old(self):
        run1, _ = self._run()
        with open(os.path.join(self._run_path(run1["run_id"]), "run.json"), "rb") as fh:
            before = fh.read()
        run2, errs = self._run(rules_raw=[{"field": "gross_margin",
                                           "op": ">=", "value": 99}])
        self.assertEqual(errs, [])
        self.assertNotEqual(run1["run_id"], run2["run_id"])
        self.assertFalse(run2.get("reused"))
        with open(os.path.join(self._run_path(run1["run_id"]), "run.json"), "rb") as fh:
            after = fh.read()
        self.assertEqual(before, after, "旧结果必须原样保留")

    def test_asof_change_makes_new_run(self):
        run1, _ = self._run()
        run2, _ = self._run(as_of="2025-01-01")
        self.assertNotEqual(run1["run_id"], run2["run_id"])

    def test_period_policy_change_makes_new_run(self):
        run1, _ = self._run()
        run2, errs = self._run(period_policy_raw={"mode": "pinned",
                                                  "report_period": "2023"})
        self.assertEqual(errs, [])
        self.assertNotEqual(run1["run_id"], run2["run_id"])
        self.assertEqual(run2["report_period"], "2023年")
        run3, errs = self._run(period_policy_raw={"mode": "latest_each"})
        self.assertEqual(errs, [])
        self.assertFalse(run3["comparable"])

    def test_bad_period_policy_is_explicit_error(self):
        run, errors = self._run(period_policy_raw={"mode": "pinned"})
        self.assertIsNone(run)
        self.assertTrue(any("report_period" in e for e in errors), errors)

    def test_run_identity_is_deterministic_scope_and_policy_bound(self):
        a = st.run_identity("snap-1", "rules-1", {"mode": "uniform",
                                                  "report_period": ""}, "u-a")
        self.assertEqual(a, st.run_identity("snap-1", "rules-1",
                                            {"mode": "uniform", "report_period": ""},
                                            "u-a"))
        self.assertNotEqual(a, st.run_identity("snap-2", "rules-1",
                                               {"mode": "uniform",
                                                "report_period": ""}, "u-a"))
        # 期间政策进身份
        self.assertNotEqual(a, st.run_identity("snap-1", "rules-1",
                                               {"mode": "pinned",
                                                "report_period": "2024年"}, "u-a"))
        # 作用域进身份（防止整目录搬移冒充本人历史）
        self.assertNotEqual(a, st.run_identity("snap-1", "rules-1",
                                               {"mode": "uniform",
                                                "report_period": ""}, "u-b"))
        self.assertRegex(a, r"^[0-9a-f]{32}$")

    def test_scope_is_required(self):
        with self.assertRaises(ValueError):
            st.normalize_scope(None)
        with self.assertRaises(ValueError):
            st.normalize_scope({"project": "personal"})


class TestResolutionAndIntegrity(StoreCase):
    """完整 ID 精确匹配 + 完整性分层 + 损坏不得复用/覆盖。"""

    def test_load_run_roundtrip_and_integrity(self):
        run, _ = self._run()
        loaded, manifest, err = st.load_run(SCOPE, run["run_id"])
        self.assertIsNone(err)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["integrity"], "ok")
        self.assertTrue(loaded["usable"])
        self.assertEqual(loaded["result_hash"], run["result_hash"])
        self.assertEqual(manifest["run_id"], run["run_id"])
        self.assertTrue(manifest["files"])
        self.assertTrue(all(f["sha256"] for f in manifest["files"]))

    def test_tampered_run_is_flagged_not_silent(self):
        run, _ = self._run()
        p = os.path.join(self._run_path(run["run_id"]), "run.json")
        with open(p, encoding="utf-8") as fh:
            data = json.loads(fh.read())
        data["notes"] = ["篡改过的注记"]
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        loaded, _manifest, err = st.load_run(SCOPE, run["run_id"])
        self.assertIsNone(err)
        self.assertEqual(loaded["integrity"], "hash_mismatch")
        self.assertFalse(loaded["usable"])
        self.assertIn("篡改过的注记", loaded["notes"])   # 内容照给，但明确标注
        # 证据同样标注不可用（不得当作原版采纳）
        ev = st.evidence(loaded, "000001.SZ")
        self.assertFalse(ev["usable"])

    def test_identity_mismatch_between_manifest_and_run(self):
        run, _ = self._run()
        rid = run["run_id"]
        d = self._run_path(rid)
        with open(os.path.join(d, "manifest.json"), encoding="utf-8") as fh:
            man = json.loads(fh.read())
        man["result_hash"] = "0" * 64
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(man, fh, ensure_ascii=False)
        loaded, _man, err = st.load_run(SCOPE, rid)
        self.assertIsNone(err)
        self.assertEqual(loaded["integrity"], "identity_mismatch")
        self.assertFalse(loaded["usable"])

    def test_missing_manifest_flagged(self):
        run, _ = self._run()
        os.remove(os.path.join(self._run_path(run["run_id"]), "manifest.json"))
        loaded, manifest, err = st.load_run(SCOPE, run["run_id"])
        self.assertIsNone(err)
        self.assertEqual(loaded["integrity"], "manifest_missing")
        self.assertEqual(manifest, {})

    def test_bad_run_ids_rejected(self):
        for bad in ("../../etc/passwd", "ZZZZ", "a/b", "", None, "a" * 65, "."):
            rid, err = st.resolve_run_id(SCOPE, bad)
            self.assertIsNone(rid, bad)
            self.assertEqual(err["code"], "invalid_id", bad)
            _run, _man, err = st.load_run(SCOPE, bad)
            self.assertEqual(err["code"], "invalid_id", bad)

    def test_full_id_exact_match_no_prefix_resolution(self):
        # 架构师探针 ③ 回归：32 位完整 id 不存在 → not_found；绝不截断成前缀再匹配
        run_a, _ = self._run(rules_raw=[{"field": "gross_margin",
                                         "op": ">=", "value": 20}])
        rid_a = run_a["run_id"]
        phantom = rid_a[:16] + "1" * 16            # 与真 run 共享 16 位前缀的“完整 id”
        self.assertNotEqual(phantom, rid_a)
        self.assertTrue(phantom.startswith(rid_a[:16]))   # 前缀确实会歧义
        _run, _man, err = st.load_run(SCOPE, phantom)
        self.assertEqual(err["code"], "not_found", err)
        # 唯一短前缀可用；多匹配短前缀 → 歧义（不猜）
        rid, err = st.resolve_run_id(SCOPE, rid_a[:8])
        self.assertEqual((rid, err), (rid_a, None))
        run_b, _ = self._run(rules_raw=[{"field": "gross_margin",
                                         "op": ">=", "value": 30}])
        shared_prefix = os.path.commonprefix([rid_a, run_b["run_id"]])
        if len(shared_prefix) >= 8:
            _rid, err = st.resolve_run_id(SCOPE, shared_prefix)
            self.assertEqual(err["code"], "ambiguous", err)

    def test_corrupt_result_not_reused_and_not_overwritten(self):
        # 架构师探针 ② 回归：同身份历史结果损坏 → 执行 409（FrozenCorruptError），
        # 原文件一个字节都不动；恢复走显式隔离。
        run1, _ = self._run()
        rid = run1["run_id"]
        p = os.path.join(self._run_path(rid), "run.json")
        with open(p, "r+b") as fh:                     # 原地破坏字节（哈希不再匹配）
            fh.seek(0)
            fh.write(b"CORRUPTED")
        with open(p, "rb") as fh:
            damaged = fh.read()
        with self.assertRaises(st.FrozenCorruptError) as ctx:
            self._run()
        self.assertEqual(ctx.exception.run_id, rid)
        self.assertNotEqual(ctx.exception.integrity, "ok")
        with open(p, "rb") as fh:
            after = fh.read()
        self.assertEqual(damaged, after, "损坏文件必须原样保留，不得自动重写")

    def test_quarantine_moves_only_damaged_dirs(self):
        run_ok, _ = self._run()
        rid_ok = run_ok["run_id"]
        detail, err = st.quarantine_run(SCOPE, rid_ok)
        self.assertIsNone(detail)
        self.assertEqual(err["code"], "not_damaged", err)
        # 需要显式 confirm（HTTP 层已挡，store 层语义：只接受坏目录）
        run1, _ = self._run(rules_raw=[{"field": "gross_margin",
                                        "op": ">=", "value": 20}])
        rid = run1["run_id"]
        p = os.path.join(self._run_path(rid), "run.json")
        with open(p, "r+b") as fh:
            fh.write(b"CORRUPTED")
        detail, err = st.quarantine_run(SCOPE, rid)
        self.assertIsNone(err)
        self.assertIn("quarantine", detail["moved_to"])
        qpath = os.path.join(st.screen_base(), "quarantine", st.scope_key(SCOPE))
        self.assertTrue(any(name.startswith(rid) for name in os.listdir(qpath)))
        self.assertFalse(os.path.isdir(self._run_path(rid)))   # 原位已空出
        # 隔离后同身份重跑 → 新目录、可用
        run2, errs = self._run(rules_raw=[{"field": "gross_margin",
                                           "op": ">=", "value": 20}])
        self.assertEqual(errs, [])
        self.assertEqual(run2["run_id"], rid)
        self.assertFalse(run2.get("reused"))
        self.assertTrue(os.path.isdir(self._run_path(rid)))

    def test_concurrent_writer_reuses_winner(self):
        # 并发竞争：目录已被另一进程建好（save_run 成功路径）→ 后到者复验后按复用处理
        run = {
            "schema": st.RUN_SCHEMA, "impl_version": ss.IMPL_VERSION,
            "field_map_version": ss.FIELD_MAP_VERSION,
            "run_id": "ab" * 16, "created_at": "2026-01-01T00:00:00+08:00",
            "scope": dict(SCOPE), "as_of": "2026-01-31", "pool": ["000001.SZ"],
            "rules": [], "rules_hash": "0" * 64, "snapshot_id": "snap-x",
            "result_hash": "1" * 64,
            "period_policy": {"mode": "uniform", "report_period": ""},
            "report_period": "", "comparable": True,
            "counts": {"pass": 0, "fail": 0, "insufficient": 0, "total": 0},
            "notes": [], "source": {}, "entities": [], "excluded": [],
            "rows": {"pass": [], "fail": [], "insufficient": []},
        }
        stored1, reused1 = st.save_run(run, SCOPE)
        self.assertFalse(reused1)
        stored2, reused2 = st.save_run(run, SCOPE)
        self.assertTrue(reused2)
        self.assertEqual(stored2["run_id"], run["run_id"])


class TestScopeIsolation(StoreCase):
    """服务端作用域：跨用户列举/读取/预设不可见；搬文件冒充也不可见。"""

    def test_runs_are_scoped_per_user(self):
        run, _ = self._run()
        runs_b = st.list_runs(SCOPE_B)
        self.assertEqual(runs_b, [])
        loaded, _man, err = st.load_run(SCOPE_B, run["run_id"])
        self.assertIsNone(loaded)
        self.assertEqual(err["code"], "not_found")
        # 本人可见
        loaded, _man, err = st.load_run(SCOPE, run["run_id"])
        self.assertIsNone(err)
        self.assertEqual(loaded["run_id"], run["run_id"])

    def test_presets_are_scoped_per_user(self):
        preset, errors = st.save_preset(scope=SCOPE, name="白酒", pool=["000001.SZ"],
                                        rules_raw=RULES)
        self.assertEqual(errors, [])
        self.assertEqual(len(st.list_presets(SCOPE)), 1)
        self.assertEqual(st.list_presets(SCOPE_B), [])
        self.assertIsNone(st.load_preset(SCOPE_B, preset["preset_id"]))
        self.assertFalse(st.delete_preset(SCOPE_B, preset["preset_id"]))
        self.assertTrue(st.delete_preset(SCOPE, preset["preset_id"]))

    def test_copied_preset_file_across_scopes_stays_invisible(self):
        # 防手工搬文件：B 目录里出现 A 的预设文件，但文件内 scope 不匹配 → 不可见
        preset, _ = st.save_preset(scope=SCOPE, name="白酒", pool=["000001.SZ"],
                                   rules_raw=RULES)
        src = os.path.join(st.presets_root(SCOPE), f"{preset['preset_id']}.json")
        dst_root = st.presets_root(SCOPE_B)
        os.makedirs(dst_root, exist_ok=True)
        shutil.copy(src, os.path.join(dst_root, f"{preset['preset_id']}.json"))
        self.assertIsNone(st.load_preset(SCOPE_B, preset["preset_id"]))
        self.assertEqual(st.list_presets(SCOPE_B), [])

    def test_scope_directory_names_do_not_leak_username(self):
        key = st.scope_key(SCOPE)
        self.assertNotIn("alice", key)
        self.assertTrue(key.startswith("u-"))

    def test_missing_scope_user_rejected(self):
        run, errors = self._run(scope={"project": "personal"})
        self.assertIsNone(run)
        self.assertTrue(any("scope" in e for e in errors), errors)
        run, errors = self._run(scope={"user": "alice"})
        self.assertEqual(errors, [])
        self.assertIsNotNone(run)   # user 单独给也允许（project 默认 personal）



class TestLegacyRoots(StoreCase):
    """legacy：R1 原型实例级记录只读保留（owner=None、不可采纳、不迁移）。"""

    def _legacy_dir(self) -> str:
        root = os.path.join(self._tmp, "legacy-runs")
        os.makedirs(root, exist_ok=True)
        os.environ["WEAVEMIND_SCREEN_LEGACY_RUNS"] = root
        return root

    def _write_legacy_run(self, root: str, rid: str) -> None:
        d = os.path.join(root, rid)
        os.makedirs(d, exist_ok=True)
        run = {"schema": "weavemind.stock_screen.run/1", "run_id": rid,
               "created_at": "2026-10-22T10:00:00+08:00", "as_of": "2026-01-31",
               "pool": ["000001.SZ"], "counts": {"pass": 1, "fail": 0,
                                                 "insufficient": 0, "total": 1},
               "notes": ["R1 原型产物"], "rows": {"pass": [], "fail": [],
                                                  "insufficient": []}}
        raw = json.dumps(run, ensure_ascii=False).encode("utf-8")
        with open(os.path.join(d, "run.json"), "wb") as fh:
            fh.write(raw)
        manifest = {"schema": "weavemind.stock_screen.manifest/1", "run_id": rid,
                    "files": [{"name": "run.json", "bytes": len(raw),
                               "sha256": __import__("hashlib").sha256(raw).hexdigest()}]}
        with open(os.path.join(d, "manifest.json"), "wb") as fh:
            fh.write(json.dumps(manifest, ensure_ascii=False).encode("utf-8"))

    def test_legacy_overview_lists_ownerless_runs_readonly(self):
        root = self._legacy_dir()
        rid = "f" * 32
        self._write_legacy_run(root, rid)
        overview = st.legacy_overview()
        self.assertEqual([r["run_id"] for r in overview["runs"]], [rid])
        item = overview["runs"][0]
        self.assertTrue(item["legacy"])
        self.assertIsNone(item["owner"])
        self.assertFalse(item["usable"])
        # 不迁移：文件还在原位
        self.assertTrue(os.path.isdir(os.path.join(root, rid)))

    def test_legacy_run_not_in_scoped_list_and_not_adoptable(self):
        root = self._legacy_dir()
        rid = "e" * 32
        self._write_legacy_run(root, rid)
        self.assertEqual(st.list_runs(SCOPE), [])
        run, _man, err = st.legacy_load_run(rid)
        self.assertIsNone(err)
        self.assertTrue(run["legacy"])
        self.assertIsNone(run["owner"])
        self.assertFalse(run["usable"])
        # 本人作用域读不到 legacy（不自动升级为自己的研究记录）
        _loaded, _man2, err2 = st.load_run(SCOPE, rid)
        self.assertEqual(err2["code"], "not_found")

    def test_legacy_root_empty_is_ok(self):
        os.environ.pop("WEAVEMIND_SCREEN_LEGACY_RUNS", None)
        overview = st.legacy_overview()
        self.assertEqual(overview["runs"], [])


class TestPreviewAndPool(StoreCase):
    """预览：不落结果；股票池目录来自数据本身；真实缓存样例。"""

    def test_preview_creates_no_run_dir(self):
        payload, errors = st.preview(scope=SCOPE, pool=["000001.SZ", "999999.SZ"],
                                     as_of="2026-01-31", rules_raw=RULES)
        self.assertEqual(errors, [])
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(os.path.isdir(st.screen_base()))
        self.assertEqual(payload["missing_pool_codes"], ["999999.SZ"])
        self.assertEqual([p["code"] for p in payload["available_pool"]], ["000001.SZ"])
        self.assertEqual(payload["entities_ready"], 1)
        # 期间政策与建议报告期（预览先给，执行前看得见）
        self.assertEqual(payload["period_policy"], {"mode": "uniform",
                                                    "report_period": ""})
        self.assertEqual(payload["suggested_report_period"], "2024年")
        # PIT：未来行被排除、缺时点行待核验
        self.assertEqual(len(payload["excluded"]), 1)
        self.assertEqual(len(payload["pending"]), 1)
        self.assertEqual(payload["scope"], {"user": "alice", "project": "personal"})

    def test_suggested_period_matches_uniform_default_not_common_max(self):
        """不一致类回归：预览的「默认统一报告期」必须与实际执行同一算法。

        池内一家有更晚年度（甲 2025）而另一家只有 2023/2024 →
        uniform 取**池内最新可用年度**（2025年），不是最大共同年度（2024年）；
        预览建议 = 2025年，execute() 的 report_period 也 = 2025年，
        缺 2025 的公司按不降年列为资料不足。
        """
        os.environ["WEAVEMIND_SCREEN_CACHE"] = _write_cache_multi(
            os.path.join(self._tmp, "cache_multi.json"))
        payload, errors = st.preview(scope=SCOPE, pool=["000001.SZ", "000002.SZ"],
                                     as_of="2026-06-30", rules_raw=RULES)
        self.assertEqual(errors, [])
        self.assertEqual(payload["common_periods"], ["2023年", "2024年"])
        self.assertEqual(payload["suggested_report_period"], "2025年")
        run, errs = self._run(pool=["000001.SZ", "000002.SZ"], as_of="2026-06-30")
        self.assertEqual(errs, [])
        self.assertEqual(run["report_period"], "2025年")
        self.assertEqual(payload["suggested_report_period"], run["report_period"])
        self.assertEqual(run["counts"]["insufficient"], 1)
        ins = run["rows"]["insufficient"][0]
        self.assertEqual(ins["entity_id"], "000002.SZ")
        why = "；".join(ins["reasons"])
        self.assertIn("2025年", why)
        self.assertIn("不回退", why)

    def test_preview_rejects_bad_period_policy(self):
        payload, errors = st.preview(scope=SCOPE, pool=["000001.SZ"],
                                     as_of="2026-01-31", rules_raw=RULES,
                                     period_policy_raw={"mode": "bogus"})
        self.assertEqual(payload, {})
        self.assertTrue(any("期间政策" in e or "模式" in e for e in errors), errors)

    def test_missing_cache_reports_explicit_no_data_not_crash(self):
        """干净检出/容器镜像里没有 data/screen_cache：必须是明确「文件不存在」，不能崩。"""
        old = os.environ.get("WEAVEMIND_SCREEN_CACHE")
        os.environ["WEAVEMIND_SCREEN_CACHE"] = os.path.join(
            tempfile.gettempdir(), "weavemind-not-here", "official_disclosure_cache.json")
        try:
            payload, errors, meta = st.load_source()
        finally:
            if old is None:
                os.environ.pop("WEAVEMIND_SCREEN_CACHE", None)
            else:
                os.environ["WEAVEMIND_SCREEN_CACHE"] = old
        self.assertEqual(payload, {})
        self.assertTrue(errors)
        self.assertIn("文件不存在", errors[0])
        self.assertEqual(meta.get("row_count"), 0)   # 不假装有数据

    def test_available_pool_is_cn_and_sorted(self):
        payload, _errors, _meta = st.load_source()
        payload["rows"].append(_row("腾讯控股", "00700.HK", "2024-12-31",
                                    avail="2025-04-01", revenue=1))
        pool = st.available_pool(payload)
        codes = [p["code"] for p in pool]
        self.assertEqual(codes, ["000001.SZ"])   # HK 界外，不进候选

    @unittest.skipUnless(
        os.path.exists(os.path.join(os.path.dirname(os.path.abspath(st.__file__)),
                                    "data", "screen_cache",
                                    "official_disclosure_cache.json")),
        "本机没有真实披露缓存样本（干净检出与容器镜像里就是这样：不算失败）")
    def test_real_cache_sample_two_companies(self):
        """真实缓存（data/screen_cache）：来源身份可用，洋河/三一在候选池。"""
        os.environ["WEAVEMIND_SCREEN_CACHE"] = os.path.join(
            os.path.dirname(os.path.abspath(st.__file__)),
            "data", "screen_cache", "official_disclosure_cache.json")
        payload, errors, meta = st.load_source()
        self.assertEqual(errors, [])
        self.assertEqual(meta["label"], "official_disclosure_cache")
        self.assertFalse(meta["synthetic"])
        self.assertEqual(meta["sha256"], ss.payload_sha256(st.default_cache_path()))
        pool = st.available_pool(payload)
        self.assertEqual([p["code"] for p in pool], ["002304.SZ", "600031.SH"])
        run, errs = st.execute(scope=SCOPE, pool=["002304.SZ", "600031.SH"],
                               as_of="2025-04-30",
                               rules_raw=[{"field": "gross_margin",
                                           "op": ">=", "value": 70},
                                          {"field": "debt_ratio",
                                           "op": "<=", "value": 60}])
        self.assertEqual(errs, [])
        # 洋河过（毛利 73.16% ≥ 70，负债 23.24% ≤ 60）；三一毛利 26.43% 不达标
        self.assertEqual(run["counts"], {"pass": 1, "fail": 1, "insufficient": 0,
                                         "total": 2})
        self.assertEqual(run["rows"]["pass"][0]["entity_id"], "002304.SZ")


class TestEvidenceBinding(StoreCase):
    """证据：绑 (run_id, entity_id)，读数可解析到观察。"""

    def test_evidence_binding_resolves_fact_ids(self):
        run, _ = self._run()
        ev = st.evidence(run, "000001.SZ")
        self.assertIsNotNone(ev)
        self.assertEqual(ev["run_id"], run["run_id"])
        self.assertEqual(ev["entity_id"], "000001.SZ")
        self.assertEqual(ev["verdict"], "pass")
        self.assertEqual(ev["eval_period"], "2024年")
        readings = {r["field"]: r for r in ev["readings"]}
        gm = readings["gross_margin"]
        self.assertEqual(len(gm["inputs"]), 2)
        self.assertTrue(all(d["source_url"].startswith("http://example.com/")
                            for d in gm["inputs"]))
        self.assertEqual(gm["unresolved_inputs"], [])
        self.assertEqual({d["metric"] for d in gm["inputs"]},
                         {"gross_profit", "revenue"})
        for d in gm["inputs"]:
            self.assertTrue(d["period"].endswith("年"))
            self.assertTrue(d["period_end"])
            self.assertTrue(d["caliber"])
        self.assertEqual(len(ev["excluded"]), 1)
        self.assertEqual(len(ev["pending_pit"]), 1)
        self.assertEqual(ev["scope"], {"user": "alice", "project": "personal"})

    def test_evidence_unknown_entity_is_none(self):
        run, _ = self._run()
        self.assertIsNone(st.evidence(run, "888888.SZ"))
        self.assertIsNone(st.evidence(run, ""))
        self.assertIsNone(st.evidence({}, "000001.SZ"))

    def test_synthetic_flag_propagates_into_run(self):
        run, _ = self._run()
        self.assertTrue(run["source"]["synthetic"])
        self.assertTrue(any("合成演示" in n for n in run["notes"]))


class TestListRuns(StoreCase):
    """历史列表：本作用域、新到旧、坏目录标出。"""

    def test_list_runs_newest_first_with_summary(self):
        run1, _ = self._run()
        run2, _ = self._run(rules_raw=[{"field": "gross_margin",
                                        "op": ">=", "value": 30}])
        runs = st.list_runs(SCOPE)
        self.assertEqual(len(runs), 2)
        self.assertEqual({r["run_id"] for r in runs},
                         {run1["run_id"], run2["run_id"]})
        created = [r["created_at"] for r in runs]
        self.assertEqual(created, sorted(created, reverse=True))
        for r in runs:
            self.assertIn("counts", r)
            self.assertIn("integrity", r)
            self.assertIn("usable", r)
            self.assertIn("source", r)
            self.assertIn("report_period", r)

    def test_list_runs_empty_root(self):
        self.assertEqual(st.list_runs(SCOPE), [])

    def test_list_runs_flags_unreadable_dir(self):
        run, _ = self._run()
        d = self._run_path(run["run_id"])
        os.remove(os.path.join(d, "run.json"))
        runs = st.list_runs(SCOPE)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["integrity"], "unreadable")
        self.assertFalse(runs[0]["usable"])


class TestPresets(StoreCase):
    """预设（股票池+规则）：可保存、可编辑、可删除，且永不改动已冻结结果。"""

    def test_save_list_load_roundtrip(self):
        preset, errors = st.save_preset(scope=SCOPE, name="白酒高毛利",
                                        pool=["000001.SZ"],
                                        rules_raw=[{"field": "gross_margin",
                                                    "op": ">=", "value": 70}],
                                        as_of="2026-01-31")
        self.assertEqual(errors, [])
        listed = st.list_presets(SCOPE)
        self.assertEqual([p["preset_id"] for p in listed], [preset["preset_id"]])
        back = st.load_preset(SCOPE, preset["preset_id"])
        self.assertEqual(back["pool"], ["000001.SZ"])
        self.assertEqual(back["rules"][0]["field"], "gross_margin")
        self.assertEqual(back["as_of"], "2026-01-31")
        self.assertEqual(back["created_at"], back["updated_at"])
        self.assertEqual(back["scope"], {"user": "alice", "project": "personal"})

    def test_preset_remembers_dataset_source_and_refuses_dead_one(self):
        """预设要把数据来源一起记住：只记池与规则，重开时会静默换用另一份数据。"""
        meta, _reused, errors = st.save_dataset(scope=SCOPE, name="我的导入",
                                               filename="mine.csv",
                                               text=_csv_text(*_DATASET_ROWS))
        self.assertEqual(errors, [])
        preset, errors = st.save_preset(scope=SCOPE, name="带来源", pool=["000001.SZ"],
                                        rules_raw=[{"field": "gross_margin",
                                                    "op": ">=", "value": 20}],
                                        dataset_id=meta["dataset_id"])
        self.assertEqual(errors, [])
        self.assertEqual(preset["dataset_id"], meta["dataset_id"])
        self.assertEqual(st.list_presets(SCOPE)[0]["dataset_id"], meta["dataset_id"])
        self.assertEqual(st.load_preset(SCOPE, preset["preset_id"])["dataset_id"],
                         meta["dataset_id"])
        # 指向不存在的数据集：整体拒绝，不落半份预设
        bad, errors = st.save_preset(scope=SCOPE, name="空来源", pool=["000001.SZ"],
                                     rules_raw=[{"field": "gross_margin",
                                                 "op": ">=", "value": 20}],
                                     dataset_id="f" * 32)
        self.assertIsNone(bad)
        self.assertTrue(any("不存在或已删除" in e for e in errors))
        self.assertEqual([p["name"] for p in st.list_presets(SCOPE)], ["带来源"])

    def test_update_keeps_id_and_created_at(self):
        first, _ = st.save_preset(scope=SCOPE, name="甲", pool=["000001.SZ"],
                                  rules_raw=[{"field": "gross_margin",
                                              "op": ">=", "value": 70}])
        second, errors = st.save_preset(scope=SCOPE, name="乙",
                                        pool=["000001.SZ", "600031.SH"],
                                        rules_raw=[{"field": "debt_ratio",
                                                    "op": "<=", "value": 60}],
                                        preset_id=first["preset_id"])
        self.assertEqual(errors, [])
        self.assertEqual(second["preset_id"], first["preset_id"])
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(second["name"], "乙")
        self.assertEqual(len(st.list_presets(SCOPE)), 1)     # 更新不是新增

    def test_unknown_field_rejected_and_nothing_written(self):
        preset, errors = st.save_preset(scope=SCOPE, name="坏的", pool=["000001.SZ"],
                                        rules_raw=[{"field": "net_margin",
                                                    "op": ">=", "value": 1}])
        self.assertIsNone(preset)
        self.assertTrue(any("未知字段" in e for e in errors), errors)
        self.assertEqual(st.list_presets(SCOPE), [])

    def test_empty_name_or_pool_rejected(self):
        _, errors = st.save_preset(scope=SCOPE, name="  ", pool=["000001.SZ"],
                                   rules_raw=[{"field": "gross_margin",
                                               "op": ">=", "value": 1}])
        self.assertTrue(any("名称" in e for e in errors))
        _, errors = st.save_preset(scope=SCOPE, name="空的", pool=[],
                                   rules_raw=[{"field": "gross_margin",
                                               "op": ">=", "value": 1}])
        self.assertTrue(any("股票池" in e for e in errors))

    def test_bad_asof_rejected(self):
        _, errors = st.save_preset(scope=SCOPE, name="日期坏", pool=["000001.SZ"],
                                   rules_raw=[{"field": "gross_margin",
                                               "op": ">=", "value": 1}],
                                   as_of="2025-13-45")
        self.assertTrue(any("截止日" in e for e in errors), errors)

    def test_path_traversal_rejected(self):
        _, errors = st.save_preset(scope=SCOPE, name="越界", pool=["000001.SZ"],
                                   rules_raw=[{"field": "gross_margin",
                                               "op": ">=", "value": 1}],
                                   preset_id="../../../etc/passwd")
        self.assertTrue(errors)
        self.assertIsNone(st.load_preset(SCOPE, "../../etc/passwd"))
        self.assertFalse(st.delete_preset(SCOPE, "..\\..\\windows\\win.ini"))
        self.assertIsNone(st.load_preset(SCOPE, "0123456789abcdef"))
        self.assertFalse(st.delete_preset(SCOPE, "0123456789abcdef"))

    def test_delete_does_not_touch_runs(self):
        run, errors = self._run()
        self.assertEqual(errors, [])
        preset, _ = st.save_preset(scope=SCOPE, name="会删", pool=["000001.SZ"],
                                   rules_raw=RULES)
        self.assertTrue(st.delete_preset(SCOPE, preset["preset_id"]))
        self.assertEqual(st.list_presets(SCOPE), [])
        back, _man, err = st.load_run(SCOPE, run["run_id"])
        self.assertIsNone(err)
        self.assertEqual(back["run_id"], run["run_id"])

    def test_broken_preset_file_skipped(self):
        root = st.presets_root(SCOPE)
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "0123456789abcdef.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{ not json")
        st.save_preset(scope=SCOPE, name="好的", pool=["000001.SZ"], rules_raw=RULES)
        listed = st.list_presets(SCOPE)
        self.assertEqual([p["name"] for p in listed], ["好的"])

    def test_pool_codes_are_normalized_and_deduped(self):
        preset, errors = st.save_preset(scope=SCOPE, name="去重",
                                        pool=["000001.sz", "000001.SZ", " "],
                                        rules_raw=RULES)
        self.assertEqual(errors, [])
        self.assertEqual(preset["pool"], ["000001.SZ"])

    def test_preset_hash_matches_rules(self):
        rules = [{"field": "gross_margin", "op": ">=", "value": 70}]
        preset, _ = st.save_preset(scope=SCOPE, name="哈希", pool=["000001.SZ"],
                                   rules_raw=rules)
        parsed, _errs = ss.parse_rules(rules)
        self.assertEqual(preset["rules_hash"], ss.rules_hash(parsed))

    def test_too_many_rules_rejected(self):
        rules = [{"field": "gross_margin", "op": ">=", "value": v}
                 for v in range(1, 40)]
        _, errors = st.save_preset(scope=SCOPE, name="超量", pool=["000001.SZ"],
                                   rules_raw=rules)
        self.assertTrue(errors)



# ============================================================
# R1.2 数据入口：模板 / 上传数据集
# ============================================================

def _csv_row(entity, code, period, *, avail="2025-04-18",
             url="http://example.com/report.pdf", **metrics):
    row = {"entity": entity, "code": code, "period": period, "unit": "元",
           "currency": "CNY", "caliber": "合并", "period_type": "年报",
           "source_url": url}
    if avail is not None:
        row["available_at"] = avail
        row["disclosed_at"] = avail
    row.update(metrics)
    return row


def _csv_text(*rows) -> str:
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=list(st.CSV_COLUMNS),
                             extrasaction="ignore", restval="")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


_DATASET_ROWS = (
    _csv_row("甲公司", "000001.SZ", "2023-12-31", avail="2024-04-20",
             revenue=200_000_000, gross_profit=50_000_000, net_profit=25_000_000,
             operating_cashflow=30_000_000, total_assets=200_000_000,
             total_liabilities=80_000_000),
    _csv_row("甲公司", "000001.SZ", "2024-12-31", avail="2025-04-18",
             revenue=220_000_000, gross_profit=55_000_000, net_profit=30_000_000,
             operating_cashflow=36_000_000, total_assets=210_000_000,
             total_liabilities=84_000_000),
)


class TestDatasets(StoreCase):
    """R1.2：模板可下载可回填；上传只落本作用域；来源永不自动变成官方；损坏不复用。"""

    def _save(self, rows=None, *, name="我的导入", filename="a.csv", **kw):
        return st.save_dataset(scope=SCOPE, name=name, filename=filename,
                               text=_csv_text(*(rows or _DATASET_ROWS)), **kw)

    def test_template_contains_header_and_removable_examples(self):
        text = st.template_csv()
        self.assertTrue(text.splitlines()[0].startswith("entity,code,period"))
        payload, errors = st.parse_dataset_csv(text, filename="template.csv")
        self.assertEqual(errors, [])
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual({r["code"] for r in payload["rows"]}, {"000000.SZ", "000000.SH"})
        self.assertTrue(payload["metadata"]["user_provided"])
        self.assertFalse(payload["metadata"]["synthetic"])
        # 模板本身可直接通过 wide 行契约（示例可被删除，但不会因形状被拒）
        self.assertEqual(ss.validate_payload(payload)[1], [])

    def test_save_is_content_addressed_reused_and_scoped(self):
        meta, reused, errors = self._save()
        self.assertEqual(errors, [])
        self.assertFalse(reused)
        self.assertEqual(meta["row_count"], 2)
        self.assertEqual(meta["codes"], ["000001.SZ"])
        # 同内容换个名字/文件名 → 仍是同一数据集（身份只含数据，不含命名）
        meta2, reused2, errors2 = self._save(name="换个名字", filename="b.csv")
        self.assertEqual(errors2, [])
        self.assertTrue(reused2)
        self.assertEqual(meta2["dataset_id"], meta["dataset_id"])
        # 同 ID 目录不会被第二次写入覆盖（内容哈希一致才复用）
        loaded, payload, error = st.load_dataset(SCOPE, meta["dataset_id"])
        self.assertEqual(error, "")
        self.assertEqual(len(payload["rows"]), 2)
        # 作用域隔离：别人列不到、读不到（404 语义，不泄露存在性）
        self.assertEqual(st.list_datasets(SCOPE_B), [])
        other_meta, _other_payload, other_error = st.load_dataset(SCOPE_B, meta["dataset_id"])
        self.assertEqual(other_meta, {})
        self.assertIn("不存在", other_error)

    def test_upload_cannot_declare_itself_official(self):
        # 结构上无法自封：CSV 没有 official 列，硬塞一列会被当未知列拒绝
        base = _csv_text(_DATASET_ROWS[0]).splitlines()
        text = "\n".join(["official," + base[0], "true," + base[1]]) + "\n"
        _payload, errors = st.parse_dataset_csv(text)
        self.assertTrue(errors)
        self.assertIn("未知列", errors[0])
        self.assertIn("official", errors[0])
        meta, _reused, _errs = self._save()
        self.assertFalse(meta["official"])
        self.assertTrue(meta["user_provided"])
        source = st.dataset_source_meta(meta)
        self.assertFalse(source["official"])
        self.assertTrue(source["user_provided"])
        self.assertFalse(source["synthetic"])

    def test_parse_errors_use_file_line_numbers(self):
        blank_period = _csv_row("甲公司", "000001.SZ", "")
        _p, errors = st.parse_dataset_csv(_csv_text(blank_period))
        self.assertTrue(any("第 2 行 缺 period" in e for e in errors), errors)
        bad_number = _csv_row("甲公司", "000001.SZ", "2024-12-31", revenue="一亿")
        _p, errors = st.parse_dataset_csv(_csv_text(bad_number))
        self.assertTrue(any("第 2 行 revenue 不是数" in e and "一亿" in e for e in errors), errors)
        _p, errors = st.parse_dataset_csv(
            "entity,code,period,备注\n甲公司,000001.SZ,2024-12-31,备忘\n")
        self.assertTrue(any("未知列" in e and "备注" in e for e in errors), errors)
        _p, errors = st.parse_dataset_csv("")
        self.assertIn("上传内容为空", errors[0])

    def test_row_limit_is_explicit_not_truncated(self):
        rows = tuple(_csv_row("甲公司", "000001.SZ", f"20{20 + i}-12-31",
                              revenue=1_000_000) for i in range(3))
        meta, _reused, errors = self._save(rows, max_rows=2)
        self.assertIsNone(meta)
        self.assertTrue(any("超过上限 2" in e for e in errors), errors)

    def test_corrupt_dataset_is_refused_not_silently_used(self):
        meta, _reused, _errors = self._save()
        path = os.path.join(st.datasets_root(SCOPE), meta["dataset_id"], "rows.json")
        with open(path, "rb") as fh:
            data = fh.read()
        with open(path, "wb") as fh:
            fh.write(data + b" ")
        loaded, _payload, error = st.load_dataset(SCOPE, meta["dataset_id"])
        self.assertEqual(loaded, {})
        self.assertIn("哈希不一致", error)
        run, errors = self._run(dataset_id=meta["dataset_id"])
        self.assertIsNone(run)
        self.assertTrue(any("哈希不一致" in e for e in errors), errors)

    def test_execute_with_dataset_id_keeps_provenance_and_identity(self):
        meta, _reused, _errors = self._save()
        run, errors = self._run(dataset_id=meta["dataset_id"])
        self.assertEqual(errors, [])
        self.assertEqual(run["source"]["dataset_id"], meta["dataset_id"])
        self.assertFalse(run["source"]["official"])
        self.assertTrue(run["source"]["user_provided"])
        self.assertIn("用户数据集", run["source"]["label"])
        cache_run, cache_errors = self._run()
        self.assertEqual(cache_errors, [])
        self.assertNotEqual(run["run_id"], cache_run["run_id"], "来源不同 → 结果身份不同")
        again, _again_errors = self._run(dataset_id=meta["dataset_id"])
        self.assertTrue(again["reused"])

    def test_dataset_id_resolution_is_exact_for_full_ids(self):
        meta, _reused, _errors = self._save()
        did = meta["dataset_id"]
        self.assertEqual(st.load_dataset(SCOPE, did.upper())[0]["dataset_id"], did)
        self.assertEqual(st.load_dataset(SCOPE, did[:8])[0]["dataset_id"], did)
        other = f"{did[:31]}{'1' if not did.endswith('1') else '0'}"
        self.assertNotEqual(other, did)
        loaded, _p, error = st.load_dataset(SCOPE, other)
        self.assertEqual(loaded, {})
        self.assertIn("精确匹配", error)
        loaded, _p, error = st.load_dataset(SCOPE, "zz")
        self.assertEqual(loaded, {})
        self.assertIn("不合法", error)


class TestCompareSemantics(StoreCase):
    """B0：比较语义纳入实现版本、字段口径与**实际评估期**；池变化与结论变化分账。"""

    def _dataset_run(self, *, label, revenue, gross_profit, pool=("000001.SZ",),
                     rules=None, as_of="2026-01-31", single_period=False):
        rows = [_row("甲公司", "000001.SZ", "2023-12-31", avail="2024-04-20",
                     revenue=200_000_000, gross_profit=50_000_000,
                     net_profit=25_000_000, operating_cashflow=30_000_000,
                     total_assets=200_000_000, total_liabilities=80_000_000),
                _row("甲公司", "000001.SZ", "2024-12-31", avail="2025-04-18",
                     revenue=revenue, gross_profit=gross_profit,
                     net_profit=30_000_000, operating_cashflow=36_000_000,
                     total_assets=210_000_000, total_liabilities=84_000_000)]
        if single_period:
            rows = rows[:1]          # 只留 2023 年度：模式仍是「统一」，但实际评估期不同
        header = list(st.CSV_COLUMNS)
        lines = [",".join(header)]
        for row in rows:
            lines.append(",".join(str(row.get(c, "")) for c in header))
        meta, _reused, errors = st.save_dataset(scope=SCOPE, name=label, filename="a.csv",
                                                text="\r\n".join(lines) + "\r\n")
        self.assertEqual(errors, [])
        run, errors = st.execute(scope=SCOPE, pool=list(pool), as_of=as_of,
                                 rules_raw=rules or RULES, dataset_id=meta["dataset_id"])
        self.assertEqual(errors, [])
        return run

    def test_impl_version_change_is_not_attributable(self):
        a = self._dataset_run(label="同版", revenue=220_000_000, gross_profit=55_000_000)
        old = ss.IMPL_VERSION
        ss.IMPL_VERSION = old + "-changed"
        try:
            b = self._dataset_run(label="同版", revenue=220_000_000, gross_profit=55_000_000)
        finally:
            ss.IMPL_VERSION = old
        result, errors = st.compare_runs(SCOPE, a["run_id"], b["run_id"])
        self.assertEqual(errors, [])
        keys = {c["key"] for c in result["changed_conditions"]}
        self.assertIn("impl_version", keys)
        self.assertEqual(result["attribution"]["code"], "not_attributable")
        self.assertTrue(any("版本" in r for r in result["attribution"]["reasons"]))
        # 只读：两侧原文件字节不变
        for rid in (a["run_id"], b["run_id"]):
            path = os.path.join(self._run_path(rid), "run.json")
            with open(path, "rb") as fh:
                before = fh.read()
            st.compare_runs(SCOPE, a["run_id"], b["run_id"])
            with open(path, "rb") as fh:
                self.assertEqual(before, fh.read())

    def test_unknown_version_is_not_treated_as_equal(self):
        a = self._dataset_run(label="同版", revenue=220_000_000, gross_profit=55_000_000)
        old = ss.IMPL_VERSION
        ss.IMPL_VERSION = ""
        try:
            b = self._dataset_run(label="同版", revenue=220_000_000, gross_profit=55_000_000)
        finally:
            ss.IMPL_VERSION = old
        result, errors = st.compare_runs(SCOPE, a["run_id"], b["run_id"])
        self.assertEqual(errors, [])
        cond = {c["key"]: c for c in result["conditions"]}
        self.assertFalse(cond["impl_version"]["same"])
        self.assertIn("未记录", cond["impl_version"]["other"])
        self.assertEqual(result["attribution"]["code"], "not_attributable")

    def test_same_mode_different_actual_period_is_period_change(self):
        a = self._dataset_run(label="只有 2023", revenue=220_000_000,
                              gross_profit=55_000_000, single_period=True)
        b = self._dataset_run(label="两年", revenue=220_000_000, gross_profit=55_000_000)
        result, errors = st.compare_runs(SCOPE, a["run_id"], b["run_id"])
        self.assertEqual(errors, [])
        cond = {c["key"]: c for c in result["conditions"]}
        self.assertEqual(cond["period_policy"]["base"], cond["period_policy"]["other"])
        self.assertFalse(cond["eval_periods"]["same"])
        self.assertIn("2023年", cond["eval_periods"]["base"])
        self.assertIn("2024年", cond["eval_periods"]["other"])
        self.assertEqual(result["attribution"]["code"], "not_attributable")
        self.assertTrue(any("评估期" in r for r in result["attribution"]["reasons"]))

    def test_pool_change_counted_separately_from_verdict_change(self):
        base = self._dataset_run(label="同数据", revenue=220_000_000,
                                 gross_profit=55_000_000, pool=("000001.SZ",))
        # 池里加一个不在数据里的代码：结果里多一行「资料不足」
        wider = self._dataset_run(label="同数据", revenue=220_000_000,
                                  gross_profit=55_000_000,
                                  pool=("000001.SZ", "000099.SZ"))
        result, errors = st.compare_runs(SCOPE, base["run_id"], wider["run_id"])
        self.assertEqual(errors, [])
        self.assertEqual(result["summary"]["pool_added"], 1)
        self.assertEqual(result["summary"]["pool_removed"], 0)
        self.assertEqual(result["summary"]["entered"], 1)
        self.assertEqual(result["summary"]["became_pass"], 0)
        self.assertEqual(result["entered"][0]["reason_kind"], "pool_added")
        self.assertIn("股票池这次新增", result["entered"][0]["explain"])

    def test_verdict_change_counts_are_split(self):
        failing = self._dataset_run(
            label="现金流弱", revenue=220_000_000, gross_profit=55_000_000,
            rules=[{"field": "cashflow_coverage", "op": ">=", "value": 200}])
        passing = self._dataset_run(
            label="现金流强", revenue=220_000_000, gross_profit=55_000_000,
            rules=[{"field": "cashflow_coverage", "op": ">=", "value": 200},
                   {"field": "gross_margin", "op": ">=", "value": 20}])
        result, errors = st.compare_runs(SCOPE, failing["run_id"], passing["run_id"])
        self.assertEqual(errors, [])
        s = result["summary"]
        self.assertEqual(s["pool_added"], 0)
        self.assertEqual(s["entered"], 0)
        self.assertEqual(s["became_pass"], 0)   # 规则变了：不算「同一条件下的新通过」
        self.assertEqual(s["pool_removed"], 0)


class TestProvenance(StoreCase):
    """（真实官方缓存的来源性质单独一条，见文件末尾 TestRealCacheProvenance）"""
    """B0：来源性质 / 文件指纹 / 人工复核分开，任何路径都不代签复核。"""

    def test_official_cache_provenance_is_normalized_not_verified(self):
        payload, errors, meta = st.load_source()
        self.assertEqual(errors, [])
        prov = meta["provenance"]
        # 测试夹具标了 synthetic：性质就是合成演示，不是「官方」
        self.assertEqual(prov["kind"], "synthetic_demo")
        self.assertFalse(prov["original_verified"])
        self.assertEqual(prov["human_review"]["status"], "not_reviewed")
        self.assertIsNone(prov["human_review"]["by"])
        self.assertTrue(prov["file_sha256"])
        self.assertTrue(prov["file_sha256"])
        self.assertIsNone(prov["human_review"]["by"])

    def test_user_dataset_provenance_is_user_provided(self):
        meta, _reused, errors = st.save_dataset(
            scope=SCOPE, name="我的数据", filename="mine.csv",
            text=st.template_csv().replace("示例公司甲（示例行，请删除）", "甲公司")
                                   .replace("000000.SZ", "000001.SZ")
                                   .replace("示例公司乙（示例行，请删除）", "乙公司")
                                   .replace("000000.SH", "000002.SZ"))
        self.assertEqual(errors, [])
        payload, _errs, src = st._resolve_input(SCOPE, meta["dataset_id"])
        prov = src["provenance"]
        self.assertEqual(prov["kind"], "user_provided")
        self.assertFalse(prov["original_verified"])
        self.assertEqual(prov["human_review"]["status"], "not_reviewed")
        self.assertIn("用户上传", prov["note"])

    def test_run_source_carries_provenance_and_compare_shows_kind(self):
        run, errors = self._run()
        self.assertEqual(errors, [])
        self.assertIn("provenance", run["source"])
        self.assertEqual(run["source"]["provenance"]["kind"], "synthetic_demo")
        other, _ = self._run(as_of="2025-01-01")
        result, errors = st.compare_runs(SCOPE, run["run_id"], other["run_id"])
        self.assertEqual(errors, [])
        label = next(c for c in result["conditions"] if c["key"] == "source")
        self.assertIn("合成演示", label["base"])


class TestConcurrencyAndInterrupt(StoreCase):
    """B0：并发写、写入中断——不覆盖、不静默失败、可重试恢复。"""

    def test_concurrent_same_identity_execute_yields_one_result(self):
        import threading
        results, errors = [], []
        lock = threading.Lock()

        def worker():
            try:
                run, errs = self._run()
                with lock:
                    results.append(run)
                    errors.extend(errs)
            except Exception as exc:            # noqa: BLE001 - 线程里必须收拢
                with lock:
                    errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({r["run_id"] for r in results}), 1)
        self.assertEqual(sum(1 for r in results if not r.get("reused")), 1)
        rid = results[0]["run_id"]
        run, _manifest, integrity, detail = st._read_run_dir(self._run_path(rid), rid)
        self.assertEqual(integrity, "ok", detail)

    def test_concurrent_same_dataset_content_reuses_one_id(self):
        import threading
        text = st.template_csv().replace("000000.SZ", "000001.SZ")
        metas, errors = [], []
        lock = threading.Lock()

        def worker(idx):
            try:
                meta, _reused, errs = st.save_dataset(scope=SCOPE, name=f"并发数据集{idx}",
                                                      filename="same.csv", text=text)
                with lock:
                    metas.append(meta)
                    errors.extend(errs)
            except Exception as exc:            # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({m["dataset_id"] for m in metas}), 1)
        # 同内容换名称仍是同一 ID：错误提示不能让人以为「换名字就能绕过」
        meta2, reused, errs2 = st.save_dataset(scope=SCOPE, name="换个名字", filename="b.csv",
                                               text=text)
        self.assertEqual(errs2, [])
        self.assertTrue(reused)
        self.assertEqual(meta2["dataset_id"], metas[0]["dataset_id"])

    def test_interrupted_write_leaves_no_result_and_can_retry(self):
        original = st._write_one
        calls = {"n": 0}

        def flaky(target, name, obj):
            calls["n"] += 1
            if name == "manifest.json":
                raise OSError(28, "磁盘已满（模拟中断）")
            return original(target, name, obj)

        st._write_one = flaky
        try:
            with self.assertRaises(st.StoreWriteError):
                self._run()
        finally:
            st._write_one = original
        root = st.runs_root(SCOPE)
        leftovers = [n for n in os.listdir(root) if n.startswith(".tmp-")] if os.path.isdir(root) else []
        self.assertEqual(leftovers, [], "中断的临时目录必须清干净")
        self.assertEqual([n for n in (os.listdir(root) if os.path.isdir(root) else [])
                          if len(n) == 32], [])
        run, errors = self._run()
        self.assertEqual(errors, [])
        self.assertTrue(os.path.isdir(self._run_path(run["run_id"])))

    def test_corrupt_dataset_dir_message_does_not_suggest_rename(self):
        text = st.template_csv().replace("000000.SZ", "000001.SZ")
        meta, _reused, errors = st.save_dataset(scope=SCOPE, name="原数据集",
                                               filename="a.csv", text=text)
        self.assertEqual(errors, [])
        os.remove(st._dataset_meta_path(os.path.join(st.datasets_root(SCOPE),
                                                    meta["dataset_id"])))
        again, reused, errors = st.save_dataset(scope=SCOPE, name="换个名字",
                                                filename="b.csv", text=text)
        self.assertIsNone(again)
        self.assertFalse(reused)
        msg = errors[0]
        self.assertIn("换名称不会得到新 ID", msg)
        self.assertNotIn("请更换名称", msg)

    def test_stray_tmp_dir_is_ignored_by_listing(self):
        run, _ = self._run()
        root = st.runs_root(SCOPE)
        stray = os.path.join(root, ".tmp-deadbeef-999-aaaa")
        os.makedirs(stray, exist_ok=True)
        with open(os.path.join(stray, "run.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        listed = st.list_runs(SCOPE)
        self.assertEqual([r["run_id"] for r in listed], [run["run_id"]])

if __name__ == "__main__":
    unittest.main(verbosity=1)


class TestCompare(StoreCase):
    """R1.3：两份冻结结果的人工复筛对比（只读、字段白名单、条件变了不可直接归因）。"""

    def _dataset_run(self, *, label, revenue, gross_profit, pool=("000001.SZ",),
                     rules=None, as_of="2026-01-31", single_period=False):
        rows = [_row("甲公司", "000001.SZ", "2023-12-31", avail="2024-04-20",
                     revenue=200_000_000, gross_profit=50_000_000,
                     net_profit=25_000_000, operating_cashflow=30_000_000,
                     total_assets=200_000_000, total_liabilities=80_000_000),
                _row("甲公司", "000001.SZ", "2024-12-31", avail="2025-04-18",
                     revenue=revenue, gross_profit=gross_profit,
                     net_profit=30_000_000, operating_cashflow=36_000_000,
                     total_assets=210_000_000, total_liabilities=84_000_000)]
        if single_period:
            rows = rows[:1]          # 只留 2023 年度：模式仍是「统一」，但实际评估期不同
        header = list(st.CSV_COLUMNS)
        lines = [",".join(header)]
        for row in rows:
            lines.append(",".join(str(row.get(c, "")) for c in header))
        meta, _reused, errors = st.save_dataset(scope=SCOPE, name=label, filename="a.csv",
                                                text="\r\n".join(lines) + "\r\n")
        self.assertEqual(errors, [])
        run, errors = st.execute(scope=SCOPE, pool=list(pool), as_of=as_of,
                                 rules_raw=rules or RULES, dataset_id=meta["dataset_id"])
        self.assertEqual(errors, [])
        return run

    def test_compare_between_two_frozen_results_with_field_changes(self):
        a = self._dataset_run(label="旧数据", revenue=220_000_000, gross_profit=55_000_000)
        b = self._dataset_run(label="新数据", revenue=220_000_000, gross_profit=44_000_000)
        self.assertNotEqual(a["run_id"], b["run_id"])
        result, errors = st.compare_runs(SCOPE, a["run_id"], b["run_id"])
        self.assertEqual(errors, [])
        self.assertEqual(result["schema"], st.COMPARE_SCHEMA)
        # 只有数据来源变了（规则/池/期间口径/截止日都相同）
        self.assertEqual([c["key"] for c in result["changed_conditions"]], ["source"])
        self.assertEqual(result["attribution"]["code"], "input_changed")
        self.assertFalse(result["attribution"]["attributable"])
        stayed = {r["code"]: r for r in result["stayed"]}
        change = stayed["000001.SZ"]["field_changes"]
        self.assertEqual([c["field"] for c in change], ["gross_margin"])
        self.assertEqual(change[0]["kind"], "value")
        self.assertEqual(change[0]["base"]["display"], "25.00%")
        self.assertEqual(change[0]["other"]["display"], "20.00%")
        # 白名单外的字段不会被当成读数变化
        self.assertTrue(all(c["field"] in st.COMPARE_FIELDS for c in change))

    def test_compare_rule_change_is_not_attributable(self):
        a = self._dataset_run(label="原条件", revenue=220_000_000, gross_profit=55_000_000)
        b = self._dataset_run(label="原条件", revenue=220_000_000, gross_profit=55_000_000,
                              rules=[{"field": "gross_margin", "op": ">=", "value": 90}])
        result, errors = st.compare_runs(SCOPE, a["run_id"], b["run_id"])
        self.assertEqual(errors, [])
        self.assertIn("rules", {c["key"] for c in result["changed_conditions"]})
        self.assertEqual(result["attribution"]["code"], "not_attributable")
        self.assertEqual(result["attribution"]["label"], "不可直接归因")
        self.assertTrue(any("筛选规则" in r for r in result["attribution"]["reasons"]))
        # 规则变了 → 也会出现「读数条目不同」这类结构性差异，但结论必须写明不可归因
        self.assertIn("不可直接归因", result["attribution"]["label"])

    def test_compare_entered_and_exited_with_pool_change(self):
        a = self._dataset_run(label="宽池", revenue=220_000_000, gross_profit=55_000_000,
                              pool=("000001.SZ", "000002.SZ"))
        b = self._dataset_run(label="窄池", revenue=220_000_000, gross_profit=55_000_000,
                              pool=("000001.SZ",))
        result, errors = st.compare_runs(SCOPE, a["run_id"], b["run_id"])
        self.assertEqual(errors, [])
        self.assertEqual([r["code"] for r in result["exited"]], ["000002.SZ"])
        self.assertEqual(result["exited"][0]["verdict_label"], "资料不足")
        self.assertEqual(result["summary"]["entered"], 0)
        self.assertIn("pool", {c["key"] for c in result["changed_conditions"]})
        self.assertEqual(result["attribution"]["code"], "not_attributable")

    def test_compare_period_change_marks_readings_not_comparable(self):
        a = self._dataset_run(label="甲", revenue=220_000_000, gross_profit=55_000_000)
        b = self._dataset_run(label="甲", revenue=220_000_000, gross_profit=55_000_000,
                              rules=RULES)
        same, errors = st.compare_runs(SCOPE, a["run_id"], a["run_id"])
        self.assertEqual(same, {})
        self.assertIn("不能和自己对比", errors[0])
        pinned, errors = self._run(period_policy_raw={"mode": "pinned", "report_period": "2023年"})
        self.assertEqual(errors, [])
        result, errors = st.compare_runs(SCOPE, a["run_id"], pinned["run_id"])
        self.assertEqual(errors, [])
        self.assertIn("period_policy", {c["key"] for c in result["changed_conditions"]})
        stayed = result["stayed"][0]
        self.assertFalse(stayed["comparable"])
        self.assertTrue(stayed["note"])
        self.assertEqual(result["summary"]["not_comparable"], 1)

    def test_compare_counts_insufficient_new_and_resolved_separately(self):
        growth_only = [{"field": "revenue_growth", "op": ">=", "value": 0}]
        two_years = self._dataset_run(label="两年可比", revenue=220_000_000,
                                      gross_profit=55_000_000, rules=growth_only)
        one_year = self._dataset_run(label="只有一年", revenue=220_000_000,
                                     gross_profit=55_000_000, rules=growth_only,
                                     single_period=True)
        result, errors = st.compare_runs(SCOPE, two_years["run_id"], one_year["run_id"])
        self.assertEqual(errors, [])
        stayed = result["stayed"][0]
        self.assertEqual(stayed["base_verdict"], "pass")
        self.assertEqual(stayed["other_verdict"], "insufficient")
        self.assertEqual(result["summary"]["insufficient_new"], 1)
        self.assertEqual(result["summary"]["insufficient_resolved"], 0)
        # 反过来看就是「资料不足被补齐」，两者不共用一个计数
        back, errors = st.compare_runs(SCOPE, one_year["run_id"], two_years["run_id"])
        self.assertEqual(errors, [])
        self.assertEqual(back["summary"]["insufficient_new"], 0)
        self.assertEqual(back["summary"]["insufficient_resolved"], 1)

    def test_compare_refuses_unknown_corrupt_and_cross_scope(self):
        a = self._dataset_run(label="甲", revenue=220_000_000, gross_profit=55_000_000)
        _result, errors = st.compare_runs(SCOPE, a["run_id"], "0" * 32)
        self.assertTrue(errors)
        self.assertIn("不存在", errors[0])
        # 损坏的第二份不得参与对比
        b = self._dataset_run(label="乙", revenue=220_000_000, gross_profit=44_000_000)
        path = os.path.join(self._run_path(b["run_id"]), "run.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"run_id": b["run_id"], "tampered": True}, fh)
        _result, errors = st.compare_runs(SCOPE, a["run_id"], b["run_id"])
        self.assertTrue(errors)
        self.assertIn("不可用于对比", errors[0])
        # 跨作用域：别人看不到这份结果
        _result, errors = st.compare_runs(SCOPE_B, a["run_id"], a["run_id"])
        self.assertTrue(errors)
