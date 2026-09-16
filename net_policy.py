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


# ── 受约束的抓取通道 ─────────────────────────────────────────────

def _connect_pinned(ip: str, port: int, host: str, scheme: str, timeout: float) -> socket.socket:
    """连接到**已验 IP**；https 保留对外 Host/SNI 与系统 CA 校验。"""
    sock = socket.create_connection((ip, port), timeout=timeout)
    if scheme == "https":
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    return sock


def fetch_document(url: str, *, timeout: float | None = None,
                   max_bytes: int | None = None, headers: dict | None = None) -> dict:
    """抓取内容派生 URL：先严格校验，再用**已验 IP** 连接；不跟随重定向。

    返回 `{"status","url","headers","text","bytes"}`；策略拒绝抛 `NetworkPolicyError`，
    执行失败（含重定向、超限、超时）抛 `FetchError`。请求不携带任何调用方凭据。
    """
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

    last_error = ""
    for ip in decision.resolved:
        try:
            sock = _connect_pinned(ip, port, host, parts.scheme, budget)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        raw = b""
        try:
            hdrs = {"Host": host, "User-Agent": "WeaveMind/1.0 (+policy:public-only)",
                    "Connection": "close", "Accept": "*/*"}
            for k, v in (headers or {}).items():
                if str(k).lower() in ("authorization", "cookie", "x-api-key"):
                    continue        # 内容抓取不带任何凭据
                hdrs[str(k)] = str(v)
            req = (f"GET {path} HTTP/1.1\r\n"
                   + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n")
            sock.sendall(req.encode("utf-8", "replace"))
            started = time.time()
            while len(raw) < cap:
                if time.time() - started > budget:
                    raise FetchError("读取超时")
                chunk = sock.recv(min(65536, cap - len(raw)))
                if not chunk:
                    break
                raw += chunk
            if len(raw) >= cap:
                raise FetchError(f"响应体超过上限 {cap} 字节")
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

        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1", "replace").split("\r\n")
        try:
            status = int((lines[0] if lines else "").split(" ")[1])
        except Exception:
            status = 0
        if 300 <= status < 400:
            # 默认不跟随重定向：把目标交回调用方显式决策（每一跳都要重新校验）
            raise FetchError(f"目标返回重定向（{status}），按策略不自动跟随")
        resp_headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                resp_headers[k.strip().lower()] = v.strip()
        charset = "utf-8"
        ctype = resp_headers.get("content-type", "")
        if "charset=" in ctype:
            charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        return {"status": status, "url": url, "headers": resp_headers,
                "bytes": len(body), "raw": body, "text": body.decode(charset, "replace")}
    raise FetchError(last_error or "连接失败")


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

