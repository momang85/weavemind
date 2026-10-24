# -*- coding: utf-8 -*-
"""BETA-CLOSE-1024 · C0：筛选存储一致性回归。

覆盖范围（独立编写，不复用 architect_probes.py 的受控交错探针——
那套 barrier 探针依赖旧实现的锁行为，与新实现会死锁；这里只测**对外行为**）：

1. 并发加清单不丢更新（多线程不同代码同时加，逐只存活）；
2. 并发改备注：最终值是某一次成功写入，文件始终可解析；
3. 乐观并发：过期 `if_updated_at` → `VersionConflict`（HTTP 409 语义），正确的通过；
4. 同一预设 id 并发保存：终值唯一、created_at 保留；
5. 成本账本并发累加：终值 == 增量之和；
6. 账本损坏：record_cost 拒绝并保留原字节；rebuild 拒绝隐式自愈、confirm 后归档重建；
7. 清单文件损坏：mutate 拒绝写入、原字节保留；
8. `_write_one`：瞬时占用重试成功；持续失败 → StoreWriteError（明确未写入）；
9. 跨进程单写者：第二个写者被拒绝（子进程实测），持有者信息可见。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.abspath(__file__))   # 仓库根 = 本测试文件所在目录
sys.path.insert(0, REPO)

# RC2-B：测试目录由本脚本创建（mkdtemp），_wipe() 只清理**该目录**，
# 不再继承任意 WEAVEMIND_SCREEN_DIR 后递归删除（防误删真实数据）。
os.environ.setdefault("WEAVEMIND_TEST_MODE", "1")
TEST_ROOT = os.environ.setdefault(
    "WEAVEMIND_SCREEN_DIR", os.path.join(tempfile.mkdtemp(prefix="weavemind-c0-consistency-"),
                                         "screen"))
import screen_store as st  # noqa: E402

SCOPE_A = {"user": "c0a", "project": "personal"}
SCOPE_B = {"user": "c0b", "project": "personal"}


def _wipe():
    base = os.path.abspath(st.screen_base())
    if not base.startswith(os.path.abspath(TEST_ROOT)):
        raise RuntimeError(f"拒绝清理非本测试目录：{base}")
    if os.path.isdir(base):
        shutil.rmtree(base, ignore_errors=True)
    st._LOCKS.clear()
    handle = st._WRITER_LOCK_FH
    st._WRITER_LOCK_FH = None
    st._WRITER_INFO = {}
    if handle is not None:
        try:
            handle.close()                     # 释放上一个用例的跨进程写者声明
        except OSError:
            pass


class WatchlistConcurrency(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_concurrent_adds_no_lost_updates(self):
        """两个线程同时对同一作用域加不同代码：两只都必须存活（旧实现丢一只）。"""
        codes = ["002304.SZ", "600031.SH"]
        errors: list[str] = []

        def add(code: str):
            for _ in range(10):
                _data, errs = st.watchlist_mutate(scope=SCOPE_A, action="add", code=code)
                if not errs:
                    return
                errors.extend(errs)
        threads = [threading.Thread(target=add, args=(c,)) for c in codes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        final, load_errs = st.load_watchlist(SCOPE_A)
        self.assertEqual(load_errs, [])
        got = sorted(i["code"] for i in final["items"])
        self.assertEqual(got, sorted(codes), f"并发加清单丢了更新：{got}")

    def test_concurrent_note_writes_final_state_consistent(self):
        """两个线程同时改同一只的备注：最终备注是某一次成功写入，清单可解析。"""
        st.watchlist_mutate(scope=SCOPE_A, action="add", code="600519.SH", note="base")
        outcomes: list[str] = []
        lock = threading.Lock()

        def set_note(note: str):
            _data, errs = st.watchlist_mutate(scope=SCOPE_A, action="note",
                                              code="600519.SH", note=note)
            if not errs:
                with lock:
                    outcomes.append(note)
        t1 = threading.Thread(target=set_note, args=("甲写的备注",))
        t2 = threading.Thread(target=set_note, args=("乙写的备注",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(outcomes), 2, "两次并发写必须都成功（锁内串行），不该静默丢一次")
        final, _ = st.load_watchlist(SCOPE_A)
        self.assertEqual(len(final["items"]), 1)
        self.assertIn(final["items"][0]["note"], outcomes)
        # rev 与内容一致：读到什么就是存了什么
        self.assertEqual(final["rev"], st._watchlist_rev(final["items"]))

    def test_stale_version_conflicts_and_current_version_passes(self):
        """过期版本 → VersionConflict（409 语义）；带当前版本写入成功。"""
        d1, _ = st.watchlist_mutate(scope=SCOPE_A, action="add",
                                    code="600519.SH", name="贵州茅台", note="v1")
        seen_version = d1["items"][0]["version"]
        self.assertEqual(seen_version, 1)
        st.watchlist_mutate(scope=SCOPE_A, action="note", code="600519.SH", note="v2")
        with self.assertRaises(st.VersionConflict) as ctx:
            st.watchlist_mutate(scope=SCOPE_A, action="note", code="600519.SH",
                                note="v3", if_version=seen_version)
        self.assertIn("600519.SH", str(ctx.exception))
        self.assertEqual(ctx.exception.current.get("note"), "v2")
        self.assertEqual(ctx.exception.current.get("version"), 2)
        # 拒绝后内容没被覆盖
        cur, _ = st.load_watchlist(SCOPE_A)
        self.assertEqual(cur["items"][0]["note"], "v2")
        # 带当前版本写入成功
        d2, errs = st.watchlist_mutate(scope=SCOPE_A, action="note", code="600519.SH",
                                       note="v3", if_version=cur["items"][0]["version"])
        self.assertEqual(errs, [])
        self.assertEqual(d2["items"][0]["note"], "v3")
        self.assertEqual(d2["items"][0]["version"], 3)

    def test_corrupt_watchlist_file_refuses_write_and_preserves_bytes(self):
        """清单文件损坏：mutate 拒绝写入、原字节保留（不覆盖成空清单）。"""
        root = st.watchlist_root(SCOPE_A)
        os.makedirs(root, exist_ok=True)
        original = b'{"schema": "weavemind.stock_screen.watchlist/1", "items": [ broken'
        path = os.path.join(root, "watchlist.json")
        with open(path, "wb") as fh:
            fh.write(original)
        data, errs = st.watchlist_mutate(scope=SCOPE_A, action="add", code="600519.SH")
        self.assertEqual(data, None)
        self.assertTrue(errs and "损坏" in errs[0], errs)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), original, "损坏文件字节被改动")
        loaded, load_errs = st.load_watchlist(SCOPE_A)
        self.assertEqual(loaded.get("integrity"), "corrupt")
        self.assertTrue(load_errs)


class PresetAndCostConsistency(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_concurrent_same_preset_id_save_unique_result(self):
        """两个线程同时保存同一预设 id：终值唯一、created_at 保留、文件可解析。"""
        created, create_errs = st.save_preset(scope=SCOPE_A, name="预设原始", note="原始",
                                             pool=["002304.SZ"],
                                             rules_raw=[{"field": "gross_margin",
                                                         "op": ">=", "value": "70"}])
        self.assertEqual(create_errs, [])
        pid = created["preset_id"]
        results: list[dict] = []
        failures: list[list[str]] = []
        rules_raw = [{"field": "gross_margin", "op": ">=", "value": "70"}]
        lock = threading.Lock()

        def save(note: str):
            preset, errs = st.save_preset(scope=SCOPE_A, name=f"预设{note}", note=note,
                                          pool=["002304.SZ"], rules_raw=rules_raw,
                                          preset_id=pid)
            with lock:
                if errs:
                    failures.append(errs)
                else:
                    results.append(preset)
        t1 = threading.Thread(target=save, args=("甲",))
        t2 = threading.Thread(target=save, args=("乙",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        created = {p["created_at"] for p in results}
        self.assertEqual(len(created), 1, "并发保存同一 id：created_at 必须保留第一次的")
        loaded = st.load_preset(SCOPE_A, pid)
        self.assertIsNotNone(loaded)
        self.assertIn(loaded["note"], ("甲", "乙"))
        self.assertEqual(loaded["created_at"], results[0]["created_at"])

    def test_concurrent_record_cost_sums_all_deltas(self):
        """两个线程并发记账：终值 == 增量之和（旧实现两只各记 1 只剩 1）。"""
        st.record_cost(SCOPE_A, prepare_requests=1)
        results: list[dict] = []
        lock = threading.Lock()

        def add_runs(**deltas):
            out = st.record_cost(SCOPE_A, **deltas)
            with lock:
                results.append(out)
        t1 = threading.Thread(target=add_runs, kwargs={"runs_total": 1, "compute_runs": 1})
        t2 = threading.Thread(target=add_runs, kwargs={"runs_total": 1, "cache_hits": 1})
        t1.start(); t2.start(); t1.join(); t2.join()
        final, _ = st.load_cost(SCOPE_A)
        self.assertEqual(final["runs_total"], 2, f"并发记账丢增量：{final['runs_total']}")
        self.assertEqual(final["compute_runs"], 1)
        self.assertEqual(final["cache_hits"], 1)
        self.assertEqual(final["prepare_requests"], 1)
        self.assertTrue(all(r["recorded"] for r in results))

    def test_cost_corrupt_ledger_preserved_and_rebuild_explicit(self):
        """账本损坏：record_cost 明确未记录、原字节保留；rebuild 需显式确认且先归档。"""
        root = st.cost_root(SCOPE_A)
        os.makedirs(root, exist_ok=True)
        original = b'{"runs_total":7, invalid bytes'
        path = os.path.join(root, "ledger.json")
        with open(path, "wb") as fh:
            fh.write(original)
        out = st.record_cost(SCOPE_A, runs_total=1)
        self.assertFalse(out["recorded"])
        self.assertEqual(out["recording"]["reason"], "ledger_corrupt")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), original, "损坏账本字节被覆盖")
        # 读取侧如实给出损坏状态
        loaded, errs = st.load_cost(SCOPE_A)
        self.assertEqual(loaded["integrity"], "corrupt")
        self.assertTrue(errs)
        # 未确认 → 拒绝；确认 → 归档后重建
        refused, refuse_errs = st.rebuild_cost(SCOPE_A)
        self.assertIsNone(refused)
        self.assertTrue(refuse_errs)
        ok, _ = st.rebuild_cost(SCOPE_A, confirm=True)
        self.assertEqual(ok["status"], "ok")
        self.assertTrue(os.path.isfile(ok["archived"]), "坏字节必须归档保留")
        with open(ok["archived"], "rb") as fh:
            self.assertEqual(fh.read(), original)
        fresh, _ = st.load_cost(SCOPE_A)
        self.assertEqual(fresh["integrity"], "ok")
        self.assertEqual(fresh["runs_total"], 0, "新账本从 0 开始，不凭空补旧数字")

    def test_cost_counters_distinguish_denied_vs_attempts(self):
        """被策略拒绝与真正发起连接分开记；未知费用不冒充 0。"""
        st.record_cost(SCOPE_A, network_attempts=35, network_denied=35,
                       network_requests=0, network_ok=0, network_failed=0)
        cost, _ = st.load_cost(SCOPE_A)
        self.assertEqual(cost["network_attempts"], 35)
        self.assertEqual(cost["network_denied"], 35)
        self.assertEqual(cost["network_requests"], 0)
        self.assertEqual(cost["network_ok"], 0)
        self.assertEqual(cost["external_fee"], "unknown")
        st.record_cost(SCOPE_B, network_attempts=2, network_ok=2)
        cost_b, _ = st.load_cost(SCOPE_B)
        self.assertEqual(cost_b["network_attempts"], 2)
        self.assertEqual(cost_b["network_denied"], 0)


class WriteAtomicity(unittest.TestCase):
    def setUp(self):
        _wipe()
        os.makedirs(st.screen_base(), exist_ok=True)

    def test_write_one_retries_transient_replace_failure(self):
        """os.replace 瞬时失败（占用）：退避重试后成功，不留临时文件。"""
        import screen_store
        target = st.screen_base()
        real_replace = os.replace
        state = {"calls": 0}
        lock = threading.Lock()

        def flaky_replace(src, dst):
            with lock:
                state["calls"] += 1
                if state["calls"] <= 2:
                    raise PermissionError(32, "另一个程序正在使用此文件")
            return real_replace(src, dst)
        try:
            with mock.patch("os.replace", flaky_replace):
                screen_store._write_one(target, "probe.json", {"ok": True})
        finally:
            os.replace = real_replace
        self.assertGreaterEqual(state["calls"], 3)
        with open(os.path.join(target, "probe.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"ok": True})
        leftovers = [n for n in os.listdir(target) if n.startswith(".probe.json.")]
        self.assertEqual(leftovers, [], "重试成功后不留临时文件")

    def test_write_one_persistent_failure_raises_store_write_error(self):
        """os.replace 持续失败：StoreWriteError（明确未写入），不留目标文件。"""
        import screen_store
        target = st.screen_base()
        real_replace = os.replace

        def always_fail(src, dst):
            raise PermissionError(32, "另一个程序正在使用此文件")
        try:
            with mock.patch("os.replace", always_fail):
                with self.assertRaises(screen_store.StoreWriteError) as ctx:
                    screen_store._write_one(target, "probe.json", {"ok": True})
        finally:
            os.replace = real_replace
        self.assertIn("未写入", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(target, "probe.json")))

    def test_watchlist_write_failure_reports_not_written(self):
        """清单写入失败：返回错误并明确「未写入」，不假装成功。"""
        real_replace = os.replace

        def always_fail(src, dst):
            raise PermissionError(32, "占用")
        try:
            with mock.patch("os.replace", always_fail):
                data, errs = st.watchlist_mutate(scope=SCOPE_A, action="add", code="600519.SH")
        finally:
            os.replace = real_replace
        self.assertIsNone(data)
        self.assertTrue(errs and "未写入" in errs[0], errs)
        loaded, _ = st.load_watchlist(SCOPE_A)
        self.assertEqual(loaded["items"], [])


class SingleWriterClaim(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_second_process_writer_refused(self):
        """跨进程：第二个写者被可靠拒绝（子进程实测），持有者信息可见。"""
        ok, info = st.claim_writer()
        self.assertTrue(ok, info)
        self.assertEqual(info["pid"], os.getpid())
        holder_path = st.writer_info_path()
        self.assertTrue(os.path.isfile(holder_path))
        with open(holder_path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["pid"], os.getpid())

        probe = (
            "import json, os, sys\n"
            "sys.path.insert(0, %r)\n"
            "os.environ['WEAVEMIND_TEST_MODE'] = '1'\n"
            "import screen_store as st\n"
            "ok, info = st.claim_writer()\n"
            "print(json.dumps({'ok': ok, 'error': info.get('error', ''),\n"
            "                  'holder_pid': (info.get('holder') or {}).get('pid')},\n"
            "                 ensure_ascii=False))\n"
        ) % REPO
        env = dict(os.environ)
        env["WEAVEMIND_SCREEN_DIR"] = st.screen_base()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["ok"], "第二写者必须被拒绝")
        self.assertIn("已有写入进程", payload["error"])
        self.assertEqual(payload["holder_pid"], os.getpid())

        # 同进程重复声明：返回已持有
        ok2, info2 = st.claim_writer()
        self.assertTrue(ok2)
        self.assertEqual(info2["pid"], os.getpid())
        status = st.writer_status()
        self.assertTrue(status["claimed_by_this_process"])

    def test_writer_claim_respects_explicit_base(self):
        """显式 base 的声明独立于默认目录（隔离实例各自持锁）。"""
        base_a = os.path.join(st.screen_base(), "iso-a")
        base_b = os.path.join(st.screen_base(), "iso-b")
        ok_a, _ = st.claim_writer(base_a)
        ok_b, _ = st.claim_writer(base_b)
        self.assertTrue(ok_a)
        self.assertTrue(ok_b, "不同目录的实例互不干扰")


if __name__ == "__main__":
    unittest.main(verbosity=2)
