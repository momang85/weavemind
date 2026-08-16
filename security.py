# -*- coding: utf-8 -*-
"""输入输出安全（对标标准 C4-4.4：提示词注入 / 指令注入 / 内容安全）。

- 用户目标长度与注入检测（web_ui 提交时）；
- 外部内容注入检测（抓取的网页/上一步结果进指令前）；
- 输出内容安全（可选外部护栏，env GUARDRAIL_API 配置后启用）。
"""

import os
import re
import time

MAX_GOAL_LEN = 2000

# 提示词/指令注入模式（规则级第一道防线；外部护栏为第二道）
_INJECTION_PATTERNS = [
    (re.compile(r"忽略(之前|以上|所有|系统)?(的)?(指令|提示|要求|规则)", re.I), "忽略指令"),
    (re.compile(r"(无视|不要理会|忘记|遗忘)(之前|以上|系统)?(的)?(指令|提示|规则)", re.I), "无视指令"),
    (re.compile(r"扮演|假装你是|冒充|system prompt|system prompt 泄露", re.I), "角色扮演/系统提示泄露"),
    (re.compile(r"(?:UPDATE|DROP|DELETE|INSERT|ALTER)\s+[A-Za-z_]", re.I), "SQL 注入"),
    (re.compile(r"(?:;\s*--|\bUNION\s+SELECT\b)", re.I), "SQL 注入"),
    (re.compile(r"\$\s*\(|`[^`]{2,}`", re.I), "命令注入"),
    (re.compile(r"base64\s*[-_]\s*d|b64decode|fromCharCode", re.I), "编码混淆"),
    (re.compile(r"curl\s+\S+|wget\s+\S+|powershell\s+-", re.I), "命令注入"),
]

# 轻量内容安全词（命中即标记；接入外部护栏后以此为准）
_SENSITIVE_TERMS = (
    "爆炸物", "制造炸弹", "抢银行", "制毒", "自杀方法", "色情", "赌博网站",
)


def sanitize_goal(goal: str) -> str:
    """截断超长目标，防止上下文/成本失控。"""
    g = str(goal or "").strip()
    if len(g) > MAX_GOAL_LEN:
        g = g[:MAX_GOAL_LEN]
    return g


def detect_injection(text: str) -> tuple[bool, str]:
    """检测提示词/指令注入。返回 (是否命中, 原因)。"""
    t = str(text or "")
    if len(t) > 20000:
        t = t[:20000]
    for pat, label in _INJECTION_PATTERNS:
        if pat.search(t):
            return True, label
    return False, ""


def check_content(text: str) -> tuple[bool, list[str]]:
    """输出内容安全检查（规则级）。命中返回 (False, 问题列表)。"""
    t = str(text or "")
    hits = [k for k in _SENSITIVE_TERMS if k in t]
    return (not hits, hits)


class RateLimiter:
    """简单滑动窗口限流（进程内）。"""

    def __init__(self, limit: int = 10, window: float = 60.0):
        self._limit = limit
        self._window = window
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        bucket = [t for t in self._hits.get(key, []) if now - t < self._window]
        if len(bucket) >= self._limit:
            self._hits[key] = bucket
            return False
        bucket.append(now)
        self._hits[key] = bucket
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
