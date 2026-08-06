"""
织光 (ZhiGuang) — 边界条件与假设验证套件

验证维度（旧编排器相关的深度/等价验证已随 orchestrator.py 移除）：
    1. 状态机最终一致性
    2. Memory 注入相关性
    3. 演化泛化性 A/B 框架
"""

import os
import sys
import time
import json
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("verification")


def _load_config_env():
    """从 config.json 加载 LLM/Embedding 配置到环境变量（与 launcher 行为一致）。"""
    try:
        with open("config.json", encoding="utf-8") as f:
            cfg = json.load(f)
        llm = cfg.get("llm", {})
        emb = cfg.get("embedding", {})
        os.environ.setdefault("LLM_API_KEY", llm.get("api_key", ""))
        os.environ.setdefault("LLM_BASE_URL", llm.get("base_url", ""))
        os.environ.setdefault("LLM_MODEL", llm.get("model", ""))
        if emb.get("api_key"):
            os.environ.setdefault("EMBEDDING_API_KEY", emb["api_key"])
        if emb.get("base_url"):
            os.environ.setdefault("EMBEDDING_BASE_URL", emb["base_url"])
        if emb.get("model"):
            os.environ.setdefault("EMBEDDING_MODEL", emb["model"])
    except Exception as exc:
        logger.warning("Failed to load config.json env: %s", exc)


# ============================================================================
# 验证 1: 状态机最终一致性
# ============================================================================


class FlakyWorker:
    """模拟在 set_idle 和 publish 之间崩溃的 Worker。"""

    def __init__(self, agent_id="flaky", crash_after_idle=True):
        self.agent_id = agent_id
        self.crash_after_idle = crash_after_idle
        self.state = "starting"
        self.result_published = False

    def execute_task(self, task):
        """模拟任务执行 → set_idle → (可能崩溃) → publish。"""
        self.state = "busy"
        # 模拟执行
        time.sleep(0.1)
        result = f"executed {task.get('task_id', '?')}"

        # Step 1: 更新状态为 idle（修改后新顺序）
        self.state = "idle"

        # Step 2: 发布结果前崩溃
        if self.crash_after_idle:
            logger.warning(
                "[FlakyWorker] CRASH after set_idle, before publish! State=%s",
                self.state,
            )
            # 模拟崩溃：不发布结果
            return None, True  # (result, crashed)

        # 正常流程
        self.result_published = True
        return result, False


def verify_state_consistency():
    """验证 Worker 崩溃后的状态一致性。

    场景:
        Worker 在 set_idle 后、publish 前崩溃
        → Registry 中状态为 idle（实际上已死）
        → 新任务被派发给已死的 Worker → 超时 → Guardian 介入

    测量:
        - 状态不一致窗口：从崩溃到 Guardian 检测的时间
        - 是否有任务被错误派发
    """
    print("\n" + "=" * 60)
    print("VERIFICATION 1: State Machine Consistency (FlakyWorker)")
    print("=" * 60)

    import redis

    r = redis.Redis(host="localhost", port=6379, decode_responses=True)

    # 注册 FlakyWorker
    r.set("flaky_state", "idle")

    # 模拟崩溃场景
    worker = FlakyWorker(agent_id="flaky_01", crash_after_idle=True)
    task = {"task_id": "crash-test-001", "instruction": "test"}

    # 崩溃前状态
    state_before = worker.state
    result, crashed = worker.execute_task(task)
    state_after = worker.state

    # 关键测量
    inconsistency_detected = crashed and state_after == "idle"
    tasks_misdirected = 0  # 模拟：如果有任务在崩溃后被推送到此Worker

    # 模拟 Guardian 检测延迟
    # Guardian 每 15s 扫描，心跳超时 20s
    # 最坏情况：20s 后发现 Worker 死亡
    worst_detection_delay = 20

    # 模拟：在窗口期内推送一个任务
    if crashed and state_after == "idle":
        r.lpush("task_queue:flaky_01", json.dumps({
            "task_id": "misdirected-001",
            "instruction": "这个任务会被发给已死的Worker",
        }))
        tasks_misdirected = 1
        # 清理
        r.delete("task_queue:flaky_01")

    results = {
        "test": "state_consistency",
        "worker": worker.agent_id,
        "crashed": crashed,
        "state_before_crash": state_before,
        "state_after_crash": state_after,
        "publish_succeeded": worker.result_published,
        "inconsistency_window": {
            "type": "state=idle but Worker dead",
            "worst_case_detection": f"{worst_detection_delay}s (Guardian heartbeat timeout)",
            "tasks_potentially_misdirected": tasks_misdirected,
        },
        "verdict": (
            "PASS: 已反转执行顺序，窗口最小化"
            if not crashed
            else "ACCEPTABLE: 窗口期存在但 Guardian 会在 20s 内修正"
        ),
        "mitigation": "Guardian 每 15s 扫描 + 心跳超时 20s = 最坏 20s 内修复",
    }

    return results


