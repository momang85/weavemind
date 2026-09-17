# -*- coding: utf-8 -*-
"""根任务预算：**唯一**预留/结算入口（M0-e）。

为什么单独一个模块：此前的"预算"不存在——步骤超时是**每步各自**的 300 秒下限，
主备切换与内外层重试各自重新计时，反思/评审/验收/收尾各有各的调用，没有任何地方
对"这个根任务一共花了多少次调用、多少秒、多少 token"负责。实测表现为：
任务能在余额耗尽后继续空转，或者一次任务把时间预算翻好几倍。

五条不变量：
- **一次任务一份账**：按根任务建账，落盘在任务工作区（`budget_state.json`），
  恢复/换进程继续用同一份剩余额度，不被主备切换或重试重置；
- **先预留后发送**：发送前原子预留（次数/时间/token），发送后结算实际用量，
  发送前失败可退回；预留不到就直接拒绝新调用（不"先花再算"）；
- **token 也按上界预留**：不是"先发再按实际扣"——否则 `max_tokens=100` 时两次
  `reserve(tokens=80)` 都会获准，等结算时才发现超了一倍（R2 反例）；
- **票据状态互斥迁移**：一张票据只能从 `open` 走到 `settled` **或** `unsettled`
  中的**一个**，且只能走一次。取消时把票据转 `unsettled`（可能仍在计费），
  收尾就不得再把它记成 `settled ok`——两边都记一遍等于把真实状态藏起来；
  虚构出来的票据（从未预留过）一律拒绝，不做"假平衡"；
- **跨进程原子**：配置了 Redis 时计数与票据号走 Redis 原子操作（`INCRBY`），
  文件只是快照；两个进程各自拿到的票据号因此不再重复，预留也不会双花。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """预算不足：拒绝新调用（不是"继续跑但记一笔"）。"""


# 在飞调用转"待对账"后，供应商侧可能什么时候才真正结清（R2：与"取消 UI 响应时限"
# 是**两个**时限，不能混成一个数）。默认 900s，可用环境变量调整。
INFLIGHT_SETTLE_SECONDS = float(
    os.environ.get("WM_INFLIGHT_SETTLE_SECONDS", "900") or 900
)


@dataclass
class BudgetLimits:
    """根任务预算上限；0 表示该维度不限。"""

    max_seconds: float = 0.0
    max_calls: int = 0
    max_tokens: int = 0


@dataclass
class BudgetState:
    started_at: float = 0.0
    calls_reserved: int = 0
    calls_settled: int = 0
    calls_unsettled: int = 0
    tokens_reserved: int = 0
    tokens_settled: int = 0
    tokens_unsettled: int = 0
    seq: int = 0
    # 账本身份：首次落盘时生成、随文件持久化。共享计数的键空间按它隔离，
    # 这样"同一次运行"（含断点恢复）共用计数，"重新开始的一次"不继承上一轮。
    ledger_id: str = ""
    # 票据号 → {stage, at, calls, tokens, detail}；只在 open/unsettled 时存在
    open_tickets: dict = field(default_factory=dict)
    unsettled_tickets: dict = field(default_factory=dict)
    stages: dict = field(default_factory=dict)
    # 被拒绝的迁移（虚构票据 / 重复迁移 / 已取消再结算）——账本自身的问题要看得见
    rejected_transitions: dict = field(default_factory=dict)


def limits_from_config(cfg: dict | None = None) -> BudgetLimits:
    """从 config 的 `system.budget` 段读上限；缺省全为 0（**真正不限**，见 `limited`）。"""
    sys_cfg = ((cfg or {}).get("system") or {}) if isinstance(cfg, dict) else {}
    b = sys_cfg.get("budget") or {}
    if not isinstance(b, dict):
        b = {}

    def _num(key: str, default: float = 0.0) -> float:
        try:
            return float(b.get(key, default) or 0.0)
        except (TypeError, ValueError):
            return default

    return BudgetLimits(
        max_seconds=_num("max_seconds"),
        max_calls=int(_num("max_calls")),
        max_tokens=int(_num("max_tokens")),
    )


class RootBudget:
    """单根任务的账本：线程安全 + 原子落盘（Redis 可用时跨进程原子预留）。"""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()
    # 票据号里的实例标记：即使两个进程各自从 0 计数，票据号也不会撞成同一个
    _instance_tag = f"{os.getpid():x}-{uuid.uuid4().hex[:4]}"
    # 跨进程后端健康：**按后端实例缓存**（进程级），失败一次就不再重复付连接超时。
    # 账本在预留/收尾/看门狗等热路径上被读，"每次都探测一次不可达的后端"会把
    # 一次告警、一次预留拖到秒级（实测过）。
    _backend_health: "weakref.WeakKeyDictionary" = None
    _health_guard = threading.Lock()

    def __init__(self, root_task_id: str, workspace: str | Path,
                 limits: BudgetLimits | None = None, redis_factory=None):
        self.root_task_id = str(root_task_id or "")
        self.path = Path(workspace) / "budget_state.json"
        self.limits = limits or BudgetLimits()
        with RootBudget._locks_guard:
            self._lock = RootBudget._locks.setdefault(str(self.path), threading.Lock())
        # 跨进程原子后端（可选）：只有它能让"两个进程共用一份剩余额度"成立。
        # 没有它时按单进程语义记（文件仍是快照，但并发写是后写覆盖）。
        self._redis_factory = redis_factory
        self._redis = None
        self._redis_failed = False
        # 跨进程后端是否已**建立**（写路径成功用过一次）。观测读取不建立后端。
        self._established = False
        self.state = self._load()
        if not self.state.ledger_id:
            # 新账本：生成身份并立刻落盘，后续实例（含恢复）读回同一个身份
            self.state.ledger_id = uuid.uuid4().hex[:12]
            self._save()

    # ── Redis 原子后端 ──────────────────────────────────────
    @classmethod
    def _health_cache(cls):
        if cls._backend_health is None:
            cls._backend_health = weakref.WeakKeyDictionary()
        return cls._backend_health

    def _mark_backend(self, healthy: bool) -> None:
        """记住该后端（factory 实例）当前是否可用；失败即永久降级到进程结束。"""
        self._redis_failed = not healthy
        if self._redis_factory is None:
            return
        try:
            with RootBudget._health_guard:
                RootBudget._health_cache()[self._redis_factory] = bool(healthy)
        except TypeError:
            # 不可弱引用的可调用对象（如内置函数）：只记在本实例上
            pass

    def _known_unhealthy(self) -> bool:
        if self._redis_factory is None:
            return True
        try:
            with RootBudget._health_guard:
                return RootBudget._health_cache().get(self._redis_factory) is False
        except TypeError:
            return False

    def _r(self):
        if self._redis_failed or self._redis_factory is None:
            return None
        if self._known_unhealthy():
            self._redis_failed = True
            return None
        if self._redis is None:
            try:
                client = self._redis_factory()
                for op in ("incrby", "decrby", "get"):
                    if not callable(getattr(client, op, None)):
                        raise AttributeError(f"redis 客户端缺少 {op}")
                self._redis = client
            except Exception as exc:
                logger.warning("预算跨进程后端不可用，降级为单进程账本：%s", str(exc)[:100])
                self._mark_backend(False)
                return None
        return self._redis

    def _keys(self) -> dict:
        """共享计数的键空间：**按账本身份**（根任务 + 本次运行起始时间）隔离。

        只用 root_task_id 会串账：同一个任务 id 被重新提交（重跑、测试复用 id）时，
        上一轮留在 Redis 里的计数还在（键不会自己消失），新一次的第一次预留就会被
        "上一轮已经花完"直接拒绝——实测复现：跨进程用例把计数留在 Redis 后，
        同 id 的其它用例第一次派发即被拒绝（用 started_at 的**整秒**做过一版，
        同一秒内启动的两个账本仍会撞键，故改用随账本持久化的 `ledger_id`）。

        `ledger_id` 随账本文件持久化：同一次运行（含断点恢复）共用同一份计数，
        重新开始的一次运行拿到新的键空间，不继承上一轮。
        """
        base = f"wm:budget:{self.root_task_id}:{self.state.ledger_id}"
        return {"calls": f"{base}:calls", "tokens": f"{base}:tokens",
                "seq": f"{base}:seq"}

    def _shared(self, name: str):
        """读共享计数（Redis）；不可用或后端尚未建立时返回 None（读本地快照）。

        跨进程时**共享计数才是真源**：本进程的本地字段只记"我这个进程预留了多少"，
        另一个进程花的额度只有 Redis 知道——额度是否还有剩余必须按共享值判断。

        两条纪律：
        - **观测读取不为探测付代价**：只有写路径（`reserve`）才建立后端连接，
          看门狗/快照这类观测读取不带连接超时（实测：看门狗的首次告警被后端探测的
          连接超时拖到断言之后才发出，告警形同无效）。后端建立后这里自然读到共享计数。
        - 探测失败**永久降级**为本进程的本地账本（`_mark_backend`）：账本在预留、
          收尾、看门狗等热路径上被读，每次重试会把"Redis 不可用"变成反复的连接超时。
          降级状态在 `snapshot()["shared"]` 里可见，不假装还在跨进程合并。
        """
        if not self._established:
            return None
        r = self._r()
        if r is None:
            return None
        try:
            v = r.get(self._keys()[name])
            return None if v is None else int(v)
        except Exception as exc:
            self._mark_backend(False)
            logger.warning("预算共享计数读取失败，降级为本地账本：%s", str(exc)[:100])
            return None

    def calls_committed(self) -> int:
        """已发出的调用次数（跨进程时取共享计数）。"""
        shared = self._shared("calls")
        return max(0, int(self.state.calls_reserved)) if shared is None else max(0, shared)

    # ── 落盘 ────────────────────────────────────────────────
    def _load(self) -> BudgetState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        st = BudgetState(started_at=float(raw.get("started_at") or time.time()))
        st.calls_reserved = int(raw.get("calls_reserved") or 0)
        st.calls_settled = int(raw.get("calls_settled") or 0)
        st.calls_unsettled = int(raw.get("calls_unsettled") or 0)
        st.tokens_reserved = int(raw.get("tokens_reserved") or 0)
        st.tokens_settled = int(raw.get("tokens_settled") or 0)
        st.tokens_unsettled = int(raw.get("tokens_unsettled") or 0)
        st.seq = int(raw.get("seq") or 0)
        st.ledger_id = str(raw.get("ledger_id") or "")
        st.open_tickets = dict(raw.get("open_tickets") or {})
        st.unsettled_tickets = dict(raw.get("unsettled_tickets") or {})
        st.stages = dict(raw.get("stages") or {})
        st.rejected_transitions = dict(raw.get("rejected_transitions") or {})
        return st

    def _save(self) -> None:
        payload = {
            "root_task_id": self.root_task_id,
            "started_at": self.state.started_at,
            "calls_reserved": self.state.calls_reserved,
            "calls_settled": self.state.calls_settled,
            "calls_unsettled": self.state.calls_unsettled,
            "tokens_reserved": self.state.tokens_reserved,
            "tokens_settled": self.state.tokens_settled,
            "tokens_unsettled": self.state.tokens_unsettled,
            "seq": self.state.seq,
            "ledger_id": self.state.ledger_id,
            "open_tickets": self.state.open_tickets,
            "unsettled_tickets": self.state.unsettled_tickets,
            "stages": self.state.stages,
            "rejected_transitions": self.state.rejected_transitions,
            "limits": {
                "max_seconds": self.limits.max_seconds,
                "max_calls": self.limits.max_calls,
                "max_tokens": self.limits.max_tokens,
            },
            "updated_at": time.time(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.warning("预算账本落盘失败（task=%s）：%s", self.root_task_id, str(exc)[:100])

    # ── 查询 ────────────────────────────────────────────────
    @property
    def limited(self) -> bool:
        """是否配置了任何上限。

        没配置（全 0）时所有方法都是无副作用的空操作——**0 就是不限**，
        不再拿 `system.task_timeout` 之类别的配置兜底成"其实还是有个上限"。
        """
        return bool(self.limits.max_seconds or self.limits.max_calls
                    or self.limits.max_tokens)

    def elapsed(self) -> float:
        return max(0.0, time.time() - self.state.started_at)

    def remaining(self) -> dict:
        """剩余额度。

        - 调用次数按**已发出的调用**扣（结算不退还：调用确实发生了）；
        - token 按**已承诺的量**扣 = open 票据的上界 + 已结算实际 + 待对账上界。
          用上界而不是"事后实际"，第二次 `reserve(tokens=80)` 才会在
          `max_tokens=100` 时被拒绝，而不是等两笔都花完才发现超了一倍（R2 反例）。
        """
        left = {
            "seconds": None if not self.limits.max_seconds
            else max(0.0, self.limits.max_seconds - self.elapsed()),
            "calls": None if not self.limits.max_calls
            else max(0, self.limits.max_calls - self.calls_committed()),
            "tokens": None if not self.limits.max_tokens
            else max(0, self.limits.max_tokens - self.tokens_committed()),
        }
        return left

    def tokens_committed(self) -> int:
        """已承诺的 token 上界。

        跨进程时取 Redis 的共享计数（共享计数按同一口径维护：预留加**上界**、
        结算把上界换成实际、待对账保留上界、退回减上界）；无 Redis 时按本地三项之和。
        """
        shared = self._shared("tokens")
        if shared is not None:
            return max(0, shared)
        return (max(0, int(self.state.tokens_reserved))
                + max(0, int(self.state.tokens_settled))
                + max(0, int(self.state.tokens_unsettled)))

    def exhausted_reason(self) -> str:
        left = self.remaining()
        if left["seconds"] is not None and left["seconds"] <= 0:
            return f"根任务时间预算已用尽（{self.limits.max_seconds:.0f}s）"
        if left["calls"] is not None and left["calls"] <= 0:
            return f"根任务调用次数预算已用尽（{self.limits.max_calls} 次）"
        if left["tokens"] is not None and left["tokens"] <= 0:
            return f"根任务 token 预算已用尽（{self.limits.max_tokens}）"
        return ""

    def _reject(self, kind: str, ticket: str, why: str) -> None:
        """拒绝一次非法票据迁移：计数可见 + 记日志，不悄悄改数字。"""
        key = f"{kind}"
        entry = self.state.rejected_transitions.setdefault(key, {"count": 0, "last": ""})
        entry["count"] = int(entry.get("count") or 0) + 1
        entry["last"] = f"{ticket}: {why}"[:200]
        logger.warning("预算票据迁移被拒绝（%s, ticket=%s）：%s", kind, ticket, why)

    # ── 预留 / 结算 ─────────────────────────────────────────
    def reserve(self, stage: str, *, calls: int = 1, tokens: int = 0,
                detail: dict | None = None) -> str:
        """发送前原子预留；不足则抛 `BudgetExceeded`。返回票据号（不限额度时返回空串）。"""
        if not self.limited:
            return ""
        calls = max(1, int(calls or 1))
        tokens = max(0, int(tokens or 0))
        with self._lock:
            why = self.exhausted_reason()
            if why:
                raise BudgetExceeded(why)
            left = self.remaining()
            if left["calls"] is not None and left["calls"] < calls:
                raise BudgetExceeded(
                    f"根任务剩余调用次数不足（剩 {left['calls']}，需要 {calls}）")
            if left["tokens"] is not None and tokens and left["tokens"] < tokens:
                raise BudgetExceeded(
                    f"根任务剩余 token 不足（剩 {left['tokens']}，需要 {tokens}）")
            # 跨进程原子预留：先加后校验，超了再退回。INCRBY 的返回值是**加完之后**的
            # 计数，所以两个进程不可能都拿到"仍在额度内"的结论（R2 反例：两个实例
            # 各自 reserve 都获准，各自返回同一个票据号）。
            seq_remote = self._reserve_remote(calls, tokens)
            # 本地字段只记"本进程预留了多少"（文件是快照）；跨进程的总额度判断
            # 一律走 `calls_committed()`/`tokens_committed()` 读共享计数
            self.state.seq += 1
            seq = seq_remote if seq_remote is not None else self.state.seq
            self.state.calls_reserved += calls
            self.state.tokens_reserved += tokens
            ticket = f"{stage}-{seq}-{RootBudget._instance_tag}"
            now = time.time()
            self.state.open_tickets[ticket] = {
                "stage": stage, "at": now, "calls": calls, "tokens": tokens,
                "detail": dict(detail or {}),
            }
            entry = self.state.stages.setdefault(stage, {
                "reserved": 0, "settled": 0, "unsettled": 0,
                "tokens": 0, "tokens_reserved": 0, "refunded": 0,
                "first_at": now, "last_at": now,
            })
            entry["reserved"] = int(entry.get("reserved") or 0) + calls
            entry["tokens_reserved"] = int(entry.get("tokens_reserved") or 0) + tokens
            entry["last_at"] = now
            self._save()
        return ticket

    def _reserve_remote(self, calls: int, tokens: int):
        """Redis 原子预留；返回票据序号（不可用时返回 None，走本地计数）。"""
        r = self._r()
        if r is None:
            return None
        k = self._keys()
        try:
            new_calls = int(r.incrby(k["calls"], calls))
            if self.limits.max_calls and new_calls > self.limits.max_calls:
                r.decrby(k["calls"], calls)
                raise BudgetExceeded(
                    f"根任务调用次数预算已用尽（上限 {self.limits.max_calls}，"
                    f"本次已到 {new_calls}）")
            if tokens:
                new_tok = int(r.incrby(k["tokens"], tokens))
                if self.limits.max_tokens and new_tok > self.limits.max_tokens:
                    r.decrby(k["tokens"], tokens)
                    r.decrby(k["calls"], calls)
                    raise BudgetExceeded(
                        f"根任务 token 预算已用尽（上限 {self.limits.max_tokens}，"
                        f"本次已到 {new_tok}）")
            seq = int(r.incrby(k["seq"], calls))
            self._established = True
            return seq
        except BudgetExceeded:
            raise
        except Exception as exc:
            logger.warning("跨进程预留失败，按本地账本继续：%s", str(exc)[:100])
            self._mark_backend(False)
            return None

    def _take_open(self, ticket: str) -> dict | None:
        """把票据从 `open` 取出（迁移的唯一入口）；不在 open 里返回 None。

        只释放**未发出调用的上界**（token）。调用次数不在这里退还——票据被取出
        意味着"这次调用已经发生"（结算或转待对账），只有 `refund`（发送前失败）
        才算没发生、才退还次数。
        """
        rec = self.state.open_tickets.pop(ticket, None)
        if rec is None:
            return None
        self._release_tokens_remote(int(rec.get("tokens") or 0))
        return rec

    def _release_tokens_remote(self, tokens: int, *, actual: int | None = None) -> None:
        """跨进程 token 计数回退：`actual=None` 表示整笔上界退回（发送前失败）；
        给定 `actual` 表示结算——把上界换成实际（可能多退，也可能补扣）。"""
        r = self._r()
        if r is None:
            return
        delta = -tokens if actual is None else (actual - tokens)
        if not delta:
            return
        try:
            r.incrby(self._keys()["tokens"], delta)
            self._established = True
        except Exception as exc:
            self._mark_backend(False)
            logger.warning("跨进程 token 回退失败，降级为本地账本：%s", str(exc)[:100])

    def _release_calls_remote(self, calls: int) -> None:
        r = self._r()
        if r is None or not calls:
            return
        try:
            r.decrby(self._keys()["calls"], calls)
            self._established = True
        except Exception as exc:
            self._mark_backend(False)
            logger.warning("跨进程调用次数回退失败，降级为本地账本：%s", str(exc)[:100])

    def settle(self, ticket: str, *, tokens: int = 0, ok: bool = True,
               note: str = "") -> None:
        """结算一次调用（成功或失败都要结算——失败也花了钱）。

        互斥迁移：只有仍 `open` 的票据能结算。已经转 `unsettled` 的票据（取消时
        转过去的、可能仍在计费）**不得**再记成 `settled`——两边都记等于把
        "这个调用到底停下没有"藏起来；虚构票据同样拒绝。
        """
        if not ticket or not self.limited:
            return
        with self._lock:
            if ticket in self.state.unsettled_tickets:
                self._reject("settle_after_unsettled", ticket,
                             "该票据已转待对账（可能仍在计费），不得再记为已结算")
                self._save()
                return
            rec = self._take_open(ticket)
            if rec is None:
                self._reject("settle_unknown_ticket", ticket, "票据不存在或已结算")
                self._save()
                return
            stage = str(rec.get("stage") or "")
            actual = max(0, int(tokens or 0))
            # token：上界换成实际；调用次数不退（这次调用已经发生了）
            upper = int(rec.get("tokens") or 0)
            self.state.tokens_reserved = max(0, self.state.tokens_reserved - upper)
            self._release_tokens_remote(upper, actual=actual)
            entry = self.state.stages.get(stage)
            if isinstance(entry, dict):
                entry["open"] = [t for t in (entry.get("open") or [])
                                 if str(t.get("ticket")) != ticket]
                entry["settled"] = int(entry.get("settled") or 0) + 1
                entry["tokens"] = int(entry.get("tokens") or 0) + actual
                entry["tokens_reserved"] = max(
                    0, int(entry.get("tokens_reserved") or 0) - upper)
                entry["last_at"] = time.time()
                if not ok:
                    entry["last_failed"] = True
                if note:
                    entry["last_note"] = str(note)[:200]
            self.state.calls_settled += 1
            self.state.tokens_settled += actual
            self._save()

    def refund(self, ticket: str, *, note: str = "") -> None:
        """发送前失败 → 退回预留（这次调用没有发生）。"""
        if not ticket or not self.limited:
            return
        with self._lock:
            rec = self._take_open(ticket)
            if rec is None:
                self._reject("refund_unknown_ticket", ticket, "票据不存在或已结算")
                self._save()
                return
            stage = str(rec.get("stage") or "")
            calls = int(rec.get("calls") or 1)
            upper = int(rec.get("tokens") or 0)
            self.state.calls_reserved = max(0, self.state.calls_reserved - calls)
            self.state.tokens_reserved = max(0, self.state.tokens_reserved - upper)
            self._release_calls_remote(calls)
            entry = self.state.stages.get(stage)
            if isinstance(entry, dict):
                entry["open"] = [t for t in (entry.get("open") or [])
                                 if str(t.get("ticket")) != ticket]
                entry["refunded"] = int(entry.get("refunded") or 0) + 1
                entry["tokens_reserved"] = max(
                    0, int(entry.get("tokens_reserved") or 0) - upper)
            if note:
                logger.info("预算退回（%s）：%s", ticket, str(note)[:120])
            self._save()

    def mark_unsettled(self, ticket: str, *, reason: str = "") -> bool:
        """取消/放弃等待：把**真实**在飞票据转待对账（可能仍在计费）。

        返回是否真的迁移成功。虚构票据（从未预留过）拒绝并计数——此前这里会
        凭空写一张 `xxx-inflight` 票据，账面上"取消已入账"，而真正预留的那张
        仍挂在 open 上，随后又被 `finally` 结算成成功，真实状态就再也读不出来了。
        """
        if not ticket or not self.limited:
            return False
        with self._lock:
            if ticket in self.state.unsettled_tickets:
                return True
            rec = self._take_open(ticket)
            if rec is None:
                self._reject("unsettled_unknown_ticket", ticket,
                             "票据不存在或已结算（不得凭空造票据）")
                self._save()
                return False
            stage = str(rec.get("stage") or "")
            rec["unsettled"] = True
            rec["reason"] = str(reason or "")[:200]
            # 供应商侧结算时限：这段时间内它仍可能被计费（与"取消在 UI 上多久生效"
            # 是两个不同的时限，分别记录，别用一个数糊过去）
            rec["reconcile_by"] = time.time() + INFLIGHT_SETTLE_SECONDS
            self.state.unsettled_tickets[ticket] = rec
            entry = self.state.stages.get(stage)
            if isinstance(entry, dict):
                entry["open"] = [t for t in (entry.get("open") or [])
                                 if str(t.get("ticket")) != ticket]
                entry["unsettled"] = int(entry.get("unsettled") or 0) + 1
                entry["last_at"] = time.time()
                # 上界留在 tokens_reserved 里不释放：这次调用可能仍在计费，
                # 按"可能已花"记账比按"肯定没花"记账诚实
                entry["tokens_unsettled"] = int(entry.get("tokens_unsettled") or 0) \
                    + int(rec.get("tokens") or 0)
            self.state.calls_unsettled += 1
            self.state.tokens_unsettled += int(rec.get("tokens") or 0)
            self._save()
            return True

    def mark_stage_unsettled(self, stage: str, *, reason: str = "") -> int:
        """把某阶段**当前所有 open 票据**转待对账（取消时不知道该调用拿了哪张票据）。

        只动真实存在的票据：没有 open 票据就什么都不做（返回 0），不造票据。
        """
        if not self.limited:
            return 0
        with self._lock:
            tickets = [t for t, rec in self.state.open_tickets.items()
                       if str((rec or {}).get("stage") or "") == stage]
        moved = 0
        for t in tickets:
            if self.mark_unsettled(t, reason=reason):
                moved += 1
        return moved

    def note_progress(self, stage: str, detail: dict | None = None) -> None:
        """记一次**有效进展**（心跳不算）：等待对象、最近有效进展由调用方给出。"""
        if not self.limited:
            return
        with self._lock:
            entry = self.state.stages.setdefault(stage, {
                "reserved": 0, "settled": 0, "unsettled": 0, "tokens": 0,
                "tokens_reserved": 0, "first_at": time.time(),
            })
            entry["last_progress_at"] = time.time()
            entry["last_progress"] = dict(detail or {})
            self._save()

    def snapshot(self) -> dict:
        """阶段观测快照：每阶段耗时/等待对象/最近有效进展/在飞，附带剩余预算。"""
        now = time.time()
        stages = {}
        for name, e in (self.state.stages or {}).items():
            if not isinstance(e, dict):
                continue
            open_list = [rec for t, rec in self.state.open_tickets.items()
                         if str((rec or {}).get("stage") or "") == name]
            # 待对账的票据同样"还在飞"（可能仍在计费，等对账才能销）：把它们算进
            # in_flight，但另外给出 open/unsettled 的分解，两者不会被混为一谈
            unsettled_list = [rec for t, rec in self.state.unsettled_tickets.items()
                              if str((rec or {}).get("stage") or "") == name]
            last_progress = float(e.get("last_progress_at") or e.get("first_at") or now)
            stages[name] = {
                "reserved": int(e.get("reserved") or 0),
                "settled": int(e.get("settled") or 0),
                "unsettled": int(e.get("unsettled") or 0),
                "refunded": int(e.get("refunded") or 0),
                "tokens": int(e.get("tokens") or 0),
                "tokens_reserved": int(e.get("tokens_reserved") or 0),
                "in_flight": len(open_list) + len(unsettled_list),
                "in_flight_open": len(open_list),
                "in_flight_unsettled": len(unsettled_list),
                "waiting_on": ((open_list or unsettled_list)[-1].get("detail")
                               if (open_list or unsettled_list) else {}),
                "last_progress_at": last_progress,
                "since_progress_sec": round(now - last_progress, 1),
                "last_progress": e.get("last_progress") or {},
            }
        return {
            "root_task_id": self.root_task_id,
            "elapsed_sec": round(self.elapsed(), 1),
            # reserved = **共享**已发出调用数（跨进程时含其它进程的预留）；
            # local_reserved = 本进程预留数（文件快照口径），两者并列不混淆
            "calls": {"reserved": self.calls_committed(),
                      "local_reserved": self.state.calls_reserved,
                      "settled": self.state.calls_settled,
                      "unsettled": self.state.calls_unsettled},
            "tokens": {"committed": self.tokens_committed(),
                       "open_upper": self.state.tokens_reserved,
                       "settled": self.state.tokens_settled,
                       "unsettled": self.state.tokens_unsettled},
            "remaining": self.remaining(),
            "limits": {"max_seconds": self.limits.max_seconds,
                       "max_calls": self.limits.max_calls,
                       "max_tokens": self.limits.max_tokens},
            "stages": stages,
            "open_tickets": {t: dict(rec) for t, rec in self.state.open_tickets.items()},
            "unsettled_tickets": {t: dict(rec)
                                  for t, rec in self.state.unsettled_tickets.items()},
            "rejected_transitions": dict(self.state.rejected_transitions),
            "cross_process": bool(self._established),
            "shared": ("failed" if self._redis_failed
                       else ("established" if self._established else "unprobed")),
            "limited": self.limited,
        }
