"""
织光 基础库测试脚本

测试 MessagingClient（使用 fakeredis 模拟）和 AgentRegistry（SQLite）。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

# 将项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (
    AgentRegistry,
    MessagingClient,
    Task,
    TaskStatus,
    Plan,
)

import fakeredis

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# 辅助：用 fakeredis 替换真实 Redis 实例
# ---------------------------------------------------------------------------


def make_fake_client() -> MessagingClient:
    """创建一个使用 fakeredis 的 MessagingClient 实例。"""
    return MessagingClient(
        "localhost", 6379,
        _redis_client=fakeredis.FakeRedis(decode_responses=True),
    )


# ============================================================================
# Test 1: 数据结构
# ============================================================================


def test_dataclasses() -> None:
    print("\n" + "=" * 60)
    print("TEST 1: 数据结构 (Task / Plan / TaskStatus)")
    print("=" * 60)

    t1 = Task(
        task_id="task-001",
        parent_id=None,
        instruction="搜索最新 AI 论文",
    )
    assert t1.status == TaskStatus.PENDING, f"默认状态应为 PENDING，实际: {t1.status}"
    assert t1.result is None, "初始 result 应为 None"
    print(f"  ✓ Task 创建: {t1}")

    t1.status = TaskStatus.IN_PROGRESS
    assert t1.status == TaskStatus.IN_PROGRESS
    print(f"  ✓ 状态变更 -> IN_PROGRESS")

    t2 = Task(
        task_id="task-002",
        parent_id="task-001",
        instruction="下载论文 PDF",
        status=TaskStatus.SUCCESS,
        result={"url": "https://example.com/paper.pdf"},
    )
    assert t2.status == TaskStatus.SUCCESS
    assert t2.result["url"] == "https://example.com/paper.pdf"
    print(f"  ✓ 子任务创建: {t2}")

    plan = Plan(goal="调研 Transformer 最新进展", steps=[t1, t2])
    assert len(plan.steps) == 2
    assert plan.goal == "调研 Transformer 最新进展"
    print(f"  ✓ Plan 创建: goal='{plan.goal}', {len(plan.steps)} steps")

    # TaskStatus 枚举完整性
    all_statuses = {s.value for s in TaskStatus}
    expected = {"PENDING", "IN_PROGRESS", "SUCCESS", "FAILED"}
    assert all_statuses == expected, f"枚举不完整: {all_statuses}"
    print(f"  ✓ TaskStatus 枚举完整: {all_statuses}")

    print("  ✅ 数据结构测试全部通过")


# ============================================================================
# Test 2: AgentRegistry (SQLite)
# ============================================================================


def test_agent_registry() -> None:
    print("\n" + "=" * 60)
    print("TEST 2: AgentRegistry (SQLite)")
    print("=" * 60)

    db_path = ":memory:"
    registry = AgentRegistry(db_path)

    # 2a. 注册智能体
    registry.register("agent_search", ["web_search", "scrape"], "idle")
    registry.register("agent_code", ["code_exec", "test"], "idle")
    registry.register("agent_writer", ["content_write"], "busy")
    print("  ✓ 注册 3 个智能体")

    # 2b. list_agents
    agents = registry.list_agents()
    assert len(agents) == 3, f"应有 3 个智能体，实际: {len(agents)}"
    print(f"  ✓ list_agents 返回 {len(agents)} 条记录")

    # 2c. find_capable_agent: 能找到 idle + 有对应能力
    found = registry.find_capable_agent("web_search")
    assert found == "agent_search", f"应找到 agent_search，实际: {found}"
    print(f"  ✓ find_capable_agent('web_search') -> '{found}'")

    # 2d. find_capable_agent: 能力存在但 agent 是 busy
    found = registry.find_capable_agent("content_write")
    assert found is None, f"agent_writer 是 busy 不应被找到，实际: {found}"
    print(f"  ✓ find_capable_agent('content_write') -> None (busy)")

    # 2e. find_capable_agent: 不存在的能���
    found = registry.find_capable_agent("nonexistent_capability")
    assert found is None, f"不存在的能力应返回 None，实际: {found}"
    print(f"  ✓ find_capable_agent('nonexistent') -> None")

    # 2f. update agent: 更新状态和能力
    registry.register("agent_writer", ["content_write", "translate"], "idle")
    found = registry.find_capable_agent("translate")
    assert found == "agent_writer", f"更新后应找到 agent_writer，实际: {found}"
    print(f"  ✓ 更新 agent_writer 后 find_capable_agent('translate') -> '{found}'")

    # 2g. update_heartbeat
    before = registry.get_agent("agent_search")
    assert before is not None
    time.sleep(0.1)  # 确保时间戳有差异
    registry.update_heartbeat("agent_search")
    after = registry.get_agent("agent_search")
    assert after is not None
    # 心跳时间应该更新了
    print(
        f"  ✓ update_heartbeat: {before['last_heartbeat']} -> {after['last_heartbeat']}"
    )

    # 2h. update_heartbeat 不存在的 agent (不应崩溃)
    registry.update_heartbeat("nonexistent_agent")
    print("  ✓ update_heartbeat 对不存在的 agent 不抛异常")

    # 2i. get_agent
    info = registry.get_agent("agent_code")
    assert info is not None
    assert info["agent_id"] == "agent_code"
    assert "code_exec" in info["capabilities"]
    assert info["status"] == "idle"
    print(f"  ✓ get_agent('agent_code'): {info}")

    # 2j. get_agent 不存在
    info = registry.get_agent("ghost")
    assert info is None
    print(f"  ✓ get_agent('ghost') -> None")

    # 2k. 输入校验
    try:
        registry.register("", ["web"], "idle")
        assert False, "空 agent_id 应抛出 ValueError"
    except ValueError:
        print("  ✓ register 空 agent_id 抛出 ValueError")

    try:
        registry.register("agent_x", [], "idle")
        assert False, "空 capabilities 应抛出 ValueError"
    except ValueError:
        print("  ✓ register 空 capabilities 抛出 ValueError")

    registry.close()
    print("  ✅ AgentRegistry 测试全部通过")


# ============================================================================
# Test 3: MessagingClient 发布/订阅（使用 fakeredis）
# ============================================================================


def test_messaging_pubsub() -> None:
    print("\n" + "=" * 60)
    print("TEST 3: MessagingClient 发布/订阅")
    print("=" * 60)

    # 创建客户端并用 fakeredis 替换
    client = make_fake_client()

    channel = "test_channel"
    received_messages: list[dict] = []

    # 订阅线程
    def subscriber() -> None:
        for msg in client.subscribe(channel):
            received_messages.append(msg)
            if len(received_messages) >= 2:
                break

    sub_thread = threading.Thread(target=subscriber, daemon=True)
    sub_thread.start()
    time.sleep(0.2)  # 等待订阅就绪

    # 发布消息
    msg1 = {"type": "plan", "goal": "搜索最新 AI 论文"}
    result = client.publish(channel, msg1)
    assert result is True
    print(f"  ✓ publish 消息1: {msg1}")

    msg2 = {"type": "task", "task_id": "task-001", "instruction": "搜索"}
    result = client.publish(channel, msg2)
    assert result is True
    print(f"  ✓ publish 消息2: {msg2}")

    sub_thread.join(timeout=5)
    assert len(received_messages) == 2, f"应收到 2 条消息，实际: {len(received_messages)}"
    assert received_messages[0] == msg1
    assert received_messages[1] == msg2
    print(f"  ✓ 订阅端正确收到 {len(received_messages)} 条消息")

    # 测试包含复杂类型的消息（用独立客户端避免 pubsub 状态干扰）
    received_complex: list[dict] = []
    client2 = make_fake_client()
    channel2 = "test_channel_complex"

    def subscriber2() -> None:
        for msg in client2.subscribe(channel2):
            received_complex.append(msg)
            break

    sub2 = threading.Thread(target=subscriber2, daemon=True)
    sub2.start()
    time.sleep(0.3)
    complex_msg = {
        "type": "result",
        "data": {"url": "https://example.com", "status_code": 200},
        "items": [1, 2, 3],
    }
    client2.publish(channel2, complex_msg)
    sub2.join(timeout=5)
    assert received_complex == [complex_msg], f"复杂消息应完整传递, 实际: {received_complex}"
    print(f"  ✓ 复杂嵌套消息传递正确")

    client2.close()
    client.close()
    print("  ✅ 发布/订阅测试全部通过")


# ============================================================================
# Test 4: MessagingClient 任务队列
# ============================================================================


def test_messaging_task_queue() -> None:
    print("\n" + "=" * 60)
    print("TEST 4: MessagingClient 任务队列 (push_task / pop_task)")
    print("=" * 60)

    client = make_fake_client()

    agent_id = "agent_search"

    # 4a. 压入任务
    task1 = {"task_id": "task-001", "instruction": "搜索 Transformer 论文"}
    task2 = {"task_id": "task-002", "instruction": "搜索 Attention 机制"}
    task3 = {"task_id": "task-003", "instruction": "搜索 ViT 论文"}

    assert client.push_task(agent_id, task1)
    assert client.push_task(agent_id, task2)
    assert client.push_task(agent_id, task3)
    print(f"  ✓ push_task 压入 3 个任务")

    # 4b. 弹出任务：lpush 在左，brpop 在右 → FIFO 顺序
    popped1 = client.pop_task(agent_id, timeout=1)
    assert popped1 == task1, f"第1个弹出应为 task1，实际: {popped1}"
    print(f"  ✓ pop_task #1: {popped1['task_id']}")

    popped2 = client.pop_task(agent_id, timeout=1)
    assert popped2 == task2, f"第2个弹出应为 task2，实际: {popped2}"
    print(f"  ✓ pop_task #2: {popped2['task_id']}")

    popped3 = client.pop_task(agent_id, timeout=1)
    assert popped3 == task3, f"第3个弹出应为 task3，实际: {popped3}"
    print(f"  ✓ pop_task #3: {popped3['task_id']}")

    # 4c. 空队列超时返回 None
    empty = client.pop_task(agent_id, timeout=1)
    assert empty is None, f"空队列应返回 None，实际: {empty}"
    print(f"  ✓ pop_task 空队列超时返回 None")

    # 4d. 不同 agent_id 队列隔离
    task_a = {"task_id": "task-agent-a", "instruction": "A"}
    task_b = {"task_id": "task-agent-b", "instruction": "B"}
    client.push_task("agent_a", task_a)
    client.push_task("agent_b", task_b)
    assert client.pop_task("agent_a", timeout=1) == task_a
    assert client.pop_task("agent_b", timeout=1) == task_b
    print(f"  ✓ 不同 agent_id 队列隔离正确")

    # 4e. 序列化异常
    try:
        client.push_task(agent_id, {"bad": object()})
        assert False, "不可序列化对象应抛出 TypeError"
    except TypeError:
        print("  ✓ 不可序列化对象抛出 TypeError")

    client.close()
    print("  ✅ 任务队列测试全部通过")


# ============================================================================
# Test 5: 端到端流程模拟
# ============================================================================


def test_e2e_workflow() -> None:
    """模拟完整的：注册 → 分配任务 → 执行 → 返回结果 流程"""
    print("\n" + "=" * 60)
    print("TEST 5: 端到端流程模拟")
    print("=" * 60)

    # 初始化存储和消息
    registry = AgentRegistry(":memory:")
    msg_client = make_fake_client()

    # Step 1: 注册 Worker 智能体（每个 Worker 独立能力，避免 busy 冲突）
    registry.register("worker_search_1", ["web_search"], "idle")
    registry.register("worker_search_2", ["scrape"], "idle")
    registry.register("worker_writer", ["report_write"], "idle")
    print("  ✓ Step 1: 注册 3 个 Worker")

    # Step 2: Orchestrator 制定计划
    plan = Plan(
        goal="调研 Transformer 架构最新进展并生成报告",
        steps=[
            Task(task_id="t1", parent_id=None, instruction="搜索 Transformer 论文"),
            Task(task_id="t2", parent_id=None, instruction="提取论文核心内容"),
            Task(task_id="t3", parent_id=None, instruction="生成综述报告"),
        ],
    )
    print(f"  ✓ Step 2: 制定计划 '{plan.goal}' ({len(plan.steps)} steps)")

    # Step 3: 根据能力分配任务到队列
    dispatch_map = {
        "t1": ("web_search", "worker_search_1"),
        "t2": ("scrape", "worker_search_2"),
        "t3": ("report_write", "worker_writer"),
    }

    for step in plan.steps:
        capability, worker = dispatch_map[step.task_id]
        found = registry.find_capable_agent(capability)
        assert found == worker, f"Step {step.task_id}: 期望 {worker}，找到 {found}"
        msg_client.push_task(worker, {
            "task_id": step.task_id,
            "instruction": step.instruction,
            "status": "PENDING",
        })
        # 标记为 busy
        registry.register(worker, registry.get_agent(worker)["capabilities"], "busy")
        print(f"  ✓ Step 3: {step.task_id} -> {worker} (capability: {capability})")

    # Step 4: Worker 拉取任务执行
    for worker_id in ["worker_search_1", "worker_search_2", "worker_writer"]:
        task = msg_client.pop_task(worker_id, timeout=1)
        assert task is not None, f"{worker_id} 应有任务可取"
        task["status"] = "SUCCESS"
        task["result"] = f"已完成: {task['instruction']}"
        print(f"  ✓ Step 4: {worker_id} 执行 {task['task_id']} -> SUCCESS")

    # Step 5: 所有任务完成后 Worker 恢复 idle
    for worker_id in ["worker_search_1", "worker_search_2", "worker_writer"]:
        registry.register(
            worker_id,
            registry.get_agent(worker_id)["capabilities"],
            "idle",
        )
    print(f"  ✓ Step 5: 所有 Worker 恢复 idle")

    # 验证所有 Worker 都是 idle
    agents = registry.list_agents()
    for a in agents:
        assert a["status"] == "idle", f"{a['agent_id']} 应为 idle"
    print(f"  ✓ 验证: 全部 {len(agents)} 个 Worker 状态为 idle")

    registry.close()
    msg_client.close()
    print("  ✅ 端到端流程测试全部通过")


# ============================================================================
# 主入口
# ============================================================================


if __name__ == "__main__":
    print("=" * 60)
    print("织光 (ZhiGuang) — 基础库测试")
    print("=" * 60)
    print(f"Python: {sys.version}")
    print(f"redis-py: {__import__('redis').__version__}")
    print(f"fakeredis: {__import__('fakeredis').__version__}")
    print(f"sqlite3: {__import__('sqlite3').sqlite_version}")

    try:
        test_dataclasses()
        test_agent_registry()
        test_messaging_pubsub()
        test_messaging_task_queue()
        test_e2e_workflow()

        print("\n" + "=" * 60)
        print("🎉 所有测试全部通过！")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 未预期异常: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
