# -*- coding: utf-8 -*-
"""LLM 成本估算与预算机制（对标标准 C4-4.3 成本可观测，Roadmap 余项④）。
价格为估算值（美元/百万 token），可用环境变量 COST_IN_<MODEL>/COST_OUT_<MODEL> 覆盖。

预算机制：
- 月度预算 BUDGET_MONTHLY_USD（默认 10 美元，0 表示不限制）；
- _record_usage 记账时同步累计到 Redis key llm_usage_month:<YYYY-MM>（TLL 62 天）；
- budget_exceeded() 判断本月是否超限；
- resolve_model_with_budget() 超限时把高价角色模型（planner/reflect/review/judge）
  降级为执行级（exec）模型，保证任务继续但成本受控；
- 超限降级状态通过 get_budget_status() 暴露（/api/health 与前端可观测）。
"""

import os
from datetime import datetime

PRICES = {
    "deepseek-v4-flash": {"in": 0.30, "out": 0.60},
    "deepseek-ai/deepseek-v3": {"in": 0.30, "out": 0.90},
    "deepseek-v4-pro": {"in": 2.00, "out": 8.00},
}
DEFAULT_PRICE = {"in": 1.00, "out": 2.00}

# 预算降级：高价角色 → 便宜执行模型（未配置时回退到当前模型）
_BUDGET_OVERRIDE_ROLE = "exec"
_prev_budget_state: dict = {"exceeded": False, "checked_at": ""}


def _price(model: str) -> dict:
    m = str(model or "").lower()
    base = PRICES.get(m) or PRICES.get(m.rsplit("/", 1)[-1]) or DEFAULT_PRICE
    env_in = os.environ.get(f"COST_IN_{m.replace('/', '_').upper()}")
    env_out = os.environ.get(f"COST_OUT_{m.replace('/', '_').upper()}")
    return {
        "in": float(env_in) if env_in else float(base["in"]),
        "out": float(env_out) if env_out else float(base["out"]),
    }


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p = _price(model)
    return (int(prompt_tokens or 0) / 1_000_000 * p["in"]
            + int(completion_tokens or 0) / 1_000_000 * p["out"])


def ledger_cost(ledger: dict) -> float:
    """从任务台账 Hash 估算总成本。字段：pt:<model> / ct:<model>。"""
    total = 0.0
    for k, v in (ledger or {}).items():
        if str(k).startswith("pt:"):
            model = str(k)[3:]
            pt = int(v or 0)
            ct = int(ledger.get(f"ct:{model}", 0) or 0)
            total += estimate_cost(model, pt, ct)
    return round(total, 4)


def _month_key() -> str:
    return datetime.now().strftime("%Y-%m")


def _month_redis_client():
    """月度预算 Redis 客户端（懒加载，失败返回 None 静默降级）。"""
    try:
        import redis as _redis
        return _redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            decode_responses=True,
        )
    except Exception:
        return None


def record_monthly_usage(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """月度成本累计（与任务台账并行记账，用于全局预算控制）。"""
    try:
        c = _month_redis_client()
        if c is None:
            return
        key = f"llm_usage_month:{_month_key()}"
        pipe = c.pipeline()
        pipe.hincrby(key, f"pt:{model or 'unknown'}", int(prompt_tokens or 0))
        pipe.hincrby(key, f"ct:{model or 'unknown'}", int(completion_tokens or 0))
        pipe.expire(key, 62 * 24 * 3600)
        pipe.execute()
    except Exception:
        pass


def get_monthly_spend() -> float:
    """本月已花费（美元）。Redis 不可用时返回 0（预算不生效但可观测降级）。"""
    try:
        c = _month_redis_client()
        if c is None:
            return 0.0
        ledger = c.hgetall(f"llm_usage_month:{_month_key()}")
        return ledger_cost(ledger)
    except Exception:
        return 0.0


def budget_limit() -> float:
    """月度预算上限（美元）。0 表示不限制。"""
    try:
        return max(0.0, float(os.environ.get("BUDGET_MONTHLY_USD", "10") or 0))
    except Exception:
        return 0.0


def budget_exceeded() -> bool:
    """是否超限（预算=0 表示不限制，永不超限）。"""
    lim = budget_limit()
    if lim <= 0:
        return False
    return get_monthly_spend() >= lim


def resolve_model_with_budget(usage: str, model: str) -> str:
    """超限时把高价角色模型降级为执行级模型；未超限原样返回。
    usage 为空或缺省调用不受影响（保持老行为）。"""
    global _prev_budget_state
    role = str(usage or "").lower()
    if not model or budget_limit() <= 0:
        return model
    try:
        if not budget_exceeded():
            _prev_budget_state = {"exceeded": False, "checked_at": ""}
            return model
        # 超限：非 exec 角色一律降级为 exec 级模型
        if role != "exec":
            downgraded = os.environ.get("LLM_MODEL") or model
            if not _prev_budget_state.get("exceeded"):
                _prev_budget_state = {
                    "exceeded": True,
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                }
                try:
                    import logging
                    logging.getLogger("llm_client").warning(
                        "月度预算超限：%s 角色 %s 降级为 %s",
                        role, model, downgraded,
                    )
                except Exception:
                    pass
            return downgraded
    except Exception:
        pass
    return model


def get_budget_status() -> dict:
    """预算可观测状态：{month, spend_usd, limit_usd, exceeded, degraded_at}。"""
    spend = get_monthly_spend()
    lim = budget_limit()
    return {
        "month": _month_key(),
        "spend_usd": round(spend, 4),
        "limit_usd": lim,
        "exceeded": lim > 0 and spend >= lim,
        "degraded_at": _prev_budget_state.get("checked_at", ""),
    }
