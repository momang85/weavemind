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


# 网页/文件上传（材料入口）：3 MiB ——新人上传的正文/PDF 通常远小于此
UPLOAD_MAX_BYTES = _env_int("WEAVEMIND_UPLOAD_MAX_BYTES", 3 * _MIB)
# 直链下载 PDF：30 MB —— A 股年报正文常在 5–15 MB
DOWNLOAD_MAX_BYTES = _env_int("WEAVEMIND_DOWNLOAD_MAX_BYTES", 30 * _MIB)
# 正文类抓取（HTML/JSON）：8 MiB
TEXT_MAX_BYTES = _env_int("WEAVEMIND_TEXT_MAX_BYTES", 8 * _MIB)
# 二进制抓取（非 PDF 的通用二进制）：64 MiB
BINARY_MAX_BYTES = _env_int("WEAVEMIND_BINARY_MAX_BYTES", 64 * _MIB)
# PDF 解析页数上限（超长年报：给出明确错误，而不是解析到内存爆掉）
PDF_MAX_PAGES = _env_int("WEAVEMIND_PDF_MAX_PAGES", 1200)

_LABELS = {
    "upload": ("材料上传", UPLOAD_MAX_BYTES),
    "download": ("披露文件下载", DOWNLOAD_MAX_BYTES),
    "text": ("正文抓取", TEXT_MAX_BYTES),
    "binary": ("二进制抓取", BINARY_MAX_BYTES),
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