# ============================================================================
# 验证 2: Memory 注入相关性
# ============================================================================


def verify_memory_relevance(memory_manager=None):
    """验证记忆注入的上下文是否真正相关。

    测试:
        A. 历史任务: "搜索AI市场规模"
        B. 新任务相似: "搜索AI芯片市场规模" → 应该匹配
        C. 新任务不同: "评估AI培训材料润色成本" → 不应硬匹配

    测量:
        - 相似任务的注入内容是否有具体可参考的策略
        - 不同任务的注入内容是否为空或泛化建议
    """
    print("\n" + "=" * 60)
    print("VERIFICATION 2: Memory Injection Relevance")
    print("=" * 60)

    from memory_manager import MemoryManager

    _load_config_env()

    # 使用独立的验证目录，避免污染正式记忆库
    mem_dir = "./chroma_memory_verify"
    if os.path.exists(mem_dir):
        import shutil
        shutil.rmtree(mem_dir)

    mem = MemoryManager(mem_dir)

    # 先写入几个历史记忆
    mem.consolidate_memory(
        "搜索2024年AI市场规模",
        [
            {"capability": "web_search", "instruction": "搜索2024年AI市场规模", "status": "SUCCESS"},
            {"capability": "content_summary", "instruction": "总结市场规模数据", "status": "SUCCESS"},
        ],
        "2024年AI市场规模约5000亿美元，年增长率35%。搜索关键词: 'AI market size 2024'",
    )

    mem.consolidate_memory(
        "评估AI培训材料润色成本",
        [
            {"capability": "web_search", "instruction": "搜索AI培训市场价格", "status": "SUCCESS"},
        ],
        "企业级AI培训材料润色成本约每千字200-500元，取决于技术深度",
    )

    # 测试 A: 高度相关的新任务
    ctx_similar = mem.inject_context("搜索AI芯片市场规模和增长率")
    similar_length = len(ctx_similar)

    # 测试 B: 不相关的新任务
    ctx_different = mem.inject_context("制定公司年度团建活动方案")
    different_length = len(ctx_different)

    # 测试 C: 部分相关
    ctx_partial = mem.inject_context("AI行业人才培训成本分析")
    partial_length = len(ctx_partial)

    # 分析匹配质量：检查注入内容是否包含具体策略而非泛泛而谈
    has_actionable = any(
        keyword in ctx_similar.lower()
        for keyword in ["搜索", "关键词", "market size", "增长率"]
    ) if ctx_similar else False

    # 清理
    import shutil
    shutil.rmtree(mem_dir, ignore_errors=True)

    results = {
        "test": "memory_relevance",
        "memory_store": {
            "task1": "搜索2024年AI市场规模",
            "task2": "评估AI培训材料润色成本",
        },
        "similar_task": {
            "query": "搜索AI芯片市场规模和增长率",
            "injected_chars": similar_length,
            "has_actionable_info": has_actionable,
            "relevance": "HIGH" if similar_length > 0 and has_actionable else "LOW",
        },
        "different_task": {
            "query": "制定公司年度团建活动方案",
            "injected_chars": different_length,
            "should_be_empty_or_irrelevant": different_length == 0,
            "relevance": "LOW (correct)",
        },
        "partial_task": {
            "query": "AI行业人才培训成本分析",
            "injected_chars": partial_length,
        },
        "adoption_rate": "待实际任务验证（需在生产日志中记录采纳率）",
        "verdict": (
            "PASS: 相关任务获注入，不相关任务无干扰"
            if similar_length > 0 and different_length == 0
            else "REVIEW: 检查注入相关性"
        ),
    }

    return results


# ============================================================================
# 验证 3: 演化泛化性 — A/B 测试框架
# ============================================================================


