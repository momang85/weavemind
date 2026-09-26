# -*- coding: utf-8 -*-
"""同 Worker 环境诊断（S0）：在**真正执行检索的那个环境**里取事实。

为什么单独一个模块：此前排障用的是"Zcode 终端所在环境 / 浏览器能打开主页 / 另一台机器联网成功"
这类替代证据，它们都不能说明 Worker 进程里到底发生了什么（专项 §2 的"可以确认／不能据此推断"表）。
本模块只回答三件事：**这个环境里有哪些通道、各自失败成什么类别、一次查询实际花了多少**。

两条纪律：
1. **不新建请求点**：每个探测都调用项目既有的通道函数（`adapters/text_search` 的 Bing/DDGS、
   `adapters/eastmoney` 的结构化财务、`adapters/cninfo` 的官方披露、`adapters/transport` 的文档下载）。
   诊断因此天然继承既有网络边界（主机白名单、协议/公网 IP 校验、不带凭据），也不会与生产路径
   漂移成第二套实现。
2. **有界且如实**：首轮 ≤ `MAX_CALLS` 次公开请求、总 ≤ `MAX_SECONDS` 秒、无重试、无模型调用；
   底层库隐藏 HTTP 次数时标 `http_calls_unknown`，不把一次 SDK 调用记成一次 HTTP；
   只输出类别、计数与主机名——不输出密钥、代理凭据、带 token 的 URL、客户查询正文
   （公开固定样例除外）。

输出只到标准输出（人类可读表格，或 `--json` 时输出 JSON）：诊断不写文件，
要留证据由调用方重定向，避免把外部输入变成写文件目标。

用法（在运行包内执行才是 Worker 同环境）：
    runtime\\python.exe search_diag.py --no-network
    runtime\\python.exe search_diag.py --json > search_diag.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

# 公开固定样例（专项 §4 指定；只有它允许在证据里保留全文）
PUBLIC_SAMPLE = "洋河股份 002304 2024 年度报告"
SAMPLE_COMPANY = "洋河股份"
SAMPLE_CODE = "002304"
SAMPLE_YEAR = 2024

MAX_CALLS = 6
MAX_SECONDS = 60.0
_MAX_BODY = 512 * 1024
# 已知公开披露文件的冻结记录（项目内相对路径；读取前会做归一化与容器校验）
FROZEN_SAMPLE_REL = "evals/real/yanghe_ar2024_excerpt.json"
# 同一份冻结记录里的公开 URL。`evals/` 不进运行包，包内只能用它——固定常量，
# 不是按输入拼出来的目标（手拼 URL 会绕过"不新建请求点"的纪律）。
KNOWN_DISCLOSURE_URL = "https://data.eastmoney.com/notices/detail/002304/AN202504281664011244.html"

# 错误分类（专项 §4 的类别表）。这一层的核心目的是区分
# "正常没找到"（no_results / no_relevant_results）与"根本没有完成查询"（其余）。
ERROR_CLASSES = (
    "missing_dependency", "not_configured", "proxy_error", "dns_error", "tls_error",
    "timeout", "policy_blocked", "auth_error", "rate_limited", "challenge",
    "parse_error", "no_results", "no_relevant_results", "partial", "ok",
)
_RESULT_CLASSES = ("ok", "no_results", "no_relevant_results", "partial")

_CHALLENGE_MARKERS = ("captcha", "验证码", "人机验证", "are you a robot", "security check",
                      "访问受限", "请开启 javascript")


class Budget:
    """首轮诊断预算：调用次数 + 单调时钟截止，二者共用。"""

    def __init__(self, calls: int = MAX_CALLS, seconds: float = MAX_SECONDS):
        self.calls_left = int(calls)
        self.deadline = time.monotonic() + float(seconds)
        self.used = 0

    def time_left(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return self.calls_left <= 0 or self.time_left() <= 0

    def take(self) -> float:
        """占用一次调用，返回本次可用秒数；超预算抛 RuntimeError（不重试）。"""
        if self.expired():
            raise RuntimeError("budget exhausted")
        self.calls_left -= 1
        self.used += 1
        return max(1.0, min(self.time_left(), 20.0))


def classify_error(exc: BaseException) -> str:
    """异常 → 专项类别表。判不出来时归 parse_error（不假装成 timeout/no_results）。"""
    name = type(exc).__name__
    text = f"{name}: {exc}".lower()
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        return "missing_dependency"
    if isinstance(exc, urllib.error.HTTPError):
        code = int(getattr(exc, "code", 0) or 0)
        if code in (401, 407):
            return "auth_error"
        if code == 403:
            return "policy_blocked"
        if code == 429:
            return "rate_limited"
        if code in (404, 410):
            return "not_found"
        return "parse_error"
    if isinstance(exc, ssl.SSLError) or "certificate verify failed" in text:
        return "tls_error"
    if isinstance(exc, socket.gaierror) or "getaddrinfo" in text or "nodename" in text \
            or "name or service not known" in text:
        return "dns_error"
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in text \
            or "timeout" in text:
        return "timeout"
    if "proxy" in text:
        return "proxy_error"
    if "policy" in text or "disallowed" in text or "blocked" in text:
        return "policy_blocked"
    if "challenge" in text or "captcha" in text:
        return "challenge"
    # "查询完成了但一条也没找到"不是故障（专项 §4：必须能与"没完成查询"分开）。
    # ddgs 在引擎跑完且无结果时抛 `DDGSException("No results found.")`；包内实测
    # `backend=brave` 8 秒后正是这条——按 parse_error 记会把它当后端故障误熔断。
    if "no results found" in text or "no result" in text:
        return "no_results"
    return "parse_error"


def looks_like_challenge(body_head: str) -> bool:
    low = str(body_head or "").lower()
    return any(m in low for m in _CHALLENGE_MARKERS)


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(str(url)).hostname or ""
    except Exception:
        return ""


def _dep_versions() -> dict:
    """检索相关依赖版本（只报版本号，不报路径）。"""
    out: dict = {}
    for name in ("ddgs", "requests", "urllib3", "httpx", "bs4", "lxml", "certifi", "redis"):
        try:
            mod = __import__(name)
            out[name] = str(getattr(mod, "__version__", "") or "unknown")
        except Exception as exc:
            out[name] = f"missing({type(exc).__name__})"
    return out


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _project_file(rel: str) -> Path:
    """项目内相对路径 → 绝对路径，并校验归一化后仍在项目目录内（拒绝越界）。"""
    root = _project_root()
    p = (root / str(rel)).resolve()
    if p != root and root not in p.parents:
        raise RuntimeError(f"路径越出项目目录：{rel}")
    return p


def _config_facts() -> dict:
    """配置状态：走项目既有加载器，只取布尔（不输出任何值）。"""
    out: dict = {"config_json": False}
    try:
        import launcher
        cfg = launcher._load_config() or {}
        llm = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
        out = {
            "config_json": _project_file("config.json").exists(),
            "llm_base_url_set": bool(str(llm.get("base_url") or "").strip()),
            "llm_key_set": bool(str(llm.get("api_key") or "").strip()),
            "search_api_key_set": bool(str(cfg.get("search_api_key") or "").strip()),
        }
    except Exception:
        pass
    return out


def environment_facts() -> dict:
    """离线事实：运行身份 / 解释器 / 依赖版本 / 通道入口 / 配置与代理（只报布尔）。无网络请求。"""
    root = _project_root()
    ident = "unknown"
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import launcher
        ident = launcher.runtime_identity()
    except Exception as exc:
        ident = f"unknown({type(exc).__name__})"
    entries = {}
    for name, rel in (
        ("worker_search", "worker_base.py"),
        ("light_text_search", "adapters/text_search.py"),
        ("url_health", "adapters/url_health.py"),
        ("transport", "adapters/transport.py"),
        ("cninfo_disclosure", "adapters/cninfo.py"),
        ("eastmoney_structured", "adapters/eastmoney.py"),
        ("document_pdf", "annual_report_pdf.py"),
        ("net_policy", "net_policy.py"),
    ):
        entries[name] = _project_file(rel).exists()
    return {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "runtime_identity": ident,
        "python": sys.version.split()[0],
        "executable": os.path.basename(sys.executable),
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "dependencies": _dep_versions(),
        "entries": entries,
        "config": _config_facts(),
        # 只报"是否设置"，不报任何值
        "proxy_env_present": {
            k: bool(os.environ.get(k)) for k in
            ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
             "NO_PROXY", "no_proxy")
        },
        "ssl_verify_disabled_by_diag": False,   # 诊断不提供关闭校验的开关
    }


def _budget_check(rec: dict, budget: Budget, elapsed: float) -> dict:
    """单次探测超时即如实标注：诊断自己也不许把"跑超了"写成正常。

    实测教训：包内 ddgs 一次调用 90 秒，冲穿了 60 秒预算，后续探测全部拿不到时间。
    所以每次调用后核对实际耗时，超了就改判 timeout 并把原因写清楚。
    """
    allowance = budget.time_left()
    if elapsed > MAX_SECONDS or (budget.expired() and elapsed > 8.0):
        rec = dict(rec)
        rec["status"] = "timeout"
        rec["reason"] = (f"单次调用耗时 {elapsed:.1f}s，超出本轮诊断预算"
                         f"（≤{MAX_SECONDS:.0f}s / 剩余 {allowance:.1f}s）；"
                         f"原状态 {rec.get('status')}")
        rec["overran_budget"] = True
    return rec


def _probe_record(channel: str, *, status: str, host: str = "", provider: str = "",
                  backend: str = "", http: int = 0, items: int | None = None,
                  content_type: str = "", elapsed: float = 0.0, reason: str = "",
                  http_unknown: bool = False, extra: dict | None = None) -> dict:
    rec = {"channel": channel, "status": status, "host": host, "provider": provider,
           "backend": backend, "http": http, "items": items,
           "content_type": content_type, "elapsed": round(elapsed, 2),
           "reason": reason[:160], "http_calls_unknown": http_unknown}
    if extra:
        rec.update(extra)
    return rec


def probe_search_html(budget: Budget) -> dict:
    """搜索通道①：Bing HTML（项目既有函数，固定主机白名单）。"""
    t0 = time.monotonic()
    try:
        budget.take()
        from adapters import text_search
        html = text_search._fetch_bing_html(PUBLIC_SAMPLE)
        blocks = len(text_search._BING_BLOCK_RE.findall(html or ""))
        if looks_like_challenge(str(html or "")[:2000]):
            return _probe_record("search_html", status="challenge", host="www.bing.com",
                                 elapsed=time.monotonic() - t0,
                                 reason="challenge/verification page markers found")
        return _probe_record("search_html", status="ok" if blocks else "no_results",
                             host="www.bing.com", items=blocks,
                             content_type="text/html", elapsed=time.monotonic() - t0,
                             reason="" if blocks else "HTTP 200 but no result blocks parsed")
    except RuntimeError as exc:
        return _probe_record("search_html", status="not_configured",
                             reason=str(exc), elapsed=time.monotonic() - t0)
    except Exception as exc:
        return _probe_record("search_html", status=classify_error(exc),
                             reason=str(exc), elapsed=time.monotonic() - t0)


def probe_search_sdk(budget: Budget) -> dict:
    """搜索通道②：DDGS SDK（**单后端、单次调用**，固定健康查询；不 auto 全扫）。

    为什么直接调 SDK 而不是 `text_search._search_ddg`：探测要**固定**一个后端看清它自己的
    耗时与错误，而生产路径会按预算在有界执行器里选提供方。这里只打一个后端、一次调用，
    并如实标注 ddgs 隐藏的 HTTP 次数。
    """
    t0 = time.monotonic()
    try:
        from adapters import text_search
        # 与生产同源：S1 之后 worker 与轻量路径都用"策略清单 ∩ ddgs 注册表实际启用"的首个后端，
        # 不用清单首位——包内 9.16 已停用 yandex，打无效名字会让 ddgs 静默回落 auto 全扫
        engine = text_search._first_available_engine()
        advertised = _advertised_backends()
        basis = _backend_basis()
        watch = {"advertised": advertised, "basis": basis}
    except Exception as exc:
        return _probe_record("search_sdk", status=classify_error(exc), provider="ddgs",
                             reason=str(exc), elapsed=time.monotonic() - t0,
                             http_unknown=True)
    if not engine:
        return _probe_record("search_sdk", status="not_configured", provider="ddgs",
                             reason="策略清单在当前 ddgs 版本里没有启用的后端"
                                    f"（实际启用：{','.join(advertised) or '注册表读不到'}）；"
                                    "不发请求（无效后端名会触发 auto 全扫）",
                             elapsed=time.monotonic() - t0, http_unknown=True,
                             extra=watch)
    try:
        budget.take()
    except RuntimeError as exc:
        return _probe_record("search_sdk", status="not_configured", provider="ddgs",
                             backend=engine, reason=str(exc),
                             elapsed=time.monotonic() - t0, http_unknown=True,
                             extra=watch)
    wait = max(3.0, min(budget.time_left(), 8.0))
    try:
        from ddgs import DDGS
    except Exception as exc:
        return _probe_record("search_sdk", status="missing_dependency", provider="ddgs",
                             backend=engine, reason=str(exc),
                             elapsed=time.monotonic() - t0, http_unknown=True,
                             extra=watch)
    try:
        with DDGS(timeout=wait) as ddgs:
            results = list(ddgs.text(PUBLIC_SAMPLE, backend=engine, max_results=5))
        n = len([r for r in results if isinstance(r, dict)])
        elapsed = time.monotonic() - t0
        rec = _probe_record("search_sdk", status="ok" if n else "no_results",
                            provider="ddgs", backend=engine, items=n, elapsed=elapsed,
                            http_unknown=True,
                            reason="" if n else "SDK returned zero items",
                            extra=watch)
        return _budget_check(rec, budget, elapsed)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        rec = _probe_record("search_sdk", status=classify_error(exc), provider="ddgs",
                            backend=engine, reason=str(exc), elapsed=elapsed,
                            http_unknown=True, extra=watch)
        return _budget_check(rec, budget, elapsed)


def _advertised_backends() -> list:
    """ddgs 当前版本真正启用的 text 后端（读 `ddgs.engines.ENGINES` 注册表，不发请求）。"""
    try:
        from adapters.search_quality import ddg_text_backends

        return list(ddg_text_backends())
    except Exception:
        return []


def _backend_basis() -> str:
    """生产选择后端的依据：registry（已核实）/ policy（注册表读不到，未核实）/ none。"""
    try:
        from adapters import text_search  # noqa: F401  (确认生产模块可导入)
        from adapters.search_quality import select_ddg_backend

        return str(select_ddg_backend()[1])
    except Exception:
        return "unknown"


def probe_structured(budget: Budget) -> dict:
    """结构化财务通道（东财聚合源）：探 schema 与行数，不把聚合源当官方原文。

    `fetch_ashare` 返回 dict（financials/metadata），无数据时抛 RuntimeError——
    探测按真实返回结构读数，避免把"有数据"读成"空结果"。
    """
    t0 = time.monotonic()
    try:
        budget.take()
        from adapters import eastmoney
        res = eastmoney.fetch_ashare(SAMPLE_COMPANY, SAMPLE_CODE,
                                     (SAMPLE_YEAR, SAMPLE_YEAR)) or {}
        rows = res.get("financials") if isinstance(res, dict) else res
        rows = list(rows or [])
        meta = (res.get("metadata") or {}) if isinstance(res, dict) else {}
        n = len(rows)
        fields = sorted((rows[0] or {}).keys())[:10] if n and isinstance(rows[0], dict) else []
        return _probe_record("structured_api", status="ok" if n else "no_results",
                             provider="eastmoney", items=n,
                             elapsed=time.monotonic() - t0,
                             reason="" if n else "financials 为空",
                             extra={"fields": fields,
                                    "source": str(meta.get("source") or ""),
                                    "unit": str(meta.get("unit") or ""),
                                    "annual_count": meta.get("annual_count")})
    except RuntimeError as exc:
        return _probe_record("structured_api", status="not_configured", provider="eastmoney",
                             reason=str(exc), elapsed=time.monotonic() - t0)
    except Exception as exc:
        return _probe_record("structured_api", status=classify_error(exc),
                             provider="eastmoney", reason=str(exc),
                             elapsed=time.monotonic() - t0)


def probe_disclosure(budget: Budget) -> dict:
    """官方披露通道（A 股）：当前实现状态如实报告。

    现状（专项 §3-7）：`adapters/cninfo.py` 默认关闭且 `_fetch_annual_rows` 恒抛错，
    A 股财务实际走东财聚合源——"有 PDF 解析器"不等于"能发现并取得目标公司年报"。
    本探测只回答"这条通道此刻是否可用"，不尝试绕过访问控制，也不伪造接口已打通。
    """
    t0 = time.monotonic()
    try:
        from adapters import cninfo
    except Exception as exc:
        return _probe_record("disclosure_api", status="missing_dependency",
                             reason=str(exc), elapsed=time.monotonic() - t0)
    try:
        if not cninfo.enabled():
            return _probe_record(
                "disclosure_api", status="not_configured", provider="cninfo",
                elapsed=time.monotonic() - t0,
                reason="官方披露通道未启用（默认关闭）；A 股财务当前走东财聚合源")
    except Exception as exc:
        return _probe_record("disclosure_api", status=classify_error(exc),
                             provider="cninfo", reason=str(exc),
                             elapsed=time.monotonic() - t0)
    try:
        budget.take()
        rows = cninfo.fetch_annual_report(SAMPLE_COMPANY, SAMPLE_CODE,
                                          (SAMPLE_YEAR, SAMPLE_YEAR)) or []
        n = len(rows)
        return _probe_record("disclosure_api", status="ok" if n else "no_results",
                             provider="cninfo", items=n,
                             elapsed=time.monotonic() - t0)
    except RuntimeError as exc:
        return _probe_record("disclosure_api", status="not_configured", provider="cninfo",
                             reason=str(exc), elapsed=time.monotonic() - t0)
    except Exception as exc:
        return _probe_record("disclosure_api", status=classify_error(exc), provider="cninfo",
                             reason=str(exc), elapsed=time.monotonic() - t0)


def _frozen_document_url() -> tuple[str, str]:
    """已知公开披露文件：优先读冻结样本记录，包内没有该样本时用同一份记录的常量。

    返回 (url, 说明)；都拿不到时返回 ("", 原因)。
    """
    try:
        p = _project_file(FROZEN_SAMPLE_REL)
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        url = str(data.get("url") or "").strip()
        if url.startswith("https://"):
            return url, str(data.get("title") or "")
    except Exception:
        pass
    # 冻结样本（evals/）不进运行包：包内回退到同一份记录里的常量 URL（不是手拼、不是猜路径）
    return KNOWN_DISCLOSURE_URL, "洋河股份:2024年年度报告（冻结样本记录的公开 URL）"


def probe_document(budget: Budget) -> dict:
    """文档通道：用**项目既有下载路径**取一个已知公开披露文件，并判定取回的是什么。

    HTTP 200 不够（专项 §4）：这里再看内容类型与 PDF 魔数。注意"取回的是 HTML 查看页"
    不是通道故障，而是**候选本身不是 PDF 直链**——如实记 `kind`，不把两者混为一谈。
    """
    t0 = time.monotonic()
    url, note = _frozen_document_url()
    if not url:
        return _probe_record("document", status="not_configured", reason=note,
                             elapsed=time.monotonic() - t0)
    try:
        budget.take()
        from adapters.transport import get_via_urllib
        text = get_via_urllib(url, timeout=20, encoding="latin-1")
        data = str(text).encode("latin-1", errors="replace")[:_MAX_BODY]
        head = data[:1024]
        try:
            from annual_report_pdf import looks_like_pdf
            is_pdf = bool(looks_like_pdf(url, head))
        except Exception:
            is_pdf = head[:4] == b"%PDF"
        low = head.lower()
        if is_pdf:
            kind = "pdf"
        elif b"<html" in low or b"<!doctype" in low:
            kind = "html"
        elif head.lstrip()[:1] in (b"{", b"["):
            kind = "json"
        else:
            kind = "other"
        reason = ""
        if kind != "pdf":
            reason = "取回的不是 PDF（该候选是公告查看页/其它类型），PDF 直链本轮未取得"
        return _probe_record("document", status="ok" if kind in ("pdf", "html", "json")
                             else "parse_error",
                             host=_host_of(url),
                             content_type="application/pdf" if is_pdf else kind,
                             elapsed=time.monotonic() - t0, reason=reason,
                             extra={"bytes": len(data), "kind": kind, "is_pdf": is_pdf,
                                    "sample_title": note[:60]})
    except RuntimeError as exc:
        return _probe_record("document", status="not_configured", host=_host_of(url),
                             reason=str(exc), elapsed=time.monotonic() - t0)
    except Exception as exc:
        return _probe_record("document", status=classify_error(exc), host=_host_of(url),
                             reason=str(exc), elapsed=time.monotonic() - t0)


def run_diagnostics(*, network: bool = True) -> dict:
    """完整诊断：离线事实 +（可选）有界公开探测。无重试、无模型调用。"""
    facts = environment_facts()
    facts["sample"] = PUBLIC_SAMPLE
    facts["budget"] = {"max_calls": MAX_CALLS, "max_seconds": MAX_SECONDS, "used": 0}
    if not network:
        facts["probes"] = []
        facts["note"] = "offline-only run（未发任何网络请求）"
        return facts
    budget = Budget()
    facts["probes"] = [
        probe_search_html(budget),
        probe_search_sdk(budget),
        probe_disclosure(budget),
        probe_structured(budget),
        probe_document(budget),
    ]
    facts["budget"]["used"] = budget.used
    facts["budget"]["seconds_left"] = round(budget.time_left(), 1)
    facts["summary"] = {
        "ok": [p["channel"] for p in facts["probes"] if p["status"] == "ok"],
        "failed": {p["channel"]: p["status"] for p in facts["probes"]
                   if p["status"] not in _RESULT_CLASSES},
        "zero_results": [p["channel"] for p in facts["probes"]
                         if p["status"] in ("no_results", "no_relevant_results")],
    }
    return facts


def render_table(facts: dict) -> str:
    """一页事实表（给人看；不含任何凭据）。"""
    lines = [
        "同 Worker 环境诊断（S0）",
        "=" * 52,
        f"检查时间     : {facts.get('checked_at')}",
        f"运行身份     : {facts.get('runtime_identity')}",
        f"Python       : {facts.get('python')}（{facts.get('executable')}）",
        f"平台         : {facts.get('platform')}",
        f"公开样例     : {facts.get('sample')}",
        "",
        "依赖版本：",
    ]
    for k, v in (facts.get("dependencies") or {}).items():
        lines.append(f"  {k:10} {v}")
    lines.append("")
    lines.append("代理环境变量（只报是否设置）：")
    lines.append("  " + ", ".join(f"{k}={'有' if v else '无'}"
                                   for k, v in (facts.get("proxy_env_present") or {}).items()))
    lines.append("")
    if facts.get("probes"):
        lines.append("通道探测（每行一次有界调用，无重试）：")
        for p in facts["probes"]:
            extra = f" backend={p['backend']}" if p.get("backend") else ""
            http = f" http={p['http']}" if p.get("http") else ""
            items = f" items={p['items']}" if p.get("items") is not None else ""
            unk = "（HTTP次数未知）" if p.get("http_calls_unknown") else ""
            lines.append(f"  [{p.get('status', ''):17}] {p.get('channel', ''):15}"
                         f"{p.get('host', '') or p.get('provider', '')}{extra}{http}{items}"
                         f" {p.get('elapsed', 0)}s{unk}")
            if p.get("reason"):
                lines.append(f"      原因：{p['reason']}")
        b = facts.get("budget") or {}
        lines.append(f"  预算：用 {b.get('used', 0)}/{b.get('max_calls')} 次，"
                     f"剩余 {b.get('seconds_left', 0)}s")
        s = facts.get("summary") or {}
        lines.append(f"  小结：可用 {s.get('ok') or '无'}；零结果 {s.get('zero_results') or '无'}；"
                     f"失败 {s.get('failed') or '无'}")
    else:
        lines.append("（离线运行，未做网络探测）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同 Worker 环境诊断（S0）")
    ap.add_argument("--no-network", action="store_true", help="只做离线事实检查")
    ap.add_argument("--json", action="store_true", help="输出 JSON（由调用方重定向落盘）")
    args = ap.parse_args(argv)
    facts = run_diagnostics(network=not args.no_network)
    if args.json:
        print(json.dumps(facts, ensure_ascii=False, indent=1))
    else:
        print(render_table(facts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
