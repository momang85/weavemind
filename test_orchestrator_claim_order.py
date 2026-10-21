"""F3（收口 20261021）：编排器先认领，再处理共享结果；认领失败零删除副作用。

实机风险：`main()` 原先把「清理残留 `task_result:*`」放在实例认领**之前**。
第二个编排器进程被拉起时，会先把活跃实例正在等待的结果键删光，然后才去认领、
认领失败退出——活跃实例的 `brpop task_result:*` 从此等不到结果，在飞任务只能超时。

测试用假 Redis 替身（只实现 keys/delete/set/get/…），**不接触共享 Redis**，
不启动第二套栈，不杀任何进程。
"""
import unittest
from unittest.mock import patch

import orchestrator_v2 as ov


class FakeRedis:
    """最小 Redis 替身：键值 + 命中计数，用于断言"有没有删过东西"。"""

    def __init__(self, keys: list[str] | None = None):
        self.store: dict[str, str] = {k: "1" for k in (keys or [])}
        self.deleted: list[str] = []

    # --- 共享结果键相关 ---
    def keys(self, pattern: str = "*"):
        import fnmatch
        return [k for k in self.store if fnmatch.fnmatch(k, pattern)]

    def delete(self, *names):
        n = 0
        for k in names:
            if k in self.store:
                del self.store[k]
                self.deleted.append(k)
                n += 1
        return n

    # --- 认领机制可能用到的 ---
    def set(self, k, v, ex=None, nx=False, **kw):
        if nx and k in self.store:
            return None
        self.store[k] = str(v)
        return True

    def get(self, k):
        v = self.store.get(k)
        return v.encode() if isinstance(v, str) else v

    def exists(self, k):
        return 1 if k in self.store else 0

    def expire(self, *a, **k):
        return True

    def ttl(self, k):
        return -1

    def ping(self):
        return True


class TestClaimBeforeCleanup(unittest.TestCase):
    def test_cleanup_does_not_delete_when_not_called(self):
        """认领失败路径：什么都不该删（`_cleared_stale_results` 是唯一删除入口）。"""
        r = FakeRedis(["task_result:a", "task_result:b", "other"])
        with patch.object(ov, "claim_ownership_with_wait",
                          return_value=(False, "已有活跃实例 999")), \
             patch.object(ov.OrchestratorV2, "_new_redis_sync",
                          return_value=r), \
             patch.object(ov, "OrchestratorV2") as _cls:
            rc = ov.main()
        self.assertEqual(rc, 2)
        _cls.assert_not_called()                 # 未认领 → 连实例都不构造
        self.assertEqual(r.deleted, [])          # 零删除副作用

    def test_cleanup_runs_only_after_successful_claim(self):
        """认领成功后：清理入口可用，且确实清掉残留结果键。"""
        r = FakeRedis(["task_result:a", "task_result:b", "keep"])
        n = ov._cleared_stale_results(r)
        self.assertEqual(n, 2)
        self.assertEqual(sorted(r.deleted), ["task_result:a", "task_result:b"])
        self.assertIn("keep", r.store)           # 非结果键不受影响

    def test_cleanup_is_idempotent_and_failure_tolerant(self):
        r = FakeRedis([])
        self.assertEqual(ov._cleared_stale_results(r), 0)

        class Broken(FakeRedis):
            def keys(self, pattern="*"):
                raise RuntimeError("redis down")

        self.assertEqual(ov._cleared_stale_results(Broken()), 0)   # 不抛、不挡启动

    def test_source_order_in_main_claim_precedes_cleanup(self):
        """回归守卫：`main()` 里认领必须出现在清理之前（防有人改回去）。"""
        import inspect
        src = inspect.getsource(ov.main)
        i_claim = src.find("claim_ownership_with_wait")
        i_clean = src.find("_cleared_stale_results")
        self.assertNotEqual(i_claim, -1)
        self.assertNotEqual(i_clean, -1)
        self.assertLess(i_claim, i_clean,
                        "认领必须早于任何共享结果键清理（F3）")
        # 不允许在 clean 之前出现裸 keys/delete 清理
        before = src[:i_clean]
        self.assertNotIn('keys("task_result:*")', before)


class TestNoSideEffectsBeforeClaim(unittest.TestCase):
    def test_rejected_instance_leaves_shared_state_untouched(self):
        """模拟第二实例全流程：认领失败 → 共享结果键原样健在。"""
        r = FakeRedis(["task_result:live-1", "task_result:live-2"])
        with patch.object(ov, "claim_ownership_with_wait",
                          return_value=(False, "活跃实例心跳新鲜，拒绝启动")), \
             patch.object(ov.OrchestratorV2, "_new_redis_sync",
                          return_value=r), \
             patch.object(ov, "_cleared_stale_results") as _clean:
            rc = ov.main()
        self.assertEqual(rc, 2)
        _clean.assert_not_called()
        self.assertEqual(sorted(r.store), ["task_result:live-1", "task_result:live-2"])


if __name__ == "__main__":
    unittest.main()
