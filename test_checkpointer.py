"""织光 - checkpointer 断点续跑回归测试（V1.2）。

覆盖：
a) save -> load 往返（字段完整，含 TTL）；
b) clear 后 load 返回 None；
c) Redis 不可用时 SQLite 兜底可读；
d) _resume_from_checkpoint：SUCCESS 跳过、FAILED 保留重试、phase/iteration 正确；
e) goal 哈希不一致的 checkpoint 被忽略；
f) run() 恢复路径：已完成步骤不再执行（计数），且不清空成果文件夹；
g) list_pending_checkpoints 扫描（年龄过滤 + 终态排除）。
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import fakeredis

import checkpointer as cp_mod
import orchestrator_v2
import workspace as ws_mod
from test_orchestrator_v2 import make_orch


class MemoryRedis:
    """run() 恢复路径用：可 get/set/delete 的内存 Redis 替身。"""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    def delete(self, key):
        self.data.pop(key, None)
        return 1


def _checkpoint(
    task_id="t1", goal="目标", steps=None, phase="executing", **extra
):
    steps = steps or [
        {
            "step_id": "1", "capability": "content_summary",
            "instruction": "第一步", "result": {"status": "SUCCESS", "result": "ok"},
        },
        {
            "step_id": "2", "capability": "content_summary",
            "instruction": "第二步", "result": {"status": "FAILED", "result": "bad"},
        },
    ]
    cp = {
        "task_id": task_id,
        "goal": goal,
        "goal_hash": cp_mod.goal_hash(goal),
        "project": "default",
        "steps": steps,
        "completed_all": {
            s["step_id"]: s["result"] for s in steps
        },
        "current_steps": steps,
        "pending_steps": [
            s for s in steps
            if s["result"].get("status") != "SUCCESS"
        ],
        "phase": phase,
        "iteration": 0,
        "has_failure": True,
        "redo_rounds": 0,
        "best_report": "",
        "gate_checked": False,
        "simple": False,
        "used_template": False,
        "status": "RUNNING",
        "saved_at": cp_mod._now_iso(),
    }
    cp.update(extra)
    return cp


class TestCheckpointerStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wm_cp_")
        self._db = os.path.join(self._tmp, "agents.db")
        self._redis = fakeredis.FakeRedis(decode_responses=True)
        self._patch_db = mock.patch.object(
            cp_mod, "_db_path", return_value=self._db
        )
        self._patch_redis = mock.patch.object(
            cp_mod, "_get_redis", return_value=self._redis
        )
        self._patch_db.start()
        self._patch_redis.start()
        self.addCleanup(self._patch_db.stop)
        self.addCleanup(self._patch_redis.stop)
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_save_load_roundtrip_preserves_fields(self):
        """a) save -> load 往返：字段完整、Redis 带 TTL。"""
        cp = _checkpoint()
        cp_mod.save_checkpoint("t1", cp)
        loaded = cp_mod.load_checkpoint("t1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["task_id"], "t1")
        self.assertEqual(loaded["goal"], "目标")
        self.assertEqual(loaded["project"], "default")
        self.assertEqual(loaded["phase"], "executing")
        self.assertEqual(len(loaded["steps"]), 2)
        self.assertEqual(
            loaded["steps"][0]["result"]["status"], "SUCCESS"
        )
        self.assertEqual(loaded["steps"][1]["result"]["status"], "FAILED")
        self.assertEqual(loaded["completed_all"]["1"]["status"], "SUCCESS")
        self.assertEqual(loaded["completed_all"]["2"]["status"], "FAILED")
        self.assertEqual(loaded["goal_hash"], cp_mod.goal_hash("目标"))
        self.assertTrue(loaded.get("saved_at"))
        self.assertEqual(
            [s["step_id"] for s in loaded["pending_steps"]], ["2"],
        )
        ttl = self._redis.ttl("checkpoint:t1")
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, cp_mod.CHECKPOINT_TTL)

    def test_clear_returns_none(self):
        """b) clear 后 load 返回 None。"""
        cp_mod.save_checkpoint("t-clear", _checkpoint(task_id="t-clear"))
        self.assertIsNotNone(cp_mod.load_checkpoint("t-clear"))
        cp_mod.clear_checkpoint("t-clear")
        self.assertIsNone(cp_mod.load_checkpoint("t-clear"))

    def test_sqlite_fallback_when_redis_down(self):
        """c) Redis 不可用时 SQLite 兜底仍可读。"""
        cp = _checkpoint(task_id="t-fb")
        with mock.patch.object(
            cp_mod, "_get_redis", side_effect=RuntimeError("redis down")
        ):
            cp_mod.save_checkpoint("t-fb", cp)
            loaded = cp_mod.load_checkpoint("t-fb")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["task_id"], "t-fb")
        self.assertEqual(loaded["goal"], "目标")
        self.assertEqual(len(loaded["steps"]), 2)

    def test_list_pending_checkpoints_filters_age_and_terminal(self):
        """g) 启动恢复扫描：新鲜 checkpoint 被年龄过滤，终态被排除。"""
        fresh = _checkpoint(task_id="t-fresh", saved_at=cp_mod._now_iso())
        stale = _checkpoint(
            task_id="t-stale",
            saved_at="2020-01-01T00:00:00+00:00",
        )
        terminal = _checkpoint(
            task_id="t-done", status="SUCCESS",
            saved_at="2020-01-01T00:00:00+00:00",
        )
        cp_mod.save_checkpoint("t-fresh", fresh)
        cp_mod.save_checkpoint("t-stale", stale)
        cp_mod.save_checkpoint("t-done", terminal)
        with mock.patch.object(cp_mod, "is_task_completed", return_value=False):
            ids = {c["task_id"] for c in cp_mod.list_pending_checkpoints(age_sec=3600)}
        self.assertNotIn("t-fresh", ids)
        self.assertIn("t-stale", ids)
        self.assertNotIn("t-done", ids)
        with mock.patch.object(cp_mod, "is_task_completed", return_value=False):
            ids0 = {c["task_id"] for c in cp_mod.list_pending_checkpoints(age_sec=0)}
        self.assertIn("t-fresh", ids0)
        self.assertIn("t-stale", ids0)
        self.assertNotIn("t-done", ids0)


class TestResumeFromCheckpoint(unittest.TestCase):
    def setUp(self):
        self.o = make_orch()

    def test_resume_skips_success_and_keeps_failed(self):
        """d) SUCCESS 步骤被跳过，FAILED 步骤保留待重试，phase/iteration 正确。"""
        cp = _checkpoint(phase="executing", iteration=2)
        with mock.patch.object(
            orchestrator_v2, "load_checkpoint", return_value=cp
        ), mock.patch.object(
            orchestrator_v2, "is_task_completed", return_value=False
        ):
            state = self.o._resume_from_checkpoint("t1", "目标", "default")
        self.assertIsNotNone(state)
        self.assertEqual([s["step_id"] for s in state["steps"]], ["2"])
        self.assertEqual(state["steps"][0]["result"]["status"], "FAILED")
        self.assertEqual(state["phase"], "executing")
        self.assertEqual(state["iteration"], 2)
        self.assertTrue(state["has_failure"])
        self.assertFalse(state["skip_execute"])
        # 待重试步骤从累积列表中摘出，避免执行后 all_steps 重复
        self.assertEqual(
            [s["step_id"] for s in state["all_steps"]], ["1"],
        )
        self.assertEqual(state["completed_all"]["2"]["status"], "FAILED")

    def test_resume_finalizing_has_no_pending(self):
        """finalizing 阶段恢复：无待执行步骤，skip_execute=True。"""
        cp = _checkpoint(phase="finalizing", pending_steps=[])
        with mock.patch.object(
            orchestrator_v2, "load_checkpoint", return_value=cp
        ), mock.patch.object(
            orchestrator_v2, "is_task_completed", return_value=False
        ):
            state = self.o._resume_from_checkpoint("t1", "目标", "default")
        self.assertIsNotNone(state)
        self.assertEqual(state["phase"], "finalizing")
        self.assertEqual(state["steps"], [])
        self.assertTrue(state["skip_execute"])

    def test_goal_hash_mismatch_ignored(self):
        """e) goal 哈希不一致的 checkpoint 被忽略。"""
        cp = _checkpoint(goal="旧目标")
        cp["goal_hash"] = cp_mod.goal_hash("旧目标")
        with mock.patch.object(
            orchestrator_v2, "load_checkpoint", return_value=cp
        ), mock.patch.object(
            orchestrator_v2, "is_task_completed", return_value=False
        ):
            state = self.o._resume_from_checkpoint(
                "t1", "新目标", "default",
            )
        self.assertIsNone(state)

    def test_completed_task_not_resumed(self):
        """终态任务（SUCCESS/FAILED）不得恢复。"""
        cp = _checkpoint(status="SUCCESS")
        with mock.patch.object(
            orchestrator_v2, "load_checkpoint", return_value=cp
        ):
            state = self.o._resume_from_checkpoint("t1", "目标", "default")
        self.assertIsNone(state)


class TestRunResumePath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wm_runres_")
        self._old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(self._tmp)
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", self._old_root)
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_run_resumes_and_skips_completed_steps_without_cleaning(self):
        """f) 构造 checkpoint -> run()：已完成步骤不再执行（计数），
        成果文件夹不被清理，最终交付成功。"""
        o = make_orch()
        o._redis = MemoryRedis()
        o._inject_memory_context = lambda goal, task_id: ""
        o._query_prompt_hints = lambda goal, task_id: []

        ws_dir = ws_mod.task_workspace("t-run-1", "default")
        ws_dir.mkdir(parents=True, exist_ok=True)
        keep = ws_dir / "keep_me.txt"
        keep.write_text("已有成果", encoding="utf-8")

        cp = _checkpoint(task_id="t-run-1", phase="executing")
        dispatched = []

        def fake_dispatch(goal, step, task_id, state):
            dispatched.append(step["step_id"])
            return {
                "task_id": step["step_id"],
                "status": "SUCCESS",
                "result": f"ok-{step['step_id']}",
            }

        o._dispatch_step_safe = fake_dispatch
        with mock.patch.object(
            orchestrator_v2, "load_checkpoint", return_value=cp
        ), mock.patch.object(
            orchestrator_v2, "is_task_completed", return_value=False
        ), mock.patch.object(
            orchestrator_v2, "save_checkpoint"
        ), mock.patch.object(
            orchestrator_v2, "clear_checkpoint"
        ):
            res = o.run("t-run-1", "目标", auto_run=True)

        self.assertEqual(dispatched, ["2"], "SUCCESS 步骤 1 不应重新执行")
        self.assertEqual(res["status"], "FAILED")  # checkpoint 已含失败 → 保持
        self.assertTrue(keep.exists(), "恢复不得清空成果文件夹")
        self.assertEqual(keep.read_text(encoding="utf-8"), "已有成果")


if __name__ == "__main__":
    unittest.main(verbosity=2)
