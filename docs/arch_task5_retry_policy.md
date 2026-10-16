# 任务5 — worker_base.py 统一重试策略层

## 背景
当前 worker_base.py 的异常处理是散点式的：
- ConnectionError → 记 error 日志然后 continue，无等待无计数上限
- redis TimeoutError → 按类型名字符串判定，处理不统一
- 模型调用错误（rate limit / context too long / 拒绝）未分类处理

## 重试策略表（架构师指定，不得自行修改）
```python
RETRY_POLICY = {
    "network":    {"max": 3, "backoff": "exponential", "base": 2.0},  # 2s→4s→8s
    "rate_limit": {"max": 5, "backoff": "fixed",       "wait": 60},   # 429，等60s
    "parse":      {"max": 2, "backoff": "fixed",       "wait": 1},    # PDF/JSON解析
    "model_ctx":  {"max": 0, "backoff": None},   # context too long → 直接降级
    "model_deny": {"max": 0, "backoff": None},   # 内容拒绝 → 直接 fail
}
```

## 实现步骤
1. 在 worker_base.py 顶部（import 区之后）添加 RETRY_POLICY 字典。
2. 实现 retry_with_policy(fn, error_class, policy_key) 函数：
   按 policy_key 查表，执行对应退避策略，返回最终结果或抛出最后一次异常。
3. 找到现有 ConnectionError 处理（记 error 日志然后 continue 的位置），
   改为调用 retry_with_policy，最多重试 3 次，超过后才 continue。
4. 找到模型调用的异常捕获，按关键词分类：
   - 含 "context" / "too long" → model_ctx（不重试，触发降级逻辑）
   - 含 "rate" / "429"        → rate_limit（等待重试）
   - 含 "refuse" / "content" / "policy" → model_deny（直接 fail）
5. 保留 retry_queries 的搜索空结果重试逻辑（:1302–1324），不动它。

## 验收
- 单元测试：mock ConnectionError 连抛 2 次后成功，确认函数被调用 3 次、
  等待间隔符合指数退避。
- mock 429 连抛 6 次，确认第 6 次抛出后 fail，前 5 次等待 60s。
- 运行现有测试，无回归。

## 约束
- 只改异常处理分支，不改业务逻辑。
- RETRY_POLICY 数值不得自行调整，有疑问先报告。
