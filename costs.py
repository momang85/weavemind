# -*- coding: utf-8 -*-
"""LLM 成本估算（对标标准 C4-4.3 成本可观测）。
价格为估算值（美元/百万 token），可用环境变量 COST_IN_<MODEL>/COST_OUT_<MODEL> 覆盖。
"""

import os

PRICES = {
    "deepseek-v4-flash": {"in": 0.30, "out": 0.60},
    "deepseek-ai/deepseek-v3": {"in": 0.30, "out": 0.90},
    "deepseek-v4-pro": {"in": 2.00, "out": 8.00},
}
DEFAULT_PRICE = {"in": 1.00, "out": 2.00}


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
