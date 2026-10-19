# -*- coding: utf-8 -*-
"""织光 (ZhiGuang) — LLM 端点熔断器（任务23，docs/arch_phase4_task23_llm_failover.md）

要解决的可用性瓶颈：主模型端点持续故障时，系统只会"每步重试到耗尽后标 FAILED"，
没有任何人按"连续失败"这个口径把调用切到备援端点——单点 LLM 失效 = 全线停摆。

状态三档（架构师指定，不得自行调整口径）：

    CLOSED    正常：所有调用打到 primary
      │ 主端点连续 fail_threshold 次**端点类**失败
      ▼
    OPEN      熔断：按 backup_endpoints 列表顺序依次尝试（当前备援也连续失败到
              阈值 → 换下一个；没有下一个就留在最后一个）
      │ 持续 reset_timeout 秒后，下一次真实调用打回 primary 探测
      ▼
    HALF_OPEN 探测窗口：活跃端点回到 primary
      ├ 探测成功 → CLOSED（并恢复本进程 LLM_* 环境变量）
      └ 探测失败 → OPEN（重新计时，继续用当前备援）

架构约束（逐条落地）：
- **进程内单例**（与 `redis_health` 的降级标志同一层），不写 DB：状态存在本模块的
  `_SINGLETON` 里，每个进程自己记账、自己切换。
- **`backup_endpoints` 为空 → 零影响**：没有可用备援时状态机永远停在 CLOSED，
  `activate_for_call()` 一个字都不写 `os.environ`，调用形状与改动前完全一致。
- **`model_ctx` / `model_deny` 不计入熔断**：这两类是"请求本身的问题"（上下文超限、
  内容拒绝），换端点也一样失败，计入就会把一次坏请求误判成端点故障。同理
  `cancelled`（用户取消）与 `budget_exhausted`（本地预算拒发）也不计——请求根本没发出去。

跨进程可见性（步骤 3、4 需要的"guardian/健康页读到熔断状态"）：
worker 进程才是真正发起模型调用的地方，它的熔断状态别的进程读不到。这里沿用
`_check_routing_health` 的先例（别的进程写产物，guardian 只读）：状态**变化**时把快照
best-effort 镜像到 Redis 键 `llm:circuit:{service}`（带 TTL，进程死了自动过期）。
Redis 只是**观测镜像**，不是真源——真源始终是发起调用那个进程里的单例。

端点切换怎么生效：`llm_client` 每次调用前都从 `os.environ` 读 `LLM_BASE_URL` /
`LLM_API_KEY` / `LLM_MODEL`，所以 `activate_for_call()` 把这三个变量指向当前活跃端点，
本进程后续的模型调用就走到备援上；回到 CLOSED/探测窗口时再写回主端点。
配套 `active_model_override()`：`llm_client.get_model_for_usage()` 里 `llm.model_roles[用途]`
优先于 `LLM_MODEL`，只改环境变量换不掉模型名，落到备援上会 404；所以在备援期间把该
备援配置的模型名作为 `model_override` 传下去（与 `llm_client._async_call_backup` 同口径）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("llm_circuit_breaker")

STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"

PRIMARY_NAME = "primary"

# 架构师指定的默认阈值：连续 5 次失败熔断、60s 后探测恢复
DEFAULT_FAIL_THRESHOLD = 5
DEFAULT_RESET_TIMEOUT = 60.0

# 不计入熔断的错误类别（见模块 docstring：这些不是 endpoint 问题）
NON_ENDPOINT_ERRORS = frozenset({"model_ctx", "model_deny", "cancelled", "budget_exhausted"})

# llm_client 在每次调用前读取的环境变量：切换端点=改写这三个键
ENV_KEYS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")

_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# 跨进程观测镜像（不是真源）
STATE_KEY_PREFIX = "llm:circuit:"
STATE_TTL = 300

_HTTP_CODE_RE = re.compile(r"http[ _-]?(\d{3})")   # 匹配的是小写化后的异常文本


# ---------------------------------------------------------------------------
# 错误分类：什么算"端点故障"
# ---------------------------------------------------------------------------

def classify_endpoint_error(exc: BaseException) -> str:
    """模型调用异常 → 错误类别（与 `worker_base.classify_model_error` 同一套词表）。

    与 worker_base 的关键词表有一处**刻意的差别**：连接/超时类先判 `network`。
    `ConnectionRefusedError` 的类名里含 "refuse"，照关键词表会被归成 `model_deny`
    而**不计入熔断**——那正是端点挂了最典型的样子，漏判等于熔断器对宕机无感。
    """
    if exc is None:
        return "generic"
    if getattr(exc, "budget_exhausted", False):
        return "budget_exhausted"
    name = type(exc).__name__
    if "cancel" in name.lower():
        return "cancelled"
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) and not isinstance(
            exc, (ValueError, RuntimeError)):
        # OSError 家族（含 socket.timeout/gaierror）都是可达性问题
        return "network"
    text = f"{name}: {exc}".lower()
    if "timeout" in text or "timed out" in text or "connection" in text \
            or "connect" in text or "refused" in text or "reset by peer" in text \
            or "unreachable" in text or "gaierror" in text or "ssl" in text:
        return "network"
    if "rate" in text or "429" in text:
        return "rate_limit"
    if "context" in text or "too long" in text:
        return "model_ctx"
    if "refuse" in text or "content" in text or "policy" in text:
        return "model_deny"
    m = _HTTP_CODE_RE.search(text)
    if m:
        return f"http_{m.group(1)}"
    return "generic"


def counts_for_circuit(error_class: str) -> bool:
    """该错误类别是否计入熔断（False=请求问题，换端点也没用）。"""
    return str(error_class or "") not in NON_ENDPOINT_ERRORS


# ---------------------------------------------------------------------------
# 配置解析：primary + backup_endpoints
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """读 config.json（读不到返回空字典：熔断器不得成为启动依赖）。"""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:                                   # noqa: BLE001 - 配置缺失=无备援
        return {}


def _endpoint(name: str, raw: Any) -> dict:
    """配置项 → 端点字典（裸字符串视作 base_url；dict 取 name/base_url/api_key/model）。"""
    data = {"base_url": raw} if isinstance(raw, str) else dict(raw or {})
    return {
        "name": str(data.get("name") or name),
        "base_url": str(data.get("base_url") or "").strip(),
        "api_key": str(data.get("api_key") or "").strip(),
        "model": str(data.get("model") or "").strip(),
    }


def _is_placeholder(*values: str) -> bool:
    """占位符（模板里的 YOUR_API_KEY 之类）不算"配了端点"——与 llm_client 同一口径。"""
    try:
        from setup_wizard import looks_placeholder
    except Exception:                                   # noqa: BLE001 - 装不上就不过滤
        return False
    return any(looks_placeholder(str(v or "")) for v in values)


def _usable(ep: dict) -> bool:
    """备援端点必须**真配了**地址与密钥才可用，否则切过去只是多一次失败请求。"""
    if not ep.get("base_url") or not ep.get("api_key"):
        return False
    return not _is_placeholder(ep["base_url"], ep["api_key"])


def load_primary(cfg: dict | None = None) -> dict:
    """主端点：config.json 的 llm 段，缺失时回退到当前进程环境变量。"""
    cfg = load_config() if cfg is None else cfg
    llm = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    ep = _endpoint(PRIMARY_NAME, llm)
    if not ep["base_url"]:
        ep["base_url"] = str(os.environ.get("LLM_BASE_URL") or "").strip()
        ep["api_key"] = ep["api_key"] or str(os.environ.get("LLM_API_KEY") or "").strip()
        ep["model"] = ep["model"] or str(os.environ.get("LLM_MODEL") or "").strip()
    return ep


def load_backups(cfg: dict | None = None) -> list[dict]:
    """备援端点列表：`backup_endpoints`（有序）优先，缺失时退到既有单端点 `backup` 段。

    设计文档写的是"`config.json` 里有 `backup_endpoints` 配置"，但仓库里的配置至今只有
    **单个** `backup` 段（`{base_url, api_key, model}`，由 llm_client 的 O-29 健康路由使用）。
    两处都认：有 `backup_endpoints` 就按列表顺序依次尝试，只有 `backup` 就当一元素列表——
    否则本任务在这个仓库里是死代码。一个都没配 → 空列表 → 熔断器不改变任何行为。
    """
    cfg = load_config() if cfg is None else cfg
    raw = cfg.get("backup_endpoints")
    if not isinstance(raw, list) or not raw:
        single = cfg.get("backup")
        raw = [single] if isinstance(single, dict) else []
    out: list[dict] = []
    for item in raw:
        if item is None:
            continue
        ep = _endpoint(f"backup_{len(out) + 1}", item)   # 只按**可用**端点编号
        if _usable(ep):
            out.append(ep)
    return out


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

class LLMCircuitBreaker:
    """三态熔断器（纯内存、线程安全、可注入时钟与状态回调，便于测试）。

    Args:
        primary: 主端点（dict 或 base_url 字符串）。
        backups: 备援端点列表，按顺序依次尝试。空列表=不启用切换。
        fail_threshold: 连续多少次端点类失败触发熔断（架构师指定 5）。
        reset_timeout: OPEN 持续多少秒后进入探测窗口（架构师指定 60）。
        now_fn: 时钟（测试注入假时钟）。
        state_sink: 状态变化回调（None=模块级 Redis 镜像 `_publish_state`）。
    """

    def __init__(self, primary: Any, backups: Any, fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
                 reset_timeout: float = DEFAULT_RESET_TIMEOUT, *,
                 now_fn: Callable[[], float] = time.monotonic,
                 state_sink: Callable[[dict], None] | None = None,
                 service: str = "") -> None:
        self._primary = _endpoint(PRIMARY_NAME, primary)
        # 备援统一过 _endpoint 规范化（裸字符串与 dict 都接受），名字按列表顺序 backup_1..n
        self._backups = [
            _endpoint(f"backup_{i}", b) for i, b in enumerate(backups or [], start=1)
            if b is not None
        ]
        self._fail_threshold = max(1, int(fail_threshold))
        self._reset_timeout = max(0.0, float(reset_timeout))
        self._now = now_fn
        self._sink = state_sink
        self.service = str(service or "")
        self._lock = threading.RLock()
        self._state = STATE_CLOSED
        self._primary_failures = 0      # 主端点连续失败次数（健康接口展示的就是它）
        self._backup_failures = 0       # 当前备援连续失败次数（驱动"换下一个备援"）
        self._index = 0                 # 当前备援下标（仅 OPEN/HALF_OPEN 有意义）
        self._opened_at = 0.0           # 进入 OPEN 的时刻（探测窗口的起点）
        self._switches = 0              # 本进程累计切换次数（含备援之间互切）
        self._applied = ""              # 最近一次写进 os.environ 的端点名（幂等判定）
        self._applied_env: dict | None = None    # 最近一次写入的 LLM_* 值（漂移检测）
        self._env_baseline: dict | None = None   # 首次覆盖前的 LLM_* 原值

    # -- 基本属性 ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """配了可用备援才有意义：没配就退化为原有行为（零影响约束）。"""
        return bool(self._backups)

    @property
    def fail_threshold(self) -> int:
        return self._fail_threshold

    @property
    def reset_timeout(self) -> float:
        return self._reset_timeout

    # -- 记账 --------------------------------------------------------------

    def record_failure(self, error_class: str = "") -> None:
        """一次调用失败。请求问题（model_ctx/model_deny/取消/预算）直接返回不计。"""
        if not counts_for_circuit(error_class):
            return
        notify = False
        with self._lock:
            if self._state == STATE_HALF_OPEN:
                # 探测打的是主端点：失败=主端点仍未恢复，回到 OPEN 并重新计时
                self._primary_failures += 1
                self._open_locked("主端点探测失败", probe=True)
                notify = True
            elif self._state == STATE_OPEN:
                self._backup_failures += 1
                if self._backup_failures >= self._fail_threshold and \
                        self._index + 1 < len(self._backups):
                    self._index += 1
                    self._backup_failures = 0
                    self._open_locked("当前备援也连续失败，按列表顺序换下一个")
                # 无论换没换，都要刷新一次镜像 TTL：故障持续期间不能让状态键先过期
                notify = True
            else:
                self._primary_failures += 1
                if self.enabled and self._primary_failures >= self._fail_threshold:
                    self._index = 0
                    self._backup_failures = 0
                    self._open_locked("主端点连续失败达阈值")
                    notify = True
        if notify:
            self._emit()

    def record_success(self) -> None:
        """一次调用成功：清零计数并回 CLOSED（活跃端点因此回到主端点）。"""
        notify = False
        with self._lock:
            was_open = self._state != STATE_CLOSED
            self._state = STATE_CLOSED
            self._primary_failures = 0
            self._backup_failures = 0
            self._index = 0
            self._opened_at = 0.0
            if was_open:
                logger.info("LLM 熔断器恢复：探测成功 → CLOSED，主端点重新可用（service=%s）",
                            self.service or "-")
                notify = True
        if notify:
            self._emit()

    # -- 状态读取 ----------------------------------------------------------

    def _open_locked(self, why: str, *, probe: bool = False) -> None:
        self._state = STATE_OPEN
        self._opened_at = self._now()
        self._switches += 1
        ep = self._backups[self._index] if self.enabled else self._primary
        logger.warning(
            "LLM 熔断器 %s（%s）：连续失败 主端点=%d 备援=%d，活跃端点切到 %s；%ss 后打回主端点探测",
            "OPEN" if not probe else "OPEN（探测失败）", why, self._primary_failures,
            self._backup_failures, ep["name"], int(self._reset_timeout))

    def active_endpoint(self) -> dict:
        """当前应该使用的端点（含 OPEN→HALF_OPEN 的时间判定）。

        探测**不发合成请求**：60s 窗口一过，下一次真实模型调用就打在主端点上，
        成功即 CLOSED、失败即回 OPEN——省一次付费请求，也不新增请求站点。
        """
        transitioned = False
        with self._lock:
            if not self.enabled:
                return dict(self._primary)
            if self._state == STATE_OPEN and \
                    (self._now() - self._opened_at) >= self._reset_timeout:
                self._state = STATE_HALF_OPEN
                transitioned = True
            if self._state == STATE_HALF_OPEN:
                endpoint = dict(self._primary)
            elif self._state == STATE_OPEN:
                endpoint = dict(self._backups[self._index])
            else:
                endpoint = dict(self._primary)
        if transitioned:
            logger.info("LLM 熔断器进入 HALF_OPEN：下一次调用打回主端点探测（service=%s）",
                        self.service or "-")
            self._emit()
        return endpoint

    def current_endpoint(self) -> str:
        """当前活跃端点名（设计文档口径：`primary` / `backup_1` / …）。"""
        return self.active_endpoint()["name"]

    def state(self) -> str:
        """当前状态：CLOSED / OPEN / HALF_OPEN（读状态不推进时间判定）。"""
        with self._lock:
            return self._state

    def snapshot(self) -> dict:
        """状态快照（进日志、Redis 镜像与健康接口；**不含**任何地址或密钥）。"""
        with self._lock:
            if not self.enabled or self._state in (STATE_CLOSED, STATE_HALF_OPEN):
                name = self._primary["name"]
            else:
                name = self._backups[self._index]["name"]
            open_seconds = (round(self._now() - self._opened_at, 1)
                            if self._state == STATE_OPEN and self._opened_at else 0.0)
            return {
                "state": self._state,
                "active_endpoint": name,
                "consecutive_failures": self._primary_failures,
                "fail_threshold": self._fail_threshold,
                "reset_timeout_s": self._reset_timeout,
                "backups_configured": len(self._backups),
                "switches": self._switches,
                "open_seconds": open_seconds,
                "service": self.service,
                "ts": time.time(),
            }

    # -- 环境变量切换 ------------------------------------------------------

    def apply_env(self) -> dict:
        """把本进程 `LLM_*` 环境变量指向当前活跃端点（同一端点且没被改过→不重写）。

        未配备援（enabled=False）→ 一个字都不写：行为与改动前逐字节一致。
        活跃端点=主端点且从未切换过 → 同样不写：本进程 env 本来就是主端点，
        照抄配置只会把"进程自身设过的端点"改掉（零影响约束的另一面）。

        **漂移自愈**：`llm_client._ensure_cfg_fresh()` 在 config.json mtime 变化时会把
        `LLM_*` 重写成主端点（优先级高于我们的切换），所以幂等判定不只看"上次切到哪个
        端点"，还要看"上次写进去的值还在不在"——被改过就按当前活跃端点重写一次。
        """
        endpoint = self.active_endpoint()
        if not self.enabled:
            return endpoint
        target = endpoint["name"]
        with self._lock:
            if self._applied == target and self._env_in_place():
                return endpoint
            if target == PRIMARY_NAME and not self._applied:
                # 从未切换过：env 就是进程自己的主端点，不写（只记住这个事实）
                self._applied = PRIMARY_NAME
                self._applied_env = {}
                return endpoint
            if self._env_baseline is None:
                self._env_baseline = {k: os.environ.get(k, "") for k in ENV_KEYS}
            before = os.environ.get("LLM_BASE_URL", "")
            values = _env_values_for(endpoint, self._primary, self._env_baseline)
            for key, value in values.items():
                if value:
                    os.environ[key] = value
            self._applied = target
            self._applied_env = {k: v for k, v in values.items() if v}
        logger.warning("LLM 调用端点切换：%s → %s（活跃端点 %s，state=%s）",
                       before or "-", os.environ.get("LLM_BASE_URL") or "-", target,
                       self._state)
        return endpoint

    def _env_in_place(self) -> bool:
        """上次写入的 `LLM_*` 值是否还在 env 里（False=被热重载/人工改过 → 重写）。"""
        for key, value in (self._applied_env or {}).items():
            if value and os.environ.get(key, "") != value:
                return False
        return True

    # -- 内部：状态变化广播 ------------------------------------------------

    def _emit(self) -> None:
        """状态变化 → 回调（默认 Redis 镜像）。异常绝不外抛：观测层不得影响调用。"""
        try:
            snap = self.snapshot()
        except Exception:                               # noqa: BLE001 - 快照失败不拦记账
            return
        sink = self._sink if self._sink is not None else _publish_state
        try:
            sink(snap)
        except Exception as exc:                        # noqa: BLE001 - 镜像失败不拦记账
            logger.debug("LLM 熔断状态镜像失败：%s", str(exc)[:120])


def _env_values_for(endpoint: dict, primary: dict, baseline: dict) -> dict:
    """端点字典 → 要写入的环境变量。

    备援端点自带地址/密钥/模型；回切主端点时优先用**本熔断器记录的主端点**
    （config.json 改动会让 `breaker()` 按 mtime 重建，所以它不会长期过期），
    再回退到首次覆盖前的 env 原值。
    """
    if endpoint["name"] != PRIMARY_NAME:
        return {"LLM_BASE_URL": endpoint["base_url"], "LLM_API_KEY": endpoint["api_key"],
                "LLM_MODEL": endpoint["model"]}
    return {
        "LLM_BASE_URL": primary["base_url"] or baseline.get("LLM_BASE_URL", ""),
        "LLM_API_KEY": primary["api_key"] or baseline.get("LLM_API_KEY", ""),
        "LLM_MODEL": primary["model"] or baseline.get("LLM_MODEL", ""),
    }


# ---------------------------------------------------------------------------
# 跨进程观测镜像（Redis，best-effort；真源仍是各进程内的单例）
# ---------------------------------------------------------------------------

def _state_client():
    """一次性 Redis 客户端：关掉内建重试（Redis 不可达时 2 秒内失败，不拖住调用）。"""
    import redis as _redis
    try:
        from common import _NO_REDIS_RETRY as _retry
    except Exception:                                   # noqa: BLE001 - 独立可用
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        _retry = Retry(NoBackoff(), 0)
    return _redis.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "6379") or 6379),
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
        retry=_retry,
    )


def _publish_state(snap: dict) -> None:
    """状态快照 → Redis 镜像键 `llm:circuit:{service}`（TTL=STATE_TTL）。

    失败静默：镜像只服务于 guardian 心跳与健康页的可见性，不能反过来影响调用
    （Redis 不可达时最多等 1 秒连接超时，然后当没发生）。
    """
    if not isinstance(snap, dict):
        return
    key = f"{STATE_KEY_PREFIX}{_service_name(snap)}"
    payload = json.dumps(snap, ensure_ascii=False)
    client = None
    try:
        client = _state_client()                        # 构造也可能失败，一并吞掉
        client.setex(key, STATE_TTL, payload)
    except Exception as exc:                            # noqa: BLE001 - 镜像失败不外抛
        logger.debug("LLM 熔断镜像写入失败：%s", str(exc)[:120])
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:                           # noqa: BLE001 - 关不掉不影响结论
                pass


def read_shared_states() -> list[dict]:
    """读其他进程写下的熔断镜像（读不到/Redis 不可用 → 空列表，绝不外抛）。"""
    out: list[dict] = []
    client = None
    try:
        client = _state_client()
        for key in client.scan_iter(match=f"{STATE_KEY_PREFIX}*", count=100):
            raw = client.get(key)
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except Exception:                           # noqa: BLE001 - 坏值跳过
                continue
            if isinstance(item, dict):
                out.append(item)
    except Exception as exc:                            # noqa: BLE001 - Redis 不可用=无镜像
        logger.debug("LLM 熔断镜像读取失败：%s", str(exc)[:120])
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:                           # noqa: BLE001
                pass
    return out


_SERVICE_NAME = str(os.environ.get("WM_SERVICE") or os.environ.get("WEAVEMIND_SERVICE") or "")


def set_service_name(name: str) -> None:
    """记录本进程在镜像键里的名字（worker 用自己的 agent_id，便于定位是谁在熔断）。"""
    global _SERVICE_NAME
    name = str(name or "")
    if not name:
        return
    _SERVICE_NAME = name
    if _SINGLETON is not None:
        _SINGLETON.service = name


def _service_name(snap: dict | None = None) -> str:
    if snap and snap.get("service"):
        return str(snap["service"])
    return _SERVICE_NAME or "process"


# ---------------------------------------------------------------------------
# 进程内单例 + 调用点接口
# ---------------------------------------------------------------------------

_SINGLETON: LLMCircuitBreaker | None = None
_SINGLETON_MTIME: float | None = None
_SINGLETON_LOCK = threading.Lock()


def build_breaker(cfg: dict | None = None, **kwargs) -> LLMCircuitBreaker:
    """按当前配置构造一个熔断器（测试直接用它，不碰单例）。"""
    cfg = load_config() if cfg is None else cfg
    kwargs.setdefault("service", _SERVICE_NAME)
    # 阈值/窗口允许配置覆盖（架构师指定的默认值 5 / 60，不配置即为它）
    system = cfg.get("system") if isinstance(cfg.get("system"), dict) else {}
    llm = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    circuit = {}
    for source in (llm.get("circuit_breaker"), system.get("llm_circuit_breaker")):
        if isinstance(source, dict):
            circuit.update(source)
    kwargs.setdefault("fail_threshold",
                      int(circuit.get("fail_threshold") or DEFAULT_FAIL_THRESHOLD))
    kwargs.setdefault("reset_timeout",
                      float(circuit.get("reset_timeout") or DEFAULT_RESET_TIMEOUT))
    return LLMCircuitBreaker(load_primary(cfg), load_backups(cfg), **kwargs)


def breaker() -> LLMCircuitBreaker:
    """本进程的熔断器单例（config.json 变更时按 mtime 重建，状态随之复位）。"""
    global _SINGLETON, _SINGLETON_MTIME
    try:
        mtime: float | None = os.path.getmtime(_CONFIG_PATH)
    except Exception:                                   # noqa: BLE001 - 读不到 mtime 不重建
        mtime = None
    with _SINGLETON_LOCK:
        if _SINGLETON is None or (mtime is not None and mtime != _SINGLETON_MTIME):
            _SINGLETON = build_breaker()
            _SINGLETON_MTIME = mtime
        return _SINGLETON


def activate_for_call(service: str = "") -> str:
    """调用模型前调用：把本进程 LLM_* 指向当前活跃端点，返回端点名。

    未配备援端点 → 原样返回 `primary` 且**不改任何环境变量**（零影响约束）。
    """
    if service:
        set_service_name(service)
    try:
        return breaker().apply_env()["name"]
    except Exception as exc:                            # noqa: BLE001 - 切换失败按原状调用
        logger.debug("LLM 端点半切失败：%s", str(exc)[:120])
        return PRIMARY_NAME


def active_model_override() -> str | None:
    """当前活跃端点是备援时，返回该备援的模型名（主端点/未配备援 → None）。

    为什么需要它：`get_model_for_usage()` 的优先级是 `llm.model_roles[用途]` > `LLM_MODEL`，
    只改 `LLM_MODEL` 不会真的换模型——主端点模型名发给备援供应商就是 404（实机已见
    「流式请求被拒（http_404）回退非流式」）。这里给出的 override 与 `llm_client.
    _async_call_backup` 自己的做法一致（`b_payload["model"] = _BACKUP_CFG["model"]`）。
    """
    try:
        b = breaker()
        if not b.enabled:
            return None
        endpoint = b.active_endpoint()
        if endpoint["name"] == PRIMARY_NAME:
            return None
        return endpoint["model"] or None
    except Exception:                                   # noqa: BLE001 - 取不到就按原样调用
        return None


def report_failure(exc_or_class: Any) -> str:
    """调用失败后调用：归类并记账，返回错误类别（不计入熔断时也返回类别名）。"""
    if isinstance(exc_or_class, str):
        error_class = exc_or_class
    else:
        error_class = classify_endpoint_error(exc_or_class)
    try:
        breaker().record_failure(error_class)
    except Exception as exc:                            # noqa: BLE001 - 记账失败不拦业务
        logger.debug("LLM 熔断记账失败：%s", str(exc)[:120])
    return error_class


def report_success() -> None:
    """调用成功后调用：清零连续失败计数并回 CLOSED。"""
    try:
        breaker().record_success()
    except Exception as exc:                            # noqa: BLE001
        logger.debug("LLM 熔断成功记账失败：%s", str(exc)[:120])


def snapshot() -> dict:
    """本进程熔断状态快照。"""
    return breaker().snapshot()


def aggregate_view() -> dict:
    """本进程 + 其他进程镜像合并后的**最坏**状态（供 guardian 心跳与健康页）。"""
    views: list[dict] = []
    try:
        views.append(snapshot())
    except Exception:                                   # noqa: BLE001 - 自己都读不到就只看镜像
        pass
    try:
        views.extend(read_shared_states())
    except Exception:                                   # noqa: BLE001 - Redis 不可用=无镜像
        pass

    rank = {STATE_CLOSED: 0, STATE_HALF_OPEN: 1, STATE_OPEN: 2}
    best: tuple[int, int, str, dict] | None = None
    reporting = 0
    for view in views:
        if not isinstance(view, dict):
            continue
        state = str(view.get("state") or STATE_CLOSED)
        if state not in rank:
            state = STATE_CLOSED
        if state != STATE_CLOSED:
            reporting += 1
        failures = int(view.get("consecutive_failures") or 0)
        if best is None or (rank[state], failures) > (best[0], best[1]):
            best = (rank[state], failures, state, view)
    if best is None:
        return {"state": STATE_CLOSED, "active_endpoint": PRIMARY_NAME,
                "consecutive_failures": 0, "backups_configured": 0,
                "services_reporting": 0}
    _, failures, state, view = best
    return {
        "state": state,
        "active_endpoint": str(view.get("active_endpoint") or PRIMARY_NAME),
        "consecutive_failures": failures,
        "backups_configured": int(view.get("backups_configured") or 0),
        "services_reporting": reporting,
    }


def health_view() -> dict:
    """`/api/health` 的 `llm` 字段：只有状态名、端点名与计数（无地址、无密钥）。"""
    view = aggregate_view()
    return {
        "state": view["state"],
        "active_endpoint": view["active_endpoint"],
        "consecutive_failures": view["consecutive_failures"],
        "backups_configured": view["backups_configured"],
    }


def reset_for_tests(cfg: dict | None = None, **kwargs) -> LLMCircuitBreaker:
    """测试专用：按给定配置重建单例（cfg=None 时用真实 config.json）；不触碰环境变量。

    同时把 mtime 记成当前 config.json 的 mtime，避免下一次 `breaker()` 又按
    "配置变了"把测试构造的单例冲掉。
    """
    global _SINGLETON, _SINGLETON_MTIME
    with _SINGLETON_LOCK:
        try:
            _SINGLETON_MTIME = os.path.getmtime(_CONFIG_PATH)
        except Exception:                               # noqa: BLE001 - 没有 config.json 也允许
            _SINGLETON_MTIME = None
        _SINGLETON = build_breaker(cfg, **kwargs)
        return _SINGLETON
