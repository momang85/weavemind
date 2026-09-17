# -*- coding: utf-8 -*-
"""测试期共享隔离：把外部依赖的默认值钉成确定的替身。

为什么单开一个模块：多个测试文件都会驱动 `orchestrator_v2.run()`，而 run() 开头做两次
**真实网络**预检（端点可达性 + 余额）。本机 `config.json` 里有真实 key 时这些预检会打到
真端点——余额耗尽（HTTP 402）就让任务在预检阶段被拒，用例要断言的"反思/交付/恢复/审批"
逻辑根本没机会发生；CI 上无 key 时表现为"端点不可用"，同样早退。同一种机依赖在本会话里
把四个套件打成过假红（各自暴露为完全不同的断言失败），所以收敛成一个共享助手，
用法是在测试文件顶部挂模块级钩子：

    from tests_support import restore_llm_prechecks, stub_llm_prechecks

    def setUpModule():
        stub_llm_prechecks()

    def tearDownModule():
        restore_llm_prechecks()

注意：替身只覆盖"预检"这一步。要验证预检本身的行为（余额不足如何归类、端点不可达
如何拒绝），应另写用例显式设定期望的返回，而不是依赖本机的 key 与余额状态。
"""

from __future__ import annotations

from unittest import mock

_PATCHERS: list = []

_PRE_CHECKS = (
    # run() 起点的端点可用性预检（会真的连端点）
    ("llm_client.endpoints_available", lambda: (True, "stub（测试期不探测真实端点）")),
    # A3 余额预检：主/备都返回 insufficient_balance 时 run() 会拒绝任务
    ("llm_client.get_balance_status",
     lambda **kw: {"primary": {"ok": True, "reason": "ok"},
                   "backup": {"ok": True, "reason": "ok"}}),
)
# 刻意**不**替身 `get_task_llm_degradation` / `get_endpoint_warning`：它们是
# test_prompt_system 等用例的被测对象（断言降级台账的内容与继承关系），
# 全局换掉会让那些断言拿到空值（实测：KeyError: 'reasons'）。需要隔离"进程级台账
# 串味"的用例，请在自己文件里按需 patch。


def stub_llm_prechecks() -> None:
    """挂上"端点可用 + 余额充足"的替身（幂等：重复调用不会叠加）。"""
    if _PATCHERS:
        return
    for target, value in _PRE_CHECKS:
        pat = mock.patch(target, value)
        pat.start()
        _PATCHERS.append(pat)


def restore_llm_prechecks() -> None:
    """撤掉替身（由 tearDownModule 调用）。"""
    while _PATCHERS:
        _PATCHERS.pop().stop()
