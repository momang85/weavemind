# -*- coding: utf-8 -*-
"""出口诊断（RUNTIME-1022 P0-B，20261022）：把"没数据/连不上"拆成可操作的类别。

背景（诊断快照 20261022）：系统 DNS 把巨潮/Bing/东财相关域名解析到 `198.18.0.x` 与
`2001:2::x`，全部落在保留段，被 `net_policy` 的 SSRF 防线拒绝。这不是"没有资料"、
不是"密钥问题"，也不是"站点坏了"——但此前失败到用户手里只剩统一的"策略拒绝"甚至
"无数据"，分不清下面这些完全不同的问题：

    解析失败（dns_error）        本机 DNS 查不到/查不动
    解析到保留段（reserved_address）解析成功但地址不是公网（fake-IP 特征）
    策略拒绝（policy_blocked） 应用自己按边界拒绝（协议/凭据/内网/未登记端点）
    TLS 失败（tls_error）         能连上但证书校验不过
    连接失败（connect_error）      根本没建立连接
    超时（timeout）               建立或读取太慢
    真实零结果（no_results）       查询完成了、确实没找到——不是故障

纪律（与 search_diag 相同的两条）：
1. **不新建请求点**：这里只做 DNS 解析（本地解析器查询）与异常分类，**不发 HTTP 请求**；
   解析对象是 adapters 里既有内容主机与 net_policy 已登记的公网端点——都是应用本来
   就要访问的目标，没有为了诊断多联系任何新主机。
2. **有界且如实、全程脱敏**：解析带超时（getaddrinfo 本身没有超时参数，放线程里等），
   输出只有主机名、IP、地址类别与策略判定；没有密钥、没有完整配置、没有查询正文。

"符合 fake-IP 特征"是**观察**不是**定论**：198.18.0.0/15 与 2001:2::/48 是保留段
（RFC 2544 / RFC 5180），解析到这里最常见的解释是本机 DNS 被代理/TUN 类工具替换成
fake-IP 映射，但本模块不识别、不指认任何具体软件，也不改任何主机网络设置——
要动 DNS/代理，请走人工调整说明（见 docs/evidence/20261022-runtime-repair）。
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time

# ── 类别表：与 search_diag.ERROR_CLASSES **同一套词**（同一异常在检索通道与诊断里同名），
# 应用侧专有的两个单列在 APP_ONLY 里，测试保证两个词表一致。
APP_ONLY = ("response_too_large", "unparsable_url")


def _category_vocabulary() -> tuple[str, ...]:
    try:
        import search_diag
        base = tuple(search_diag.ERROR_CLASSES)
    except Exception:                       # 诊断模块不可用时仍给出可用词表
        base = ("missing_dependency", "not_configured", "proxy_error", "dns_error",
                "tls_error", "timeout", "policy_blocked", "auth_error", "rate_limited",
                "challenge", "parse_error", "no_results", "no_relevant_results",
                "partial", "ok")
    return tuple(dict.fromkeys(base + APP_ONLY))


CATEGORIES = _category_vocabulary()

# 诊断目标：adapters 里**既有**的内容主机（只解析不发请求；测试保证与适配器一致，
# 防止这份清单和真实抓取目标漂移成第二套事实）。
KNOWN_CONTENT_HOSTS = (
    "www.cninfo.com.cn",
    "static.cninfo.com.cn",
    "www.bing.com",
    "datacenter-web.eastmoney.com",
    "push2.eastmoney.com",
)

_RESOLVE_TIMEOUT = 2.0
# 缓存：DNS 分类结果按 TTL 复用，健康轮询不必每次都解析
_CACHE_TTL = float(os.environ.get("WM_EGRESS_DIAG_TTL", "") or 60)
# 诊断目标数上限 + 整体时间预算（秒）：健康轮询在 /api/status 的同步路径上，
# DNS 全挂时也不能把它拖住。
_MAX_TARGETS = 12
_REPORT_BUDGET = float(os.environ.get("WM_EGRESS_DIAG_BUDGET", "") or 8.0)
_cache: dict = {"ts": 0.0, "report": None}


# ── 地址类别 ─────────────────────────────────────────────────────

def address_class(ip_text: str) -> str:
    """单个 IP → 地址类别。保留段先于"私有"判定（ipaddress 把 198.18/15 也算 private，
    但"测试段/fake-IP 特征"是更可操作的说法，必须单列出来）。"""
    try:
        ip = ipaddress.ip_address(str(ip_text).strip().strip("[]"))
    except ValueError:
        return "unparsable"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return "ipv4_mapped:" + address_class(str(ip.ipv4_mapped))
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if ip in ipaddress.ip_network("198.18.0.0/15"):
        return "benchmark_198_18"
    if ip in ipaddress.ip_network("2001:2::/48"):
        return "benchmark_2001_2"
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return "cgnat"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    if getattr(ip, "is_global", False):
        return "public"
    return "reserved"


_ADDRESS_LABELS = {
    "public": "公网全局可路由",
    "loopback": "本机环回",
    "link_local": "链路本地",
    "multicast": "组播",
    "unspecified": "未指定地址",
    "benchmark_198_18": "测试保留段 198.18.0.0/15（fake-IP 特征）",
    "benchmark_2001_2": "测试保留段 2001:2::/48（fake-IP 特征）",
    "cgnat": "运营商级 NAT 段 100.64.0.0/10",
    "private": "私网地址",
    "reserved": "保留地址",
    "unparsable": "无法解析的地址文本",
}


def address_label(cls: str) -> str:
    if cls.startswith("ipv4_mapped:"):
        return f"IPv4 映射地址（内层：{_ADDRESS_LABELS.get(cls.split(':', 1)[1], cls)}）"
    return _ADDRESS_LABELS.get(cls, cls)


def address_advice(cls: str) -> str:
    """面向操作者的下一步（**只建议、不动手**；应用侧绝不放行保留段）。"""
    if cls.startswith("ipv4_mapped:"):
        return address_advice(cls.split(":", 1)[1])
    if cls in ("benchmark_198_18", "benchmark_2001_2"):
        return ("解析到测试保留段：最常见解释是本机 DNS 被代理/TUN 类工具替换为 fake-IP "
                "映射（也可能有其它原因，需人工核对）。应用按 SSRF 防线拒绝该地址，不会放行；"
                "要恢复取数，需把 DNS 恢复为真实解析或为应用提供真实出口——这是主机侧的"
                "人工调整，应用不会自行修改。")
    if cls in ("loopback", "link_local", "private", "cgnat", "reserved", "unspecified"):
        return ("目标解析为本机/内网地址，应用按 SSRF 防线拒绝。请核对是否有 DNS 劫持或 "
                "hosts 改写；登记为本地服务（loopback）的端点除外。")
    if cls == "public":
        return "公网地址：应用会用已验 IP 直连并校验 TLS 证书；连接仍失败时看 tls_error/connect_error。"
    return ""


# ── 解析（有界、不猜）────────────────────────────────────────────

def _resolve_bounded(host: str, port: int = 0, timeout: float = _RESOLVE_TIMEOUT) -> dict:
    """带超时的 DNS 解析：`socket.getaddrinfo` 没有超时参数，放线程里等；
    超时也记 dns_error但**说明是解析超时**，不冒充别的结论。"""
    result: dict = {"ips": [], "error": "", "timed_out": False}

    def _work():
        try:
            infos = socket.getaddrinfo(host, port or None, proto=socket.IPPROTO_TCP)
            seen: list[str] = []
            for info in infos:
                if info[0] in (socket.AF_INET, socket.AF_INET6):
                    addr = str(info[4][0])
                    if addr not in seen:
                        seen.append(addr)
            result["ips"] = seen
            if not seen:
                result["error"] = "DNS 未返回可用地址"
        except OSError as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(max(0.1, float(timeout)))
    if th.is_alive():
        result["timed_out"] = True
        result["error"] = f"DNS 解析超过 {timeout:g}s 未返回"
    return result


# ── 单目标分类 ───────────────────────────────────────────────────

def classify_host(host: str, port: int = 0, *,
                  timeout: float = _RESOLVE_TIMEOUT) -> dict:
    """一个主机 → 类别 + 逐地址分类。只解析，不发请求；输出脱敏（无凭据可脱，天然安全）。"""
    host = str(host or "").strip().strip(".").lower()
    if not host:
        return {"host": host, "category": "unparsable_url", "addresses": [],
                "detail": "空主机名", "advice": []}
    # 字面 IP 不经 DNS
    literal_cls = address_class(host)
    if literal_cls != "unparsable":
        addresses = [{"ip": host, "address_class": literal_cls,
                      "label": address_label(literal_cls),
                      "public": literal_cls == "public"}]
        cat = "ok" if literal_cls == "public" else "reserved_address"
        return {"host": host, "category": cat, "addresses": addresses,
                "detail": "字面 IP（不经 DNS）",
                "advice": _advice_of(cat, literal_cls)}
    res = _resolve_bounded(host, port, timeout)
    if res["error"] and not res["ips"]:
        return {"host": host, "category": "dns_error", "addresses": [],
                "detail": res["error"] + ("（解析超时）" if res["timed_out"] else ""),
                "advice": ["本机 DNS 没能解析该主机：先确认本机 DNS/代理设置，再谈数据源。"]}
    addresses = []
    for ip in res["ips"]:
        cls = address_class(ip)
        addresses.append({"ip": ip, "address_class": cls,
                          "label": address_label(cls), "public": cls == "public"})
    bad = [a for a in addresses if not a["public"]]
    if bad:
        cls = bad[0]["address_class"]
        return {"host": host, "category": "reserved_address", "addresses": addresses,
                "detail": "解析到非公网地址：" + ",".join(a["ip"] for a in bad),
                "advice": _advice_of("reserved_address", cls)}
    return {"host": host, "category": "ok", "addresses": addresses,
            "detail": "全部解析到公网地址",
            "advice": _advice_of("ok", "public")}


def _advice_of(category: str, cls: str = "") -> list[str]:
    out: list[str] = []
    if cls:
        tip = address_advice(cls)
        if tip:
            out.append(tip)
    if category == "ok":
        out.append("该目标当前解析正常；历史失败与此无关时，先核对失败时刻的类别记录。")
    return out


# ── 聚合报告（健康探针与设置诊断共用）────────────────────────────

def configured_public_hosts(limit: int = 4) -> list[str]:
    """net_policy 已登记的**公网**端点主机（环回/本地服务不进来——它们不需要出网）。"""
    try:
        from net_policy import load_registry
        eps = load_registry().values()
    except Exception:
        return []
    out: list[str] = []
    for ep in eps:
        host = str(getattr(ep, "host", "") or "").lower()
        if not host or getattr(ep, "loopback_ok", False):
            continue
        if address_class(host) == "public":
            continue                     # 字面公网 IP 无需 DNS 诊断
        if host not in out:
            out.append(host)
        if len(out) >= limit:
            break
    return out


def egress_report(*, timeout: float = _RESOLVE_TIMEOUT,
                  cache_ttl: float | None = None, refresh: bool = False) -> dict:
    """聚合诊断：已登记公网端点 + 已知内容主机，逐个 DNS 分类，汇总状态与建议。

    `state`：ok / degraded / unavailable / unknown（unknown = 没有可诊断目标，
    不假装健康）。结果按 TTL 缓存（健康轮询复用）；`refresh=True` 强制重解析。
    """
    global _cache
    ttl = float(cache_ttl if cache_ttl is not None else _CACHE_TTL)
    now = time.time()
    if not refresh and _cache["report"] is not None and now - _cache["ts"] < ttl:
        rep = dict(_cache["report"])
        rep["cached"] = True
        return rep
    hosts: list[str] = []
    for h in configured_public_hosts() + list(KNOWN_CONTENT_HOSTS):
        if h not in hosts:
            hosts.append(h)
    hosts = hosts[:_MAX_TARGETS]
    # **整体时间预算**：逐目标超时只能保证单个不挂死；DNS 全挂时 N×timeout 会拖慢
    # `/api/status`。这里用一条总截止线，超了就不再解析（没解析的如实标注）。
    deadline = time.monotonic() + _REPORT_BUDGET
    targets: list[dict] = []
    for h in hosts:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        targets.append(classify_host(h, timeout=min(float(timeout), left)))
    budget_exhausted = len(targets) < len(hosts)
    counts: dict[str, int] = {}
    for t in targets:
        counts[t["category"]] = counts.get(t["category"], 0) + 1
    blocked = counts.get("reserved_address", 0) + counts.get("dns_error", 0)
    if not targets:
        state = "unknown"
        summary = "没有可诊断的公网目标（未登记公网端点）"
    elif blocked == len(targets):
        state = "unavailable"
        summary = f"全部 {len(targets)} 个已探测公网目标被保留段/解析失败挡住——这是出口问题，不是密钥或数据源问题"
    elif blocked:
        state = "degraded"
        summary = (f"{len(targets) - blocked}/{len(targets)} 个已探测目标解析正常，"
                   f"{blocked} 个被保留段/解析失败挡住")
    else:
        state = "ok"
        summary = f"全部 {len(targets)} 个已探测公网目标解析到公网地址"
    if budget_exhausted:
        summary += f"（时间预算用尽，另有 {len(hosts) - len(targets)} 个目标未探测）"
    advice: list[str] = []
    for t in targets:
        for a in t.get("advice") or ():
            if a not in advice:
                advice.append(a)
    try:
        from net_policy import proxy_settings_report
        egress_mode = proxy_settings_report()
    except Exception:
        egress_mode = {}
    rep = {
        "checked_at": now,
        "cached": False,
        "state": state,
        "summary": summary,
        "probed": len(targets),
        "counts": counts,
        "targets": targets,
        "advice": advice[:8],
        "egress_mode": egress_mode,
        "note": "只做 DNS 分类，不发 HTTP 请求；fake-IP 是特征描述，不指认具体代理软件",
    }
    _cache = {"ts": now, "report": rep}
    out = dict(rep)
    return out


def reset_cache_for_test() -> None:
    global _cache
    _cache = {"ts": 0.0, "report": None}
