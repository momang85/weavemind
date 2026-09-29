# -*- coding: utf-8 -*-
"""网络访问策略：按"可信来源与用途"授权（依《架构决策_身份与网络边界_20260915》）。

两类接口，互不重叠：

- `request_service(endpoint_id, ...)`：**登记端点**。由本机操作者（个人版）或授权管理员
  （银行版）登记用途、scheme、host、port、允许的操作/路径；模型、MCP、embedding、webhook
  各自登记。调用方**不能**自报 `trusted`/`allow_private` 提升权限——私网例外只绑定到登记的
  具体服务。登记的环回模型服务（如 `http://127.0.0.1:11434`）允许；同一个地址出现在网页或
  研究指令里仍然拒绝。
- `fetch_document(url, ...)`：**内容派生 URL**（资料正文、任务指令、模型输出、搜索结果）。
  只允许 http/https 公网内容，拒绝环回/私网/链路本地/共享/保留/非全局可路由地址与元数据服务，
  拒绝 URL 用户凭据；**解析失败即拒绝**；所有解析结果都必须合规。

共同约束：

- 默认**不跟随重定向**（避免"校验一个地址、连到另一个地址"）；将来若需支持，每一跳都要重新校验。
- 校验与连接使用**同一个已验 IP**（`_connect_pinned`），TLS 握手保留正确 SNI 与系统 CA 校验
  （不得为兼容关闭证书校验）。
- 请求有超时、响应体大小、重试上限；审计只记**脱敏目标**与策略判定。

本模块只做应用层策略；银行部署的网络出口限制另配，应用函数本身不能证明生产隔离。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 允许的协议
ALLOWED_SCHEMES = ("http", "https")
# 内容抓取：单次响应体上限（防资源耗尽）
MAX_BODY_BYTES = int(os.environ.get("WM_FETCH_MAX_BYTES", "") or 8 * 1024 * 1024)
DEFAULT_TIMEOUT = float(os.environ.get("WM_FETCH_TIMEOUT", "") or 20)
MAX_REDIRECTS = 0                      # 决策：默认关闭自动重定向
_KNOWN_METADATA_HOSTS = (
    "metadata.google.internal", "metadata", "instance-data",
)


class NetworkPolicyError(RuntimeError):
    """策略拒绝（未登记端点 / 内容派生 URL 触达内网 / 配置非法）。"""


class FetchError(RuntimeError):
    """抓取在执行层失败（连接、TLS、超时、响应过大）——与策略拒绝区分开。"""


# ── 出口方式（S2：显式区分继承代理 / 明确直连，不由异常分支擅自切换）──────
# inherit        跟随部署环境代理设置（默认；urllib 通道吃 HTTP(S)_PROXY）
# direct         明确不走代理（urllib 通道显式清空代理表）
# proxy_required 要求内容抓取必须经代理出口——本版本**明确不支持**：代理侧自行解析域名，
#                与"校验与连接共用同一个已验 IP"冲突，故宁可拒绝也不静默改道直连
CONTENT_FETCH_MODES = ("inherit", "direct", "proxy_required")
PROXY_EGRESS_SUPPORTED = False
_MODE_ENV = "WM_CONTENT_FETCH_MODE"
_config_cache: dict = {"mtime": None, "mode": ""}


def proxy_egress_supported() -> bool:
    """内容抓取能否经代理出口（其余接口不适用）。本版本为 False，且是显式声明。"""
    return bool(PROXY_EGRESS_SUPPORTED)


def require_egress_ok() -> None:
    """内容抓取的入口守卫：`proxy_required` 但本版本不支持经代理出口 → **拒绝**。

    每个内容入口（HTML/PDF/原始披露）都要在**发请求之前**调它（指令 §4-C0.3）：
    否则禁网环境里"必须代理但没代理"仍会直连下载一次——实机反例就是 PDF 那条。
    非 `proxy_required` 模式返回 None（不改动任何设置）。
    """
    if connection_mode() == "proxy_required" and not proxy_egress_supported():
        raise NetworkPolicyError(
            "内容抓取要求经代理出口，但本版本不支持该路径（代理侧自行解析域名，"
            "无法保证校验与连接使用同一个已验 IP）；请改用 direct 或 inherit")


def _config_content_fetch_mode() -> str:
    """config.json 的 `network.content_fetch.mode`（读不到回空串，不猜）。"""
    try:
        path = Path(_CONFIG_PATH)
        mtime = path.stat().st_mtime if path.exists() else 0.0
        if _config_cache["mtime"] == mtime:
            return _config_cache["mode"]
        mode = ""
        if mtime:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
            raw = ((data.get("network") or {}).get("content_fetch") or {}).get("mode")
            mode = str(raw or "").strip().lower()
        _config_cache.update({"mtime": mtime, "mode": mode})
        return mode
    except Exception:
        return ""


def connection_mode() -> str:
    """内容抓取的出口方式：`inherit`（跟随部署环境代理设置，默认）/ `direct`（明确不走代理）
    / `proxy_required`（要求经代理出口——本版本明确不支持，见 `require_egress_ok`）。

    只认显式配置：环境变量 `WM_CONTENT_FETCH_MODE` 优先于 `config.json` 的
    `network.content_fetch.mode`；非法值按 `inherit` 处理并记日志——不把"读不到配置"
    当成"用户要求直连"。
    """
    for source, raw in (("env", os.environ.get(_MODE_ENV, "")),
                        ("config", _config_content_fetch_mode())):
        mode = str(raw or "").strip().lower()
        if not mode:
            continue
        if mode in CONTENT_FETCH_MODES:
            return mode
        logger.warning("内容抓取出口方式取值非法（%s=%r），按 inherit 处理", source, mode)
    return "inherit"


def proxy_settings() -> dict:
    """当前部署环境的代理设置（**脱敏**：只回 host:port，不带用户凭据）。"""
    out: dict = {"configured": False, "hosts": []}
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                 "ALL_PROXY", "all_proxy"):
        raw = str(os.environ.get(name, "") or "").strip()
        if not raw:
            continue
        parts = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
        target = parts.hostname or ""
        if target:
            target = f"{target}:{parts.port}" if parts.port else target
        if target and target not in out["hosts"]:
            out["hosts"].append(target)
        out["configured"] = True
    return out


def _exception_chain(exc: BaseException) -> list:
    """异常链（含 `__cause__`/`__context__`/`URLError.reason`）：urllib 把底层连接错误装在
    `URLError.reason` 里，只看 `__cause__` 会把"代理拒连"读成没有细节的 URLError。"""
    out: list = []
    seen: set = set()
    queue = [exc]
    while queue and len(out) < 10:
        cur = queue.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        out.append(cur)
        for nxt in (getattr(cur, "__cause__", None),
                    getattr(cur, "__context__", None),
                    getattr(cur, "reason", None)):
            if isinstance(nxt, BaseException):
                queue.append(nxt)
    return out


def _is_connect_stage_failure(exc: BaseException) -> bool:
    """失败是否发生在**连接建立阶段**（不是"已建连、正在说 HTTP 时断了"）。

    已拿到 HTTP 状态、或已建连后被重置都不算——那说明请求确实出去了，归源站侧更诚实。
    """
    import http.client as _http
    chain = _exception_chain(exc)
    if any(isinstance(e, (_http.HTTPException, urllib.error.HTTPError)) for e in chain):
        return False
    if any(isinstance(e, ConnectionResetError) for e in chain):
        return False
    return any(isinstance(e, (ConnectionRefusedError, socket.gaierror,
                              socket.timeout, TimeoutError)) for e in chain)


def is_proxy_failure(exc: BaseException) -> bool:
    """该失败是否发生在**代理层**——是则调用方不得降级为直连（专项 §6）。

    判定：HTTP 407；异常文本含 proxy；**配置了代理且失败在连接阶段**（链上没有 HTTP 状态
    错误、也不是已建连后被重置）。第三条是主力：代理进程没起来时 urllib 抛的是
    `URLError(ConnectionRefusedError(...))`，文本里根本没有 "proxy" 字样。
    """
    chain = _exception_chain(exc)
    for e in chain:
        if isinstance(e, urllib.error.HTTPError) and int(getattr(e, "code", 0) or 0) == 407:
            return True
    text = " ".join(f"{type(e).__name__}: {e}" for e in chain).lower()
    if "proxy" in text:
        return True
    if not proxy_settings()["configured"]:
        return False
    return _is_connect_stage_failure(exc)


def proxy_settings_report() -> dict:
    """诊断用：报告当前出口方式与（脱敏的）代理设置，**不修改任何环境**。

    只报不动的理由（指令 §4-C0.3）：内容下载不得为了自己的出口去清理整进程代理配置，
    那会连带改变其它服务的出口。要"明确直连"就设 `WM_CONTENT_FETCH_MODE=direct`，
    由各内容入口按 mode 执行。
    """
    proxy = proxy_settings()
    return {"mode": connection_mode(), "proxy_configured": bool(proxy["configured"]),
            "proxy_hosts": list(proxy["hosts"]),
            "proxy_egress_supported": proxy_egress_supported()}


def classify_network_error(exc: BaseException) -> str:
    """网络异常 → 与专项 §4 同一套诊断类别（代理失败单列 `proxy_error`）。"""
    if is_proxy_failure(exc):
        return "proxy_error"
    try:
        import search_diag
        return search_diag.classify_error(exc)
    except Exception:
        name = type(exc).__name__
        text = f"{name}: {exc}".lower()
        if isinstance(exc, (ModuleNotFoundError, ImportError)):
            return "missing_dependency"
        if "timed out" in text or "timeout" in text:
            return "timeout"
        if "getaddrinfo" in text or "nodename" in text:
            return "dns_error"
        return "parse_error"


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""
    # 已验 IP（校验与连接共用，避免二次解析竞态）
    resolved: tuple[str, ...] = ()
    url: str = ""
    kind: str = ""          # "public" | "registered"
    redacted: str = ""      # 审计用的脱敏目标

    def __bool__(self) -> bool:      # 便于 `if decision:`
        return self.ok


# ── 端点登记表 ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Endpoint:
    endpoint_id: str
    purpose: str                    # model / mcp / embedding / webhook / lora ...
    scheme: str
    host: str
    port: int = 0
    path_prefix: str = ""
    operations: tuple[str, ...] = ()
    loopback_ok: bool = False       # 仅登记为本地服务时为真（例如本机 Ollama/LoRA）

    def base_url(self) -> str:
        netloc = self.host if not self.port else f"{self.host}:{self.port}"
        return f"{self.scheme}://{netloc}{self.path_prefix or ''}"


_REGISTRY: dict[str, Endpoint] = {}
_REGISTRY_LOADED = False
_CONFIG_PATH = os.environ.get("WEAVEMIND_CONFIG") or str(Path(__file__).resolve().parent / "config.json")


def _endpoint_from_dict(raw: dict) -> Endpoint | None:
    try:
        eid = str(raw.get("id") or "").strip()
        purpose = str(raw.get("purpose") or "").strip()
        if not eid or not purpose:
            return None
        parts = urllib.parse.urlsplit(str(raw.get("url") or ""))
        if parts.scheme not in ALLOWED_SCHEMES or not parts.hostname:
            return None
        return Endpoint(
            endpoint_id=eid, purpose=purpose, scheme=parts.scheme,
            host=parts.hostname.lower(), port=int(parts.port or 0),
            path_prefix=str(parts.path or "").rstrip("/"),
            operations=tuple(str(o) for o in (raw.get("operations") or ())),
            loopback_ok=bool(raw.get("loopback")),
        )
    except Exception:
        return None


def load_registry(force: bool = False) -> dict[str, Endpoint]:
    """读登记端点：`config.json` 的 `network.endpoints`（运维/管理员维护）。

    个人单用户安装由本机操作者配置；银行版由授权管理员配置并审计。配置里的地址**不代表**
    可信——是否允许私网只由登记项自身的用途与 `loopback` 标记决定。
    """
    global _REGISTRY, _REGISTRY_LOADED
    if _REGISTRY_LOADED and not force:
        return _REGISTRY
    reg: dict[str, Endpoint] = {}
    try:
        data = json.loads(Path(_CONFIG_PATH).read_text(encoding="utf-8"))
        for raw in ((data.get("network") or {}).get("endpoints") or []):
            ep = _endpoint_from_dict(raw if isinstance(raw, dict) else {})
            if ep:
                reg[ep.endpoint_id] = ep
    except Exception as exc:
        logger.debug("网络端点登记表读取失败（按空表处理）：%s", exc)
    _REGISTRY, _REGISTRY_LOADED = reg, True
    return reg


def register_endpoint(ep: Endpoint) -> None:
    """运行时登记（例如启动向导写入配置后立即生效）。会锁定登记表，避免被惰性加载覆盖。"""
    global _REGISTRY_LOADED
    _REGISTRY[ep.endpoint_id] = ep
    _REGISTRY_LOADED = True


def reset_registry_for_test() -> None:
    global _REGISTRY, _REGISTRY_LOADED
    _REGISTRY, _REGISTRY_LOADED = {}, False


# ── 地址判定 ──────────────────────────────────────────────────────

def _ip_is_public(ip) -> bool:
    """是否全局可路由且非保留：覆盖环回/私网/链路本地/共享(CGNAT)/保留/组播/未指定。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped                     # ::ffff:127.0.0.1 等映射地址按内部地址看
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast:
        return False
    if ip.is_reserved or ip.is_unspecified:
        return False
    return bool(getattr(ip, "is_global", False))


