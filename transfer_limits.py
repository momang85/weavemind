# -*- coding: utf-8 -*-
"""传输容量策略的**单一来源**（A1）：上传 / 下载 / 内容抓取共用一个可配置上限。

为什么单独一个模块：同一件事此前散在三处硬编码——材料上传 3 MiB
（`material_intake.MAX_UPLOAD_BYTES`）、PDF 下载 30 MB（`annual_report_pdf.MAX_BYTES`）、
内容抓取 cap（`net_policy.fetch_document(cap=…)` 的调用点）。三处不一致的后果是
"同一个文件在这个入口能进、在另一个入口被拒"，且错误信息各不相同。

规则：
- 都有**明确上限**（不放无上限）；可用环境变量覆盖，便于部署时收紧或放宽；
- 超限必须给出**具体数字**与入口名，便于用户自己判断要看哪一段（而不是"解析失败"）；
- 解析页数/时间的限制也在这里登记（PDF 解析不在本模块实现）。
"""
from __future__ import annotations

import os

_MIB = 1024 * 1024


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _env_int_alias(names: tuple[str, ...], default: int) -> int:
    """按顺序读环境变量，第一个非空且合法的生效（用于**改名后兼容旧名**）。"""
    for name in names:
        raw = str(os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            v = int(float(raw))
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return default


# 网页/文件上传（材料入口）：30 MiB —— 与披露下载同一量级（K0-c）。
# 实机反例：4–5 MiB 的年报从"材料恢复"入口被 3 MiB 上限拒绝（同一份文件在下载入口
# 却进得来）。仍是**明确上限**，可用环境变量覆盖。
#
# 变量名：正式名是 `WEAVEMIND_UPLOAD_MAX_BYTES`；旧代码把它拼成了
# `WEAVIMIND_UPLOAD_MAX_BYTES`（少一个 E）。**两个都认**，正式名优先——
# 已经照旧名配过的部署不因改名失效（不删用户的既有配置）。
UPLOAD_MAX_BYTES = _env_int_alias(
    ("WEAVEMIND_UPLOAD_MAX_BYTES", "WEAVIMIND_UPLOAD_MAX_BYTES"), 30 * _MIB)
# 直链下载 PDF：30 MB —— A 股年报正文常在 5–15 MB
DOWNLOAD_MAX_BYTES = _env_int("WEAVEMIND_DOWNLOAD_MAX_BYTES", 30 * _MIB)
# 正文类抓取（HTML/JSON）：8 MiB
TEXT_MAX_BYTES = _env_int("WEAVEMIND_TEXT_MAX_BYTES", 8 * _MIB)
# 二进制抓取（非 PDF 的通用二进制）：64 MiB
BINARY_MAX_BYTES = _env_int("WEAVEMIND_BINARY_MAX_BYTES", 64 * _MIB)
# PDF 解析页数上限（超长年报：给出明确错误，而不是解析到内存爆掉）
PDF_MAX_PAGES = _env_int("WEAVEMIND_PDF_MAX_PAGES", 1200)

# ── 时间预算也要**按通道**分（2026-09-29 实机）───────────────────────────────
# 反例：材料直链取件用 30s 上限去下 4–5 MB 年报，三一/洋河的**第一次**取件都超时
# （运行中 webfetch worker 的实测原文：`读取超时（总截止 29.9824s）`），
# 只有重试才成功——把"站点慢/文件大"记成"材料不合格"是不对的。
# 因此：正文类 30s（快失败，不要卡住页面）；**披露文件下载 120s**（大文件 + 慢站点）；
# 两者都仍是**明确上限**，可用环境变量覆盖，且共用同一条根截止语义（不续期）。
TEXT_TIMEOUT = _env_float("WEAVEMIND_TEXT_TIMEOUT", 30.0)
DOWNLOAD_TIMEOUT = _env_float("WEAVEMIND_DOWNLOAD_TIMEOUT", 120.0)

_LABELS = {
    "upload": ("材料上传", UPLOAD_MAX_BYTES),
    "download": ("披露文件下载", DOWNLOAD_MAX_BYTES),
    "text": ("正文抓取", TEXT_MAX_BYTES),
    "binary": ("二进制抓取", BINARY_MAX_BYTES),
}
_TIMEOUTS = {
    "download": ("披露文件下载", DOWNLOAD_TIMEOUT),
    "text": ("正文抓取", TEXT_TIMEOUT),
    "binary": ("二进制抓取", DOWNLOAD_TIMEOUT),
}


def explain(kind: str) -> str:
    """给用户的**具体**超限说明（含数字与入口名）。"""
    label, cap = _LABELS.get(str(kind), ("内容", BINARY_MAX_BYTES))
    return f"{label}上限 {cap // _MIB} MiB（{cap} 字节；可用环境变量覆盖）"


def within(kind: str, size: int) -> bool:
    _, cap = _LABELS.get(str(kind), ("内容", BINARY_MAX_BYTES))
    try:
        return int(size) <= int(cap)
    except (TypeError, ValueError):
        return False


def timeout_of(kind: str) -> float:
    """该通道的**总截止**（秒）：披露文件下载比正文抓取宽，但都是明确上限。"""
    _, seconds = _TIMEOUTS.get(str(kind), ("正文抓取", TEXT_TIMEOUT))
    return float(seconds)


def timeout_explain(kind: str) -> str:
    """超时说明（含秒数与入口名）——与容量说明同一处口径。"""
    label, seconds = _TIMEOUTS.get(str(kind), ("正文抓取", TEXT_TIMEOUT))
    return f"{label}总截止 {seconds:g}s（可用环境变量覆盖）"
