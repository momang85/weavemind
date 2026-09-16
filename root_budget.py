# -*- coding: utf-8 -*-
"""根任务预算：**唯一**预留/结算入口（M0-e）。

为什么单独一个模块：此前的"预算"不存在——步骤超时是**每步各自**的 300 秒下限，
主备切换与内外层重试各自重新计时，反思/评审/验收/收尾各有各的调用，没有任何地方
对"这个根任务一共花了多少次调用、多少秒、多少 token"负责。实测表现为：
任务能在余额耗尽后继续空转，或者一次任务把时间预算翻好几倍。

三条不变量：
- **一次任务一份账**：按根任务建账，落盘在任务工作区（`budget_state.json`），
  恢复/换进程继续用同一份剩余额度，不被主备切换或重试重置；
- **先预留后发送**：发送前原子预留（次数/时间/token），发送后结算实际用量，
  发送前失败可退回；预留不到就直接拒绝新调用（不"先花再算"）；
- **在飞与已结算分开**：取消/放弃等待时在飞调用记账为 `unsettled`（可能仍在计费），
  由对账补齐，不冒充"已退款"。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """预算不足：拒绝新调用（不是"继续跑但记一笔"）。"""


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
    tokens_settled: int = 0
    stages: dict = field(default_factory=dict)


def limits_from_config(cfg: dict | None = None) -> BudgetLimits:
    """从 config 的 `system.budget` 段读上限；缺省全为 0（不限额，保持既有行为）。"""
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
    """单根任务的账本：线程安全 + 原子落盘。"""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, root_task_id: str, workspace: str | Path,
                 limits: BudgetLimits | None = None):
        self.root_task_id = str(root_task_id or "")
        self.path = Path(workspace) / "budget_state.json"
        self.limits = limits or BudgetLimits()
        with RootBudget._locks_guard:
            self._lock = RootBudget._locks.setdefault(str(self.path), threading.Lock())
        self.state = self._load()

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
        st.tokens_settled = int(raw.get("tokens_settled") or 0)
        st.stages = dict(raw.get("stages") or {})
        return st

    def _save(self) -> None:
        payload = {
            "root_task_id": self.root_task_id,
            "started_at": self.state.started_at,
            "calls_reserved": self.state.calls_reserved,
            "calls_settled": self.state.calls_settled,
            "calls_unsettled": self.state.calls_unsettled,
            "tokens_settled": self.state.tokens_settled,
            "stages": self.state.stages,
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
        """是否配置了任何上限（没配置时所有方法都是无副作用的空操作）。"""
        return bool(self.limits.max_seconds or self.limits.max_calls
                    or self.limits.max_tokens)

    def elapsed(self) -> float:
        return max(0.0, time.time() - self.state.started_at)

    def remaining(self) -> dict:
        left = {
            "seconds": None if not self.limits.max_seconds
            else max(0.0, self.limits.max_seconds - self.elapsed()),
            "calls": None if not self.limits.max_calls
            else max(0, self.limits.max_calls - self.state.calls_reserved),
            "tokens": None if not self.limits.max_tokens
            else max(0, self.limits.max_tokens - self.state.tokens_settled),
        }
        return left

    def exhausted_reason(self) -> str:
        left = self.remaining()
        if left["seconds"] is not None and left["seconds"] <= 0:
            return f"根任务时间预算已用尽（{self.limits.max_seconds:.0f}s）"
        if left["calls"] is not None and left["calls"] <= 0:
            return f"根任务调用次数预算已用尽（{self.limits.max_calls} 次）"
        if left["tokens"] is not None and left["tokens"] <= 0:
            return f"根任务 token 预算已用尽（{self.limits.max_tokens}）"
        return ""

    # ── 预留 / 结算 ─────────────────────────────────────────
    def reserve(self, stage: str, *, calls: int = 1, tokens: int = 0,
                detail: dict | None = None) -> str:
        """发送前原子预留；不足则抛 `BudgetExceeded`。返回票据号。"""
        if not self.limited:
            return ""
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
            self.state.calls_reserved += calls
            ticket = f"{stage}-{self.state.calls_reserved}"
            entry = self.state.stages.setdefault(stage, {
                "reserved": 0, "settled": 0, "unsettled": 0,
                "tokens": 0, "first_at": time.time(), "last_at": time.time(),
                "open": [],
            })
            entry["reserved"] = int(entry.get("reserved") or 0) + calls
            entry["last_at"] = time.time()
            open_list = entry.setdefault("open", [])
            open_list.append({"ticket": ticket, "at": time.time(),
                              "detail": dict(detail or {})})
            self._save()
        return ticket

    def settle(self, ticket: str, *, tokens: int = 0, ok: bool = True,
               note: str = "") -> None:
        """结算一次调用（成功或失败都要结算——失败也花了钱）。"""
        if not ticket or not self.limited:
            return
        with self._lock:
            stage = str(ticket).rsplit("-", 1)[0]
            entry = self.state.stages.get(stage)
            if isinstance(entry, dict):
                entry["open"] = [t for t in (entry.get("open") or [])
                                 if str(t.get("ticket")) != ticket]
                entry["settled"] = int(entry.get("settled") or 0) + 1
                entry["tokens"] = int(entry.get("tokens") or 0) + int(tokens or 0)
                entry["last_at"] = time.time()
                if note:
                    entry["last_note"] = str(note)[:200]
            self.state.calls_settled += 1
            self.state.tokens_settled += int(tokens or 0)
            self._save()

    def refund(self, ticket: str, *, note: str = "") -> None:
        """发送前失败 → 退回预留（不算这次调用）。"""
        if not ticket or not self.limited:
            return
        with self._lock:
            stage = str(ticket).rsplit("-", 1)[0]
            entry = self.state.stages.get(stage)
            if isinstance(entry, dict):
                before = len(entry.get("open") or [])
                entry["open"] = [t for t in (entry.get("open") or [])
                                 if str(t.get("ticket")) != ticket]
                if len(entry["open"]) != before:
                    self.state.calls_reserved = max(0, self.state.calls_reserved - 1)
                    entry["refunded"] = int(entry.get("refunded") or 0) + 1
            self._save()

    def mark_unsettled(self, ticket: str, *, reason: str = "") -> None:
        """取消/放弃等待：在飞调用转待对账（可能仍在计费）。"""
        if not ticket or not self.limited:
            return
        with self._lock:
            stage = str(ticket).rsplit("-", 1)[0]
            entry = self.state.stages.get(stage)
            if isinstance(entry, dict):
                for t in (entry.get("open") or []):
                    if str(t.get("ticket")) == ticket:
                        t["unsettled"] = True
                        t["reason"] = str(reason or "")[:200]
                entry["unsettled"] = int(entry.get("unsettled") or 0) + 1
            self.state.calls_unsettled += 1
            self._save()

    def note_progress(self, stage: str, detail: dict | None = None) -> None:
        """记一次**有效进展**（心跳不算）：等待对象、最近有效进展由调用方给出。"""
        if not self.limited:
            return
        with self._lock:
            entry = self.state.stages.setdefault(stage, {
                "reserved": 0, "settled": 0, "unsettled": 0, "tokens": 0,
                "first_at": time.time(),
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
            open_list = e.get("open") or []
            last_progress = float(e.get("last_progress_at") or e.get("first_at") or now)
            stages[name] = {
                "reserved": int(e.get("reserved") or 0),
                "settled": int(e.get("settled") or 0),
                "unsettled": int(e.get("unsettled") or 0),
                "tokens": int(e.get("tokens") or 0),
                "in_flight": len(open_list),
                "waiting_on": (open_list[-1].get("detail") if open_list else {}),
                "last_progress_at": last_progress,
                "since_progress_sec": round(now - last_progress, 1),
                "last_progress": e.get("last_progress") or {},
            }
        return {
            "root_task_id": self.root_task_id,
            "elapsed_sec": round(self.elapsed(), 1),
            "calls": {"reserved": self.state.calls_reserved,
                      "settled": self.state.calls_settled,
                      "unsettled": self.state.calls_unsettled},
            "tokens_settled": self.state.tokens_settled,
            "remaining": self.remaining(),
            "limits": {"max_seconds": self.limits.max_seconds,
                       "max_calls": self.limits.max_calls,
                       "max_tokens": self.limits.max_tokens},
            "stages": stages,
            "limited": self.limited,
        }