def _parse_ip(text: str):
    try:
        return ipaddress.ip_address(str(text).strip().strip("[]"))
    except ValueError:
        return None


def _redact(url: str) -> str:
    """审计用脱敏目标：去掉查询串与用户凭据，只留 scheme://host[:port]/path。"""
    try:
        p = urllib.parse.urlsplit(url)
        netloc = p.hostname or ""
        if p.port:
            netloc = f"{netloc}:{p.port}"
        return urllib.parse.urlunsplit((p.scheme, netloc, p.path, "", ""))
    except Exception:
        return "<unparsable>"


def _resolve_all(host: str, port: int) -> tuple[list[str], str]:
    """解析主机的全部地址。解析失败或空结果都返回错误原因（**失败即拒绝**）。"""
    try:
        infos = socket.getaddrinfo(host, port or None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return [], f"DNS 解析失败：{exc}"
    ips: list[str] = []
    for info in infos:
        if info[0] in (socket.AF_INET, socket.AF_INET6):
            addr = str(info[4][0])
            if addr not in ips:
                ips.append(addr)
    if not ips:
        return [], "DNS 未返回可用地址"
    return ips, ""


# ── 内容派生 URL ─────────────────────────────────────────────────

def validate_public_url(url: str) -> Decision:
    """内容派生 URL 的严格校验（替代"解析失败也放行"的旧实现）。

    判定：协议 http/https、无用户凭据、host 非 localhost 变体/元数据服务、解析成功、
    **全部**解析地址都是公网地址（含 IPv4/IPv6 与映射地址归一化）。
    """
    red = _redact(url)
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except Exception:
        return Decision(False, "URL 无法解析", redacted=red)
    if parts.scheme not in ALLOWED_SCHEMES:
        return Decision(False, f"协议不允许：{parts.scheme or '(空)'}", redacted=red)
    if parts.username or parts.password:
        return Decision(False, "URL 不得携带用户凭据", redacted=red)
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return Decision(False, "缺少主机名", redacted=red)
    if host == "localhost" or host.endswith(".localhost") or host in _KNOWN_METADATA_HOSTS:
        return Decision(False, f"目标是本机/元数据服务：{host}", redacted=red)

    literal = _parse_ip(host)
    if literal is not None:
        ips, err = [str(literal)], ""
    else:
        ips, err = _resolve_all(host, parts.port or 0)
    if err:
        return Decision(False, err, redacted=red)
    bad = [a for a in ips if not _ip_is_public(_parse_ip(a))]
    if bad:
        return Decision(False, f"解析到非公网地址：{','.join(bad)}", redacted=red)
    return Decision(True, "", resolved=tuple(ips), url=url, kind="public", redacted=red)


# ── 登记端点 ─────────────────────────────────────────────────────

def request_service(endpoint_id: str, operation: str = "", **kwargs) -> Decision:
    """登记端点的解析与授权。返回 `Decision`（含已验地址与 base_url）。

    调用方**不能**用 kwargs 提升权限：`trusted` / `allow_private` / `allow_internal`
    一律拒绝；私网例外只来自登记项自身（`loopback` 标记 + 用途）。
    """
    for forbidden in ("trusted", "allow_private", "allow_internal"):
        if kwargs.pop(forbidden, None):
            return Decision(False, f"调用方不得自报 {forbidden}（私网例外只由登记项授予）")
    key = str(endpoint_id or "").strip()
    ep = load_registry().get(key)
    if ep is None:
        return Decision(False, f"未登记的端点：{key or '(空)'}", redacted=f"endpoint:{key}")
    if operation and ep.operations and operation not in ep.operations:
        return Decision(False, f"端点 {key} 不允许操作 {operation}", redacted=f"endpoint:{key}")

    literal = _parse_ip(ep.host)
    if literal is not None:
        if not _ip_is_public(literal) and not ep.loopback_ok:
            return Decision(False, f"端点 {key} 指向内部地址但未标记为本地服务",
                            redacted=f"endpoint:{key}")
        return Decision(True, "", resolved=(str(literal),), url=ep.base_url(),
                        kind="registered", redacted=f"endpoint:{key}")

    ips, err = _resolve_all(ep.host, ep.port)
    if err:
        return Decision(False, f"端点 {key} 解析失败：{err}", redacted=f"endpoint:{key}")
    if not ep.loopback_ok:
        bad = [a for a in ips if not _ip_is_public(_parse_ip(a))]
        if bad:
            return Decision(False, f"端点 {key} 解析到非公网地址：{','.join(bad)}",
                            redacted=f"endpoint:{key}")
    return Decision(True, "", resolved=tuple(ips), url=ep.base_url(),
                    kind="registered", redacted=f"endpoint:{key}")


def require_service(endpoint_id: str, operation: str = "") -> str:
    """登记端点的 base_url（不通过即抛 `NetworkPolicyError`）。供客户端调用点使用。"""
    d = request_service(endpoint_id, operation)
    if not d.ok:
        raise NetworkPolicyError(d.reason)
    return d.url


def fetch_text(url: str, *, timeout: float | None = None, encoding: str = "utf-8",
               max_bytes: int | None = None, headers: dict | None = None,
               method: str = "GET", body: bytes | str | None = None,
               content_type: str = "") -> str:
    """`fetch_document` 的文本形态（按调用方给的 encoding 解码）。

    给"明确直连"的适配器用：它们本来直接调 urllib（会吃掉环境代理），改调本函数即
    走同一条已验 IP 通道，不需要各自再拼请求。`method`/`body` 原样透传。
    """
    got = fetch_document(url, timeout=timeout, max_bytes=max_bytes, headers=headers,
                         method=method, body=body, content_type=content_type)
    return bytes(got.get("raw") or b"").decode(encoding, errors="replace")


def connect_validated(ip: str, port: int, host: str, scheme: str,
                      timeout: float) -> socket.socket:
    """连接到 `validate_public_url` 给出的**已验 IP**（供各传输通道复用同一条路径）。"""
    return _connect_pinned(ip, port, host, scheme, timeout)


# ── 受约束的抓取通道 ─────────────────────────────────────────────

def _connect_pinned(ip: str, port: int, host: str, scheme: str, timeout: float) -> socket.socket:
    """连接到**已验 IP**；https 保留对外 Host/SNI 与系统 CA 校验。"""
    sock = socket.create_connection((ip, port), timeout=timeout)
    if scheme == "https":
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    return sock


def fetch_document(url: str, *, timeout: float | None = None,
                   max_bytes: int | None = None, headers: dict | None = None,
                   method: str = "GET", body: bytes | str | None = None,
                   content_type: str = "") -> dict:
    """抓取内容派生 URL：先严格校验，再用**已验 IP** 连接；不跟随重定向。

    返回 `{"status","url","headers","text","bytes","egress"}`；策略拒绝抛
    `NetworkPolicyError`，执行失败（含重定向、超限、超时）抛 `FetchError`。
    请求不携带任何调用方凭据。

    `method`/`body` 只为两类官方端点准备：公告查询接口是 `POST` 表单，正文才是 `GET`。
    方法与体积都收在这里，不另外开一个"能发任意请求"的散口——出域校验、已验 IP 直连、
    不跟随跳转、不带凭据这四条对两类请求一视同仁。

    **读取在关闭之前**（2026-09-29 修）：响应必须在 socket 还开着的时候读，
    关掉之后再解析只会得到 `WinError 10038`（"在非套接字上尝试了一个操作"）——
    A1 重做时把"先收字节、后关连接、再手拆头"的老顺序留下了，而解析已经换成
    `http.client`，结果整条通道在生产上恒失败。假替身的 `close()` 是空操作，
    所以离线用例全绿也没发现；替身已按真实生命周期重做（关后即报 10038）。

    出口方式（专项 §6）：本通道**用已验 IP 直连，经代理出口在本版本不支持**——代理侧
    自行解析域名，与"校验与连接共用同一个已验 IP"冲突。因此：

    - `inherit`（默认）且环境里配了代理 → 结果里显式标 `egress="direct_pinned"`，并记日志
      说明"未使用环境代理"；不假装请求走了代理；
    - `direct` → 同上（直连是操作者明确选的）；
    - `proxy_required` → **拒绝**（`NetworkPolicyError`），不静默改道直连：这是"做不到时
      明确不支持该路径"的落地方式。

    即：**本通道从不因代理不可用而改走直连，也从不把直连说成代理**。
    """
    require_egress_ok()
    decision = validate_public_url(url)
    audit_decision(decision, action="fetch_document")
    if not decision.ok:
        raise NetworkPolicyError(decision.reason)

    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    budget = float(timeout or DEFAULT_TIMEOUT)
    cap = int(max_bytes or MAX_BODY_BYTES)
    method_up = str(method or "GET").upper()
    if method_up not in ("GET", "POST"):
        raise ValueError(f"内容抓取只支持 GET/POST，收到 {method!r}")
    payload = b""
    if method_up == "POST":
        payload = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
    mode = connection_mode()
    proxy = proxy_settings()
    egress = "direct_pinned"
    if proxy["configured"] and mode != "direct":
        logger.info("内容抓取走已验 IP 直连（本版本不支持经代理出口）；环境代理=%s 未使用",
                    ",".join(proxy["hosts"]) or "?")
    # **根截止**：地址尝试与重试共用一条线，不每次续期（否则 N 个地址 = N 倍预算）
    root_deadline = time.monotonic() + budget

    def _remaining() -> float:
        return root_deadline - time.monotonic()

    last_error = ""
    for ip in decision.resolved:
        remain = _remaining()
        if remain <= 0:
            raise FetchError(f"总截止 {budget:g}s 已用尽，未试完 {len(decision.resolved)} 个地址")
        try:
            sock = _connect_pinned(ip, port, host, parts.scheme, remain)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        try:
            hdrs = {"Host": host, "User-Agent": "WeaveMind/1.0 (+policy:public-only)",
                    "Connection": "close", "Accept": "*/*"}
            for k, v in (headers or {}).items():
                if str(k).lower() in ("authorization", "cookie", "x-api-key"):
                    continue        # 内容抓取不带任何凭据
                hdrs[str(k)] = str(v)
            if method_up == "POST":
                hdrs.setdefault("Content-Type",
                                str(content_type or "application/x-www-form-urlencoded"))
                hdrs["Content-Length"] = str(len(payload))
            request = (f"{method_up} {path} HTTP/1.1\r\n"
                       + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
                       + "\r\n").encode("utf-8", "replace") + payload
            sock.sendall(request)
            remain = _remaining()
            if remain <= 0:
                raise FetchError(f"总截止 {budget:g}s 在发出请求后用尽")
            # 不预读、不提前关闭：解析器自己按截止线读，读完（或报错）才关连接
            return _read_http_response(sock, url=url, egress=egress, cap=cap,
                                       budget=remain)
        except FetchError:
            raise
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        finally:
            try:
                sock.close()
            except Exception:
                pass
    raise FetchError(last_error or "连接失败")


def _gunzip_bounded(raw: bytes, cap: int) -> bytes:
    """有界 gunzip：逐块解压，任一时刻的输出都不超过 `cap`（超了抛 FetchError）。

    为什么不能用 `gzip.decompress`：它先把整包解开再让调用方检查上限——压缩炸弹
    （几 MiB 压出几 GiB）会在检查之前就把内存吃掉。`zlib.decompressobj(16+MAX_WBITS)`
    用 `max_length` 限制**单次**产出，循环累加并在每次之后查上限。
    """
    import zlib

    obj = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out: list[bytes] = []
    total = 0
    data = raw
    while data:
        chunk = obj.decompress(data, max(1, cap + 1 - total))
        data = obj.unconsumed_tail
        total += len(chunk)
        if total > cap:
            raise FetchError(f"解压后超过上限 {cap} 字节（已在解压过程中停止）")
        out.append(chunk)
    tail = obj.flush()
    if total + len(tail) > cap:
        raise FetchError(f"解压后超过上限 {cap} 字节（收尾阶段）")
    out.append(tail)
    return b"".join(out)


def _read_http_response(sock, *, url: str, egress: str, cap: int, budget: float) -> dict:
    """把已连上的 socket 读成一个 HTTP 响应（**成熟实现**，不再手拆头）。

    旧实现 `raw.partition(b"\r\n\r\n")` 之后把 body 直接当正文：**chunked 响应的分块框架
    会被当成内容**（PDF 直接损坏），gzip 也没解。这里用 `http.client.HTTPResponse` 解析
    状态行/头并透明解 chunked，正文读取走 `read_with_deadline`（每次底层读都查钟 + 总量上限）。

    保留既有安全语义：连接仍是**已验 IP 直连**（调用方 pin），3xx 不跟随，不带凭据。
    **时钟同源**：截止用 `time.monotonic()` 起算（`read_with_deadline` 也用单调钟；
    若拿调用方的墙钟 `time.time()` 相减会得到几十亿秒，总截止形同虚设）。
    """
    import http.client

    from adapters.search_runner import (
        ResponseTooLarge, _close_quietly, _wrap_response_deadline, read_with_deadline,
    )

    deadline = time.monotonic() + float(budget)
    resp = http.client.HTTPResponse(sock)
    # **响应头也吃同一条截止线**（K0-c，2026-09-29 夜验收）：`begin()` 解析状态行/头时是
    # `fp.readline()` 连续 recv，socket 超时只按"单次操作"算——慢头（每 5ms 一字节）能把
    # 50ms 预算拖到 525ms。先把 `resp.fp` 包一层，让时钟检查落进**头解析的每一次底层读**。
    _wrapped, _restore = _wrap_response_deadline(resp, deadline)
    try:
        resp.begin()
    except (TimeoutError, socket.timeout) as exc:
        _close_quietly(resp)
        raise FetchError(f"读取超时（总截止 {budget:g}s，响应头未读完）：{str(exc)[:80]}")
    except Exception as exc:                          # noqa: BLE001
        _close_quietly(resp)
        raise FetchError(f"响应解析失败：{type(exc).__name__}: {str(exc)[:120]}")
    status = int(getattr(resp, "status", 0) or 0)
    hdrs = {str(k).lower(): str(v) for k, v in (resp.getheaders() or [])}
    if 300 <= status < 400:
        raise FetchError(f"目标返回重定向（{status}），按策略不自动跟随")
    try:
        body = read_with_deadline(resp, deadline, max_bytes=cap)
    except ResponseTooLarge:
        raise FetchError(f"响应体超过上限 {cap} 字节")
    except TimeoutError as exc:
        raise FetchError(f"读取超时（总截止 {budget:g}s）：{str(exc)[:80]}")
    except Exception as exc:                          # noqa: BLE001
        raise FetchError(f"读取失败：{type(exc).__name__}: {str(exc)[:120]}")
    if str(hdrs.get("content-encoding") or "").lower() == "gzip":
        # **有界解压**（K0-c）：`gzip.decompress` 会先把整包解开再让我们检查上限——
        # 一个 8 MiB 的压缩炸弹能先展开成几 GB。这里逐块解、每块都查上限，超了立刻停。
        try:
            body = _gunzip_bounded(body, cap)
        except FetchError:
            raise
        except Exception as exc:                      # noqa: BLE001
            raise FetchError(f"gzip 解码失败：{type(exc).__name__}: {str(exc)[:80]}")
    charset = "utf-8"
    ctype = str(hdrs.get("content-type") or "")
    if "charset=" in ctype:
        charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
    return {"status": status, "url": url, "headers": hdrs, "bytes": len(body),
            "raw": body, "egress": egress,
            "transfer_encoding": str(hdrs.get("transfer-encoding") or ""),
            "text": body.decode(charset, "replace")}


# ── 审计（只记脱敏目标与策略判定）──────────────────────────────────

def audit_decision(decision: Decision, action: str = "", actor: str = "") -> None:
    try:
        from audit_logger import audit_log
        audit_log(
            user=actor or "system", ip="-", action=action or "net_policy",
            target=decision.redacted, result="allow" if decision.ok else "deny",
            detail=(decision.kind or "") + (f" | {decision.reason}" if decision.reason else ""),
        )
    except Exception:
        logger.info("net_policy %s %s -> %s (%s)", action, decision.redacted,
                    "allow" if decision.ok else "deny", decision.reason)