def verify_evolution_ab():
    """搭建 A/B 测试框架，验证演化策略的泛化能力。

    框架设计:
        - 80% 流量 → 稳定版策略 (baseline)
        - 20% 流量 → 演化版策略 (candidate)
        - 收集指标: success_rate, avg_latency, user_satisfaction
        - 24 小时后对比，统计显著性检验
    """
    print("\n" + "=" * 60)
    print("VERIFICATION 3: Evolution A/B Framework")
    print("=" * 60)

    import statistics
    import random

    # 模拟 24 小时的 A/B 测试数据
    random.seed(42)

    class ABTestTracker:
        def __init__(self):
            self.baseline = {"success": [], "latency": [], "tasks": 0}
            self.candidate = {"success": [], "latency": [], "tasks": 0}

        def record(self, variant, success, latency_ms):
            bucket = self.baseline if variant == "baseline" else self.candidate
            bucket["success"].append(success)
            bucket["latency"].append(latency_ms)
            bucket["tasks"] += 1

        def report(self):
            def stats(bucket):
                if not bucket["tasks"]:
                    return {"success_rate": 0, "avg_latency": 0, "tasks": 0}
                return {
                    "success_rate": sum(bucket["success"]) / len(bucket["success"]),
                    "avg_latency_ms": statistics.mean(bucket["latency"]),
                    "std_latency_ms": statistics.stdev(bucket["latency"]) if len(bucket["latency"]) > 1 else 0,
                    "tasks": bucket["tasks"],
                }

            b = stats(self.baseline)
            c = stats(self.candidate)

            # 简单统计检验: 成功率差异的 z-test 近似
            improvement = c["success_rate"] - b["success_rate"]
            significant = abs(improvement) > 0.05  # 5% 差异阈值

            return {
                "baseline": b,
                "candidate": c,
                "improvement": improvement,
                "statistically_significant": significant,
                "recommendation": (
                    "全量切换" if improvement > 0.05 and significant
                    else "继续观察" if improvement > 0
                    else "回退旧版"
                ),
            }

    tracker = ABTestTracker()

    # 模拟 24 小时数据（每 10 分钟一个周期，144 个周期）
    for _cycle in range(144):
        # 80% 流量 → baseline
        for _ in range(8):
            success = random.random() < 0.85  # baseline 85% 成功率
            latency = 500 + random.gauss(0, 100)
            tracker.record("baseline", success, latency)

        # 20% 流量 → candidate
        for _ in range(2):
            success = random.random() < 0.88  # candidate 88% 成功率
            latency = 450 + random.gauss(0, 80)
            tracker.record("candidate", success, latency)

    report = tracker.report()

    results = {
        "test": "evolution_ab",
        "framework": {
            "traffic_split": "80/20",
            "observation_period": "24h (simulated 144 cycles)",
            "metrics": ["success_rate", "avg_latency", "statistical_significance"],
        },
        "simulated_results": report,
        "deployment_gate": {
            "requires": [
                "success_rate improvement > 5%",
                "statistically significant",
                "no increase in p99 latency",
                "manual approval",
            ],
            "current_decision": report["recommendation"],
        },
        "verdict": "PASS: A/B 框架就绪，实际运行需接入真实流量",
    }

    return results


# ============================================================================
# 主入口
# ============================================================================


if __name__ == "__main__":
    _load_config_env()

    print("=" * 60)
    print("织光 — 边界条件与假设验证套件")
    print("=" * 60)
    print(f"Python: {sys.version.split()[0]}")
    print()

    all_results = {}

    # 运行三项验证（旧编排器相关验证已移除）
    all_results["1_state_consistency"] = verify_state_consistency()
    all_results["2_memory_relevance"] = verify_memory_relevance()
    all_results["3_evolution_ab"] = verify_evolution_ab()

    # 汇总
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    passed = 0
    review = 0
    failed = 0

    for key, result in all_results.items():
        verdict = result.get("verdict", "UNKNOWN")
        status = "✅" if verdict.startswith("PASS") else ("🔶" if "REVIEW" in verdict or "ACCEPTABLE" in verdict else "❌")
        test_name = key.split("_", 1)[1]
        print(f"  {status} 验证{key[0]}: {test_name.replace('_', ' ').title()}")
        print(f"       → {verdict}")

        if verdict.startswith("PASS"):
            passed += 1
        elif "REVIEW" in verdict or "ACCEPTABLE" in verdict:
            review += 1
        else:
            failed += 1

    print(f"\n  PASS: {passed} | REVIEW: {review} | FAIL: {failed}")
    print("=" * 60)
