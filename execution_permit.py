# -*- coding: utf-8 -*-
"""执行许可接缝（阶段 T0-b）：不可变许可 + 可信当前记录 + 有界拒绝。

为什么需要它：现役代码的"能不能继续花钱/继续发布"散落在三处各判一次——
编排器在 `_dispatch` **入口**查一次租约（之后可能 `sleep(5)×6=30s` 等 worker，
再无条件 `lpush`）、worker 不继承任何取消守卫、终态落库不看"是不是同一段尝试"。
阶段T 把它收成一条判据：

- `ExecutionPermit(root_task_id, attempt_id, epoch, deadline, owner, cancelled)` 是
  **不可变**的：一次尝试一份，改字段就换新尝试。
- **可信当前记录**（attempt store）说"这个根任务现在算哪一次尝试"：编排器每段持有期
  只认自己 epoch 的许可；旧持有期的许可（gen1）在拿到新代号（gen3）后**不得复活**。
- 一切副作用之前调用 `verify(permit, current)`：**未知即拒绝**（读不到当前记录、
  attempt/epoch 不符、已取消、过截止、持有者不符），理由是稳定的字符串，便于对账。

本模块只依赖标准库与一个可选 Redis 客户端，**不 import web_ui / orchestrator**。
默认内存存储（单进程/离线/测试确定），真实多进程用 `RedisPermitStore`——
真正共享时才叫"另一进程也看得见撤销"。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field, replace

PERMIT_KEY_PREFIX = "wm:permit:"
DEFAULT_ATTEMPT_TTL = 3600.0

# 拒绝理由（稳定字符串：测试与对账都按它断言）
R_NO_PERMIT = "permit_missing"
R_NO_CURRENT = "no_current_permit"
R_ATTEMPT = "attempt_mismatch"
R_EPOCH = "epoch_mismatch"
R_CANCELLED = "cancelled"
R_DEADLINE = "deadline_passed"
R_OWNER = "owner_mismatch"
R_TASK = "root_task_mismatch"


@dataclass(frozen=True)
class ExecutionPermit:
    """一次尝试的执行许可（不可变）。"""

    root_task_id: str
    attempt_id: str
    epoch: int
    deadline: float
    owner: str = ""
    cancelled: bool = False

    def to_wire(self) -> dict:
        return {
            "root_task_id": self.root_task_id, "attempt_id": self.attempt_id,
            "epoch": int(self.epoch), "deadline": float(self.deadline),
            "owner": self.owner, "cancelled": bool(self.cancelled),
        }

    @classmethod
    def from_wire(cls, raw) -> "ExecutionPermit | None":
        """结构不识别返回 None（调用方按"没有许可"处理，不得猜）。"""
        if not isinstance(raw, dict):
            return None
        try:
            task_id = str(raw.get("root_task_id") or "")
            attempt = str(raw.get("attempt_id") or "")
            if not task_id or not attempt:
                return None
            return cls(
                root_task_id=task_id, attempt_id=attempt,
                epoch=int(raw.get("epoch") or 0),
                deadline=float(raw.get("deadline") or 0.0),
                owner=str(raw.get("owner") or ""),
                cancelled=bool(raw.get("cancelled")),
            )
        except (TypeError, ValueError):
            return None

    def fresh(self, *, now: float | None = None) -> bool:
        """许可自身是否还没过截止（不看可信记录）。"""
        now = time.time() if now is None else now
        return float(self.deadline or 0.0) > now


def new_attempt_id() -> str:
    """新尝试代号（不可变身份；恢复/接管必须换一个，不得复用旧代号）。"""
    return f"at-{uuid.uuid4().hex[:12]}"


def verify(permit: ExecutionPermit | None, current: ExecutionPermit | None,
           *, now: float | None = None) -> tuple[bool, str]:
    """这份许可现在还能不能用。返回 `(ok, reason)`；`ok=False` 时 reason 见常量。

    顺序刻意是"先证明有可信记录，再逐项比对"：没有当前记录 = 未知 → 拒绝，
    不拿"我手里这份看着没过期"当依据（那正是旧实现的问题）。
    """
    now = time.time() if now is None else now
    if permit is None:
        return False, R_NO_PERMIT
    if current is None:
        return False, R_NO_CURRENT
    if str(permit.root_task_id) != str(current.root_task_id):
        return False, R_TASK
    if str(permit.attempt_id) != str(current.attempt_id):
        return False, R_ATTEMPT
    if int(permit.epoch) != int(current.epoch):
        return False, R_EPOCH
    if bool(current.cancelled):
        return False, R_CANCELLED
    if not current.fresh(now=now):
        return False, R_DEADLINE
    if permit.owner and current.owner and permit.owner != current.owner:
        return False, R_OWNER
    return True, ""


# ---------------------------------------------------------------- 存储

class MemoryPermitStore:
    """进程内尝试记录（离线/单进程/测试确定）。"""

    def __init__(self) -> None:
        self._data: dict[str, ExecutionPermit] = {}
        self._lock = threading.Lock()

    def put(self, permit: ExecutionPermit) -> None:
        with self._lock:
            self._data[str(permit.root_task_id)] = permit

    def get(self, root_task_id: str) -> ExecutionPermit | None:
        with self._lock:
            return self._data.get(str(root_task_id))

    def cancel(self, root_task_id: str) -> ExecutionPermit | None:
        with self._lock:
            cur = self._data.get(str(root_task_id))
            if cur is None:
                return None
            out = replace(cur, cancelled=True)
            self._data[str(root_task_id)] = out
            return out

    def clear(self, root_task_id: str) -> None:
        with self._lock:
            self._data.pop(str(root_task_id), None)


class RedisPermitStore:
    """Redis 尝试记录：多进程（编排器/worker/另一实例）看到同一份撤销。"""

    def __init__(self, client, *, prefix: str = PERMIT_KEY_PREFIX,
                 ttl_seconds: float = DEFAULT_ATTEMPT_TTL) -> None:
        self._r = client
        self._prefix = prefix
        self._ttl = float(ttl_seconds)

    def _key(self, root_task_id: str) -> str:
        return f"{self._prefix}{root_task_id}"

    def put(self, permit: ExecutionPermit) -> None:
        self._r.set(self._key(permit.root_task_id),
                    json.dumps(permit.to_wire(), ensure_ascii=False), ex=int(self._ttl))

    def get(self, root_task_id: str) -> ExecutionPermit | None:
        try:
            raw = self._r.get(self._key(root_task_id))
        except Exception:
            return None
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            return ExecutionPermit.from_wire(json.loads(raw))
        except Exception:
            return None

    def cancel(self, root_task_id: str) -> ExecutionPermit | None:
        """标记取消（幂等：只从 false→true）。

        读-改-写不加事务：取消是**单向幂等**的，且同一个根的写者只有当前持有者；
        竞态最多让标记晚一次可见，不会把"已取消"读回"未取消"。
        """
        cur = self.get(root_task_id)
        if cur is None:
            return None
        out = replace(cur, cancelled=True)
        try:
            ttl = int(self._r.pttl(self._key(root_task_id)) or 0)
        except Exception:
            ttl = int(self._ttl * 1000)
        if ttl <= 0:
            ttl = int(self._ttl * 1000)
        try:
            self._r.set(self._key(root_task_id),
                        json.dumps(out.to_wire(), ensure_ascii=False),
                        px=int(ttl))
        except Exception:
            return None
        return out

    def clear(self, root_task_id: str) -> None:
        try:
            self._r.delete(self._key(root_task_id))
        except Exception:
            pass


_store = MemoryPermitStore()
_store_lock = threading.Lock()


def configure_store(store) -> None:
    """安装当前进程使用的尝试记录（编排器/worker 各自在启动时装）。"""
    global _store
    with _store_lock:
        _store = store if store is not None else MemoryPermitStore()


def get_store():
    with _store_lock:
        return _store


# ---------------------------------------------------------------- 便捷入口

def begin_attempt(root_task_id: str, *, epoch: int, owner: str = "",
                  ttl_seconds: float = DEFAULT_ATTEMPT_TTL,
                  store=None) -> ExecutionPermit:
    """开始一次**新尝试**：新的 attempt_id + 当前持有期代号。

    恢复/接管必须走这里——旧续程拿着旧 attempt_id 一律不符（`R_ATTEMPT`），
    所以"旧续程借新代号放行"在结构上不成立。
    """
    store = store or get_store()
    now = time.time()
    permit = ExecutionPermit(
        root_task_id=str(root_task_id), attempt_id=new_attempt_id(),
        epoch=int(epoch), deadline=now + max(0.0, float(ttl_seconds)),
        owner=str(owner or ""),
    )
    store.put(permit)
    return permit


def current_permit(root_task_id: str, store=None) -> ExecutionPermit | None:
    return (store or get_store()).get(root_task_id)


def note_cancel(root_task_id: str, store=None) -> ExecutionPermit | None:
    """取消信号落到尝试记录上：**跨进程**可见（worker 也据此停手）。"""
    return (store or get_store()).cancel(root_task_id)


def clear_attempt(root_task_id: str, store=None) -> None:
    (store or get_store()).clear(root_task_id)


def validate(permit: ExecutionPermit | None, root_task_id: str = "",
             store=None, *, now: float | None = None) -> tuple[bool, str]:
    """按根任务取可信记录并核对这份许可（副作用前的唯一入口）。"""
    if permit is not None and root_task_id and permit.root_task_id != root_task_id:
        return False, R_TASK
    task = root_task_id or (permit.root_task_id if permit else "")
    return verify(permit, current_permit(task, store=store), now=now)


def echo_fields(permit: ExecutionPermit | None) -> dict:
    """许可随结果回传的最小字段（旧 attempt 的结果据此被识别）。"""
    if permit is None:
        return {}
    return {"root_task_id": permit.root_task_id, "attempt_id": permit.attempt_id,
            "epoch": int(permit.epoch)}


@dataclass
class GateDecision:
    ok: bool
    reason: str = ""
    detail: str = ""
    extras: dict = field(default_factory=dict)


def send_gate(permit: ExecutionPermit | None, root_task_id: str = "",
              store=None, *, now: float | None = None,
              require_fresh: bool = True) -> GateDecision:
    """发送前的统一闸门：许可有效才允许 `lpush` / 新付费调用 / 发布。

    `require_fresh=False` 用于"结果回收"这种不该被自身截止卡住的判断。
    """
    ok, reason = validate(permit, root_task_id, store=store, now=now)
    if ok and not require_fresh:
        cur = current_permit(root_task_id or (permit.root_task_id if permit else ""),
                             store=store)
        if cur is not None and not cur.fresh(now=now):
            ok, reason = False, R_DEADLINE
    if ok:
        return GateDecision(True)
    return GateDecision(False, reason=reason,
                        detail=f"执行许可不通过：{reason}")
