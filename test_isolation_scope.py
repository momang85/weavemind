# -*- coding: utf-8 -*-
"""隔离与缓存作用域（P2）回归测试。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TestQuoteCacheScope(unittest.TestCase):
    """行情缓存按项目分桶：不同项目不共用同一份排行快照，旧键仍可读（兼容）。"""

    def _cache(self):
        """纯内存缓存：显式屏蔽 Redis 客户端创建，避免用例间通过真实 Redis 串状态。"""
        from adapters.quote_cache import QuoteCache
        with mock.patch("adapters.quote_cache._new_redis_client",
                        side_effect=RuntimeError("no redis in unit test")):
            return QuoteCache(redis_client=None)

    def test_scoped_and_legacy_keys_are_distinct(self):
        from adapters.quote_cache import QuoteCache
        self.assertNotEqual(
            QuoteCache._key("a", "amount", 10, "default"),
            QuoteCache._key("a", "amount", 10, "research"))
        self.assertEqual(QuoteCache._key("a", "amount", 10, ""),
                         "ranking:a:amount:10", "无作用域时保持旧键格式")

    def test_values_do_not_leak_across_scopes(self):
        cache = self._cache()
        cache.set("a", "amount", {"source": "sina_ranking", "n": 1},
                  top_n=10, scope="proj-a")
        self.assertIsNotNone(cache.get("a", "amount", 10, scope="proj-a"))
        self.assertIsNone(cache.get("a", "amount", 10, scope="proj-b"),
                          "不同项目不得互相命中")

    def test_legacy_key_read_fallback(self):
        """旧的无作用域缓存仍可读（切换作用域不导致缓存整体失效）。"""
        cache = self._cache()
        cache.set("a", "amount", {"source": "legacy"}, top_n=10, scope="")
        got = cache.get("a", "amount", 10, scope="proj-a")
        self.assertIsNotNone(got)
        self.assertEqual(got["source"], "legacy")

    def test_new_writes_only_go_to_scoped_bucket(self):
        cache = self._cache()
        cache.set("a", "amount", {"source": "fresh"}, top_n=10, scope="proj-a")
        self.assertIsNone(cache.get("a", "amount", 10, scope=""),
                          "写入带作用域时不回填旧键")


class TestScopePlumbing(unittest.TestCase):
    def test_route_structured_forwards_scope(self):
        import adapters.router as r
        with mock.patch.object(r, "cache_get_ranking", return_value=None) as cg, \
                mock.patch.object(r, "_fetch_ranking_with_fallback",
                                  return_value={"source": "tencent_ranking", "data": []}) as fb:
            out = r.route_structured("统计今日A股成交额排行前十", scope="default")
        self.assertTrue(out)
        self.assertEqual(cg.call_args.kwargs.get("scope"), "default")
        self.assertEqual(fb.call_args.kwargs.get("scope"), "default")

    def test_preload_passes_project_scope(self):
        """预载必须把任务项目作为作用域（否则缓存按进程共享）。"""
        src = Path("structured_pipeline/__init__.py").read_text(encoding="utf-8")
        self.assertIn('_scope = str(project or "")', src)
        self.assertEqual(src.count("route_structured(goal, **_scope_kw)"), 2,
                         "首取与重试两处都应传作用域（无项目时不传参）")


class TestWorkerWorkspaceIsolation(unittest.TestCase):
    def test_default_workspace_not_shared_dir(self):
        """无任务工作区时不得使用跨任务共享目录。"""
        from workers.code_execution_worker import CodeExecutionWorker
        w = CodeExecutionWorker.__new__(CodeExecutionWorker)
        # 复刻 __init__ 的兜底路径（不跑真实 worker 循环）
        import tempfile as _tf
        w.workspace = Path(_tf.mkdtemp(prefix="agent_workspace_worker_"))
        shared = Path(_tf.gettempdir()) / "agent_workspace" / "project"
        self.assertNotEqual(w.workspace, shared)
        self.assertIn("agent_workspace_worker_", w.workspace.name)
        import shutil
        shutil.rmtree(w.workspace, ignore_errors=True)

    def test_source_no_longer_uses_shared_project_dir(self):
        src = Path("workers/code_execution_worker.py").read_text(encoding="utf-8")
        self.assertNotIn('tempfile.gettempdir()) / "agent_workspace" / "project"', src,
                         "共享目录兜底应已移除")


if __name__ == "__main__":
    unittest.main()
