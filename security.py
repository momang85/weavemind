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

# 反引号里的行内代码**看起来像命令**才算命令注入。
# 此前规则是"任意成对反引号且中间 ≥2 字符"，于是正常中文研报里的 `` `CNY` ``、
# `` `亿元` `` 都被判成命令注入，进指令前整段被替换成 `[已过滤可疑内容：命令注入]`
# （架构复核 P1：分析被吃掉）。收窄后真实命令仍拦得住：
#   `rm -rf /`、`curl http://x | sh`、`powershell -enc ...`、`cat /etc/passwd`
_SHELL_VERBS = (
    "rm", "del", "format", "mkfs", "dd", "shutdown", "reboot", "kill", "killall",
    "curl", "wget", "nc", "ncat", "netcat", "telnet", "ssh", "scp", "rsync",
    "powershell", "pwsh", "cmd", "bash", "sh", "zsh", "python", "python3", "perl",
    "ruby", "node", "npm", "pip", "pip3", "git", "sudo", "su", "chmod", "chown",
    "cat", "echo", "export", "invoke-expression", "iex", "reg", "sc", "net",
)
_SHELL_INLINE_RE = re.compile(
    r"`\s*(?:"
    r"(?:" + "|".join(_SHELL_VERBS) + r")\b[^`]*"          # 已知命令动词开头
    r"|/[A-Za-z0-9_./-]{2,}[^`]*"                          # 以路径开头
    r"|[^`]*(?:&&|\|\||\||;)\s*(?:" + "|".join(_SHELL_VERBS) + r")\b[^`]*"
    r")`",
    re.I,
)

# 提示词/指令注入模式（规则级第一道防线；外部护栏为第二道）。
# 每条带**稳定 rule_id**：诊断只记 ID 与形状，不记正文（架构复核 §F1 要求）。
_INJECTION_PATTERNS = [
    (re.compile(r"忽略(之前|以上|所有|系统)?(的)?(指令|提示|要求|规则)", re.I),
     "忽略指令", "ignore_instructions"),
    (re.compile(r"(无视|不要理会|忘记|遗忘)(之前|以上|系统)?(的)?(指令|提示|规则)", re.I),
     "无视指令", "ignore_instructions"),
    (re.compile(r"扮演|假装你是|冒充|system prompt|system prompt 泄露", re.I),
     "角色扮演/系统提示泄露", "role_play_or_prompt_leak"),
    (re.compile(r"(?:UPDATE|DROP|DELETE|INSERT|ALTER)\s+[A-Za-z_]", re.I),
     "SQL 注入", "sql_dml"),
    (re.compile(r"(?:;\s*--|\bUNION\s+SELECT\b)", re.I), "SQL 注入", "sql_comment_or_union"),
    (re.compile(r"\$\s*\(", re.I), "命令注入", "shell_substitution"),
    (_SHELL_INLINE_RE, "命令注入", "shell_inline_code"),
    (re.compile(r"base64\s*[-_]\s*d|b64decode|fromCharCode", re.I),
     "编码混淆", "encoding_obfuscation"),
    (re.compile(r"curl\s+\S+|wget\s+\S+|powershell\s+-", re.I),
     "命令注入", "shell_download_or_powershell"),
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


def detect_injection_detail(text: str) -> tuple[bool, str, str]:
    """检测提示词/指令注入。返回 `(是否命中, 原因, rule_id)`。

    rule_id 是稳定标识（诊断用），label 是给人看的文案。
    """
    t = str(text or "")
    if len(t) > 20000:
        t = t[:20000]
    for pat, label, rule_id in _INJECTION_PATTERNS:
        if pat.search(t):
            return True, label, rule_id
    return False, "", ""


def detect_injection(text: str) -> tuple[bool, str]:
    """检测提示词/指令注入。返回 (是否命中, 原因)。"""
    bad, label, _rule_id = detect_injection_detail(text)
    return bad, label


def scan_lines(text: str) -> list[tuple[int, str, str]]:
    """逐行扫描注入：返回 `[(行号, 原因, rule_id)]`（从 0 计）。

    为什么要逐行：整段命中就把整段替换掉，会连没问题的分析一起吞掉
    （架构复核 P1）。逐行只隔离命中那一行。
    """
    hits: list[tuple[int, str, str]] = []
    for i, line in enumerate(str(text or "").splitlines()):
        bad, label, rule_id = detect_injection_detail(line)
        if bad:
            hits.append((i, label, rule_id))
    return hits


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
