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

import contextlib
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

# 账本落盘的跨进程锁等待上限（秒）。拿不到就照写并留痕——账本是审计视图，
# 不能因为锁争用把记账丢掉；共享计数（Redis）才是额度的真源。
SAVE_LOCK_SECONDS = float(os.environ.get("WM_BUDGET_SAVE_LOCK_SECONDS", "5") or 5)


class BudgetExceeded(RuntimeError):
    """预算不足：拒绝新调用（不是"继续跑但记一笔"）。"""


def _lock_fd(fh) -> bool:
    """非阻塞地取文件锁（Windows: msvcrt 锁 1 字节；POSIX: flock）。取到返回 True。"""
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_fd(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:                        # noqa: BLE001 - 解锁失败不影响结果
        pass


@contextlib.contextmanager
def cross_process_file_lock(path, timeout: float | None = None):
    """覆盖完整"读改写"的跨进程锁（锁文件与数据文件分离，进程退出即释放）。

    为什么必须覆盖读改写：`_save` 是"读旧 writers → 合并 → 写 tmp → replace"，
    只有进程内 `threading.Lock` 时，两个进程可以读到同一份旧快照、各自合并、互相覆盖
    ——成功预留两次，最终快照只剩一次（协作审查在内存文件系统里确定性复现）。
    锁文件用 `a+b` 打开并锁 1 字节：文件本身是否存在无关紧要（不是靠存在性加锁），
    进程崩溃时 OS 自动释放，不留死锁。
    """
    limit = SAVE_LOCK_SECONDS if timeout is None else float(timeout)
    lock_path = Path(str(path) + ".lock")
    fh = None
    got = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+b")
        deadline = time.time() + max(0.0, limit)
        while True:
            if _lock_fd(fh):
                got = True
                break
            if time.time() >= deadline:
                logger.warning("账本落盘锁等待超时（%s）：本次仍写，计数以共享后端为准",
                               lock_path.name)
                break
            time.sleep(0.02)
        yield got
    finally:
        if fh is not None:
            if got:
                _unlock_fd(fh)
            try:
                fh.close()
            except Exception:                # noqa: BLE001
                pass


# 在飞调用转"待对账"后，供应商侧可能什么时候才真正结清（R2：与"取消 UI 响应时限"
# 是**两个**时限，不能混成一个数）。默认 900s，可用环境变量调整。
INFLIGHT_SETTLE_SECONDS = float(
    os.environ.get("WM_INFLIGHT_SETTLE_SECONDS", "900") or 900
)


@dataclass
class BudgetLimits:
    """根任务预算上限；0 表示该维度不限。

    口径（09-22 复核 A-4）：`max_calls` 扣的是**所有已预留票据**——含 `step` 步骤调度、
    `llm`/`backup` 供应商请求、`plan`/`review` 等阶段，不是"供应商实际收到多少次请求"。
    两者分开展示（`snapshot()["provider_requests"]` 与 `["steps_dispatched"]`），
    核对时不得把票据数当成供应商请求数，也不得据此放宽任何上限。
    """

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
    # D5 夜间补修：供应商**实际用量**与"没取到用量"的调用数分开记。
    # `tokens_settled` 里未知用量的部分记的是**预留上界**（不填 0）。
    tokens_actual: int = 0
    tokens_unknown_calls: int = 0
    seq: int = 0
    # 账本身份：首次落盘时生成、随文件持久化。共享计数的键空间按它隔离，
    # 这样"同一次运行"（含断点恢复）共用计数，"重新开始的一次"不继承上一轮。
    ledger_id: str = ""
    # 票据号 → {stage, at, calls, tokens, detail}；只在 open/unsettled 时存在
    open_tickets: dict = field(default_factory=dict)
    unsettled_tickets: dict = field(default_factory=dict)
    stages: dict = field(default_factory=dict)
    # D5：这次运行的**声明上限**（0=不限）。写进账本供审计——"这次跑的上限是多少"
    # 应当是账本里的事实，而不是事后只能靠剩余额度反推。
    limits: dict = field(default_factory=dict)
    # 被拒绝的迁移（虚构票据 / 重复迁移 / 已取消再结算）——账本自身的问题要看得见
    rejected_transitions: dict = field(default_factory=dict)
    # 落盘可信度：上一次落盘没能可信完成（锁超时/写失败）。断点恢复时继承，
    # 同一账本继续按"未知状态"处理，直到对账把它清掉。
    persist_uncertain: bool = False


def limits_from_config(cfg: dict | None = None) -> BudgetLimits:
    """从 config 的 `system.budget` 段读上限；缺省全为 0（**真正不限**，见 `limited`）。

    D5：允许**按运行声明**每任务上限（不改全局 config）——
    `WM_TASK_MAX_SECONDS / WM_TASK_MAX_CALLS / WM_TASK_MAX_TOKENS` 存在且可解析时
    覆盖对应维度；有界运行的上限因此是显式声明并可入账审计的。
    """
    sys_cfg = ((cfg or {}).get("system") or {}) if isinstance(cfg, dict) else {}
    b = sys_cfg.get("budget") or {}
    if not isinstance(b, dict):
        b = {}

    def _num(key: str, default: float = 0.0) -> float:
        try:
            return float(b.get(key, default) or 0.0)
        except (TypeError, ValueError):
            return default

    def _env(key: str) -> float | None:
        raw = os.environ.get(key, "")
        if str(raw).strip() == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    seconds = _env("WM_TASK_MAX_SECONDS")
    calls = _env("WM_TASK_MAX_CALLS")
    tokens = _env("WM_TASK_MAX_TOKENS")
    return BudgetLimits(
        max_seconds=(seconds if seconds is not None else _num("max_seconds")),
        max_calls=int(calls if calls is not None else _num("max_calls")),
        max_tokens=int(tokens if tokens is not None else _num("max_tokens")),
    )


def multiprocess_default() -> bool:
    """生产默认按**多进程**语义建账本（编排器与各 Worker 是独立进程）。

    `WM_SINGLE_PROCESS=1` 显式声明单进程语义（离线单测、单进程脚本）：此时本地计数
    就是全部事实，显式有界任务不因"共享后端不可用"拒发。默认值刻意偏严——少写一个
    环境变量不会静默退化成"每个进程各一份上限"。
    """
    return str(os.environ.get("WM_SINGLE_PROCESS", "") or "").strip().lower() \
        not in ("1", "true", "yes", "on")


def default_redis_factory():
    """账本的跨进程后端（Redis）。

    **短超时是刻意的**：账本在预留、收尾、看门狗等热路径上被读，而消息总线用的
    客户端连接超时是 5 秒（为长连接 pubsub 保留）。Redis 不在时那 5 秒会把一次
    告警、一次预留拖到秒级（实测：看门狗告警被拖到断言之后才发出）。这里只要
    0.3s 连接 / 0.5s 读，失败立刻降级为本地账本。

    主进程、Worker、LLM 客户端各建各的账本实例，但**必须共用同一个后端**：只给
    编排器接后端、LLM 路径退回本地计数时，"每个进程各自 40 次"会一起花掉，
    上限实际管不住整次运行（实机：一次运行的账本里只剩 step 阶段，供应商请求
    一次没进共享计数）。
    """
    try:
        import redis as _redis
        try:
            from common import _NO_REDIS_RETRY as _noretry
        except Exception:
            from redis.backoff import NoBackoff as _NoBackoff
            from redis.retry import Retry as _Retry
            _noretry = _Retry(_NoBackoff(), 0)
        return _redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            decode_responses=True,
            socket_timeout=0.5,
            socket_connect_timeout=0.3,
            retry=_noretry,
        )
    except Exception as exc:                       # noqa: BLE001 - 缺 redis 就按单进程记
        logger.warning("预算跨进程后端不可用（按单进程记账）：%s", str(exc)[:100])
        return None


class RootBudget:
    """单根任务的账本：线程安全 + 原子落盘（Redis 可用时跨进程原子预留）。"""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()
    # 票据号里的实例标记：即使两个进程各自从 0 计数，票据号也不会撞成同一个。
    # 按**账本实例**取（不是按类取）：同一进程里先后开两份账本（恢复、重开、
    # 测试）各写自己的增量，共用一个标记会让后一份覆盖前一份的计数。
    _instance_tag = f"{os.getpid():x}-{uuid.uuid4().hex[:4]}"  # 兼容旧引用
    # 跨进程后端健康：**按后端实例缓存**（进程级），失败一次就不再重复付连接超时。
    # 账本在预留/收尾/看门狗等热路径上被读，"每次都探测一次不可达的后端"会把
    # 一次告警、一次预留拖到秒级（实测过）。
    _backend_health: "weakref.WeakKeyDictionary" = None
    _health_guard = threading.Lock()

    def __init__(self, root_task_id: str, workspace: str | Path,
                 limits: BudgetLimits | None = None, redis_factory=None,
                 multiprocess: bool = False):
        self.root_task_id = str(root_task_id or "")
        self._tag = f"{os.getpid():x}-{uuid.uuid4().hex[:4]}"
        self.path = Path(workspace) / "budget_state.json"
        self.limits = limits or BudgetLimits()
        # 多进程语义：编排器与 Worker 各自建账本时置 True。显式有界任务在共享计数
        # 不可用时要 **fail closed**（不能静默退成"每个进程各一份上限"）。
        self._multiprocess = bool(multiprocess)
        with RootBudget._locks_guard:
            self._lock = RootBudget._locks.setdefault(str(self.path), threading.Lock())
        # 跨进程原子后端（可选）：只有它能让"两个进程共用一份剩余额度"成立。
        # 没有它时按单进程语义记（文件仍是快照，但并发写是后写覆盖）。
        self._redis_factory = redis_factory
        self._redis = None
        self._redis_failed = False
        # 跨进程后端是否已**建立**（写路径成功用过一次）。观测读取不建立后端。
        self._established = False
        # 落盘可信度：拿不到落盘锁或写失败时置 True（有界多进程任务据此拒绝新付费请求）
        self._persist_uncertain = False
        self.state = self._load()
        # 上一轮落盘不确定（锁超时/写失败）：同一账本继续按"未知状态"处理——
        # 断点恢复不能把"数字可能不是全部事实"这一事实洗掉，直到对账清掉它。
        if self.state.persist_uncertain:
            self._persist_uncertain = True
        # 增量基线：文件是**多进程合并视图**，本进程只把自己的增量写进 `writers`，
        # 否则第二次保存会把读进来的别人计数当成自己的再记一遍（重复计数）。
        self._baseline = self._copy_state(self.state)
        if not self.state.ledger_id:
            # 新账本：身份必须在**跨进程锁内**确定。两个进程同时启动时，先拿锁的写身份，
            # 后拿锁的必须**采纳**它——否则各写各的身份，`writers` 合并时"身份不同不并"，
            # 对方的增量会被当成上一轮丢掉（并发复现：两进程各预留 40 次，快照只剩 40）。
            #
            # 09-22 晚间复核：初始化同样要**检查锁的 got**——拿不到锁还写，等于无锁覆盖，
            # 并且会凭空造一个竞争身份（内存探针：锁返回 False 仍写 1 次、生成 ledger_id、
            # persist_uncertain=False）。拿不到锁时：不写、不生成新身份（保持空身份，
            # 由下一次拿到锁的初始化或本进程后续保存补齐）、标记落盘不确定 →
            # 有界多进程任务据此拒绝新付费请求。
            with cross_process_file_lock(self.path) as _got:
                if not _got:
                    self._persist_note("账本初始化未取到落盘锁（本次不写、不生成身份）")
                else:
                    disk = self._load()      # 锁内重读：可能已有别人刚写的身份
                    if disk.ledger_id:
                        self.state = disk    # 采纳已有账本（含它的计数）
                    else:
                        self.state.ledger_id = uuid.uuid4().hex[:12]
                    self._baseline = self._copy_state(self.state)
                    self._apply_declared_limits()
                    self._save_locked()
        # D5：声明上限入账（审计"这次跑的上限是多少"）——恢复时以磁盘为准，
        # 但本进程传入的上限若不同则更新（配置/环境变了如实反映）
        if self._apply_declared_limits():
            self._save()

    def _apply_declared_limits(self) -> bool:
        """把本进程声明的上限写进账本；有变化返回 True。"""
        declared = {"max_seconds": float(self.limits.max_seconds or 0.0),
                    "max_calls": int(self.limits.max_calls or 0),
                    "max_tokens": int(self.limits.max_tokens or 0)}
        if dict(self.state.limits or {}) != declared:
            self.state.limits = declared
            return True
        return False

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

    def _bounded_requires_shared(self) -> bool:
        """显式有界任务是否要求跨进程共享计数。

        判据：配了任一上限（`limited`）且本进程按**多进程语义**运行（`multiprocess=True`，
        编排器与 Worker 两条生产路径都这么传）。单进程（离线检查、单元测试、单进程 CLI）
        不受此约束——那里本地计数就是全部事实，不会出现"每个进程各一份额度"。
        """
        return bool(self.limited and self._multiprocess)

    def _shared_available(self) -> bool:
        """共享计数是否**可用**：直接探一次（GET 计数键）。

        不能只看 `_established`（那是"写路径成功用过一次"的标记）：第一次预留时它还是
        False，而恰恰是这一次需要知道"共享账本到底能不能用"。探测失败即标记后端降级
        （后续不再重复付连接超时）。
        """
        r = self._r()
        if r is None:
            return False
        try:
            r.get(self._keys()["calls"])
            self._established = True
            return True
        except Exception as exc:                 # noqa: BLE001 - 探测失败即视为不可用
            logger.warning("共享账本探测失败，按不可用处理：%s", str(exc)[:100])
            self._mark_backend(False)
            return False

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
        st.tokens_actual = int(raw.get("tokens_actual") or 0)
        st.tokens_unknown_calls = int(raw.get("tokens_unknown_calls") or 0)
        st.seq = int(raw.get("seq") or 0)
        st.ledger_id = str(raw.get("ledger_id") or "")
        st.open_tickets = dict(raw.get("open_tickets") or {})
        st.unsettled_tickets = dict(raw.get("unsettled_tickets") or {})
        st.stages = dict(raw.get("stages") or {})
        st.limits = dict(raw.get("limits") or {})
        st.rejected_transitions = dict(raw.get("rejected_transitions") or {})
        st.persist_uncertain = bool(raw.get("persist_uncertain"))
        return st

    _COUNT_KEYS = ("calls_reserved", "calls_settled", "calls_unsettled",
                   "tokens_reserved", "tokens_settled", "tokens_unsettled",
                   "tokens_actual", "tokens_unknown_calls")
    _STAGE_SUM_KEYS = ("reserved", "settled", "unsettled", "tokens",
                       "tokens_reserved", "refunded")

    @staticmethod
    def _copy_state(st: "BudgetState") -> "BudgetState":
        """深拷贝账本状态（增量基线用；dict 字段必须复制，不能共享引用）。"""
        return BudgetState(
            started_at=st.started_at,
            calls_reserved=st.calls_reserved, calls_settled=st.calls_settled,
            calls_unsettled=st.calls_unsettled,
            tokens_reserved=st.tokens_reserved, tokens_settled=st.tokens_settled,
            tokens_unsettled=st.tokens_unsettled,
            tokens_actual=st.tokens_actual,
            tokens_unknown_calls=st.tokens_unknown_calls, seq=st.seq,
            ledger_id=st.ledger_id,
            open_tickets=json.loads(json.dumps(st.open_tickets or {})),
            unsettled_tickets=json.loads(json.dumps(st.unsettled_tickets or {})),
            stages=json.loads(json.dumps(st.stages or {})),
            limits=dict(st.limits or {}),
            rejected_transitions=json.loads(json.dumps(st.rejected_transitions or {})),
            persist_uncertain=bool(st.persist_uncertain),
        )

    def _mine(self) -> dict:
        """本进程自加载以来的增量快照（只记自己的，供 `writers` 合并）。

        主进程/Worker/评审进程各有一份账本，`budget_state.json` 此前是"后写覆盖"——
        最后一个保存的进程把自己的视图整份写下去，别的进程记的 llm/backup 阶段计数
        就消失了（实机：一次运行的文件里只剩 step 阶段，7 次供应商请求全无痕迹）。
        现在每个进程只写**自己的增量**，文件里的总数由所有进程的增量相加得到。
        """
        st, base = self.state, self._baseline
        counters = {k: int(getattr(st, k) or 0) - int(getattr(base, k) or 0)
                    for k in self._COUNT_KEYS}
        stages: dict = {}
        for name, entry in (st.stages or {}).items():
            e = entry if isinstance(entry, dict) else {}
            b = (base.stages or {}).get(name)
            b = b if isinstance(b, dict) else {}
            d: dict = {}
            for k in self._STAGE_SUM_KEYS:
                v = int(e.get(k) or 0) - int(b.get(k) or 0)
                if v:
                    d[k] = v
            for k in ("open", "last_note", "last_failed", "last_progress",
                      "last_progress_at", "last_at", "first_at"):
                if e.get(k) != b.get(k) and e.get(k) is not None:
                    d[k] = e.get(k)
            if d:
                stages[name] = d
        tag = self._tag
        tickets = {t: r for t, r in (st.open_tickets or {}).items()
                   if str(t).endswith(tag)}
        unsettled = {t: r for t, r in (st.unsettled_tickets or {}).items()
                     if str(t).endswith(tag)}
        rejected: dict = {}
        for kind, entry in (st.rejected_transitions or {}).items():
            e = entry if isinstance(entry, dict) else {}
            b = (base.rejected_transitions or {}).get(kind)
            b = b if isinstance(b, dict) else {}
            d = {"count": int(e.get("count") or 0) - int(b.get("count") or 0)}
            if e.get("last") != b.get("last"):
                d["last"] = e.get("last") or ""
            if d["count"] or d.get("last"):
                rejected[kind] = d
        return {"counters": counters, "stages": stages, "open_tickets": tickets,
                "unsettled_tickets": unsettled, "rejected_transitions": rejected,
                "seq": int(st.seq or 0) - int(base.seq or 0)}

    @classmethod
    def _merge_writers(cls, writers: dict) -> dict:
        """把各进程的增量快照合并成**整次运行**的视图（计数相加、票据并集）。"""
        total = {k: 0 for k in cls._COUNT_KEYS}
        stages: dict = {}
        open_tickets: dict = {}
        unsettled: dict = {}
        rejected: dict = {}
        seq = 0
        for snap in (writers or {}).values():
            if not isinstance(snap, dict):
                continue
            for k in cls._COUNT_KEYS:
                total[k] += int((snap.get("counters") or {}).get(k) or 0)
            seq = max(seq, int(snap.get("seq") or 0))
            for name, d in (snap.get("stages") or {}).items():
                if not isinstance(d, dict):
                    continue
                e = stages.setdefault(name, {})
                for k in cls._STAGE_SUM_KEYS:
                    if d.get(k):
                        e[k] = int(e.get(k) or 0) + int(d.get(k) or 0)
                for k in ("first_at",):
                    if d.get(k) is not None:
                        e[k] = min(float(e.get(k) or d[k]), float(d[k]))
                for k in ("last_at", "last_note", "last_failed", "last_progress",
                          "last_progress_at"):
                    if d.get(k) is not None:
                        e[k] = d[k]
                # `open` 是本进程的在飞票据列表：并集（不同进程的票据号不重复）
                if d.get("open"):
                    e["open"] = list(e.get("open") or []) + list(d["open"])
            for src, dst in ((snap.get("open_tickets"), open_tickets),
                             (snap.get("unsettled_tickets"), unsettled)):
                for t, r in (src or {}).items():
                    dst.setdefault(t, r)
            for kind, d in (snap.get("rejected_transitions") or {}).items():
                if not isinstance(d, dict):
                    continue
                e = rejected.setdefault(kind, {"count": 0, "last": ""})
                e["count"] = int(e.get("count") or 0) + int(d.get("count") or 0)
                if d.get("last"):
                    e["last"] = d["last"]
        return {"counters": total, "stages": stages, "open_tickets": open_tickets,
                "unsettled_tickets": unsettled, "rejected_transitions": rejected,
                "seq": seq}

    def _save(self) -> bool:
        """落盘一次；返回**是否可信地写成功**（拿不到锁或写失败 → False）。

        整段"读 writers → 合并 → 写 tmp → replace"必须在**跨进程锁**内完成：
        只有进程内锁时，两个进程会读到同一份旧快照、各自合并、互相覆盖（成功预留两次，
        最终快照只剩一次——协作审查在内存文件系统里确定性复现）。

        拿不到锁时**不写**：锁超时说明有别的进程正在读改写，这时照写就是无锁覆盖，
        会把对方刚写进去的增量抹掉。宁可这次快照少记自己的增量（共享计数仍是真源），
        也不能破坏别人的账。
        """
        with cross_process_file_lock(self.path) as got:
            if not got:
                self._persist_note("落盘锁等待超时（本次不写，避免无锁覆盖其他进程的增量）")
                return False
            return self._save_locked()

    def _persist_note(self, why: str) -> None:
        """记录一次"落盘结果不可信"；只提示一次，避免刷日志。"""
        if not self._persist_uncertain:
            logger.warning("预算账本落盘状态不确定（task=%s）：%s；"
                           "有界多进程任务将拒绝新付费请求", self.root_task_id, why)
        self._persist_uncertain = True

    def _save_locked(self) -> bool:
        # 读回磁盘上的 `writers`（同一账本身份才继承）：本进程的增量替换自己的那一份，
        # 其余进程的原样保留——这样"最后保存的进程"不会再抹掉别人的计数。
        writers: dict = {}
        try:
            disk = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except Exception:
            disk = {}
        # 09-23：初始化没拿到锁时本进程保持**空身份**；等到这次拿到锁再落盘时，
        # 必须先在锁内重读、**采纳已有身份或建立新身份**，再合并 writers——
        # 否则空身份与磁盘身份不匹配，旧 writers（别人的计数）会被当成上一轮丢掉，
        # 并且会把空身份写进账本。空身份一律不普通保存。
        if not str(self.state.ledger_id or ""):
            disk_id = str(disk.get("ledger_id") or "")
            if disk_id:
                self.state.ledger_id = disk_id
                self._baseline = self._copy_state(self.state)
            else:
                self.state.ledger_id = uuid.uuid4().hex[:12]
                self._baseline = self._copy_state(self.state)
        if str(disk.get("ledger_id") or "") == str(self.state.ledger_id or ""):
            writers = dict(disk.get("writers") or {})
        writers[self._tag] = self._mine()
        merged = self._merge_writers(writers)
        # 落盘不可信是**账本级**的事实：任一进程（含磁盘上记着的）报过不确定，
        # 后来的进程不能把它改回"可信"——否则一次锁超时会被下一次成功写入洗掉。
        persist_uncertain = bool(self._persist_uncertain
                                 or (disk.get("persist_uncertain")
                                     if str(disk.get("ledger_id") or "")
                                     == str(self.state.ledger_id or "") else False))
        try:
            payload = self._payload(merged, writers, persist_uncertain)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except Exception as exc:
            # 序列化失败与写盘失败同等对待：账本没能落地 → 状态不确定
            self._persist_note(f"写入失败（{str(exc)[:80]}）")
            logger.warning("预算账本落盘失败（task=%s）：%s", self.root_task_id, str(exc)[:100])
            return False

    def _payload(self, merged: dict, writers: dict, persist_uncertain: bool) -> dict:
        """账本快照的完整内容（写盘前可再校验/序列化）。"""
        return {
            "root_task_id": self.root_task_id,
            "started_at": self.state.started_at,
            "calls_reserved": merged["counters"]["calls_reserved"],
            "calls_settled": merged["counters"]["calls_settled"],
            "calls_unsettled": merged["counters"]["calls_unsettled"],
            "tokens_reserved": merged["counters"]["tokens_reserved"],
            "tokens_settled": merged["counters"]["tokens_settled"],
            "tokens_unsettled": merged["counters"]["tokens_unsettled"],
            "tokens_actual": merged["counters"]["tokens_actual"],
            "tokens_unknown_calls": merged["counters"]["tokens_unknown_calls"],
            "seq": max(int(self.state.seq or 0), int(merged.get("seq") or 0)),
            "ledger_id": self.state.ledger_id,
            "open_tickets": merged["open_tickets"],
            "unsettled_tickets": merged["unsettled_tickets"],
            "stages": merged["stages"],
            "rejected_transitions": merged["rejected_transitions"],
            # 逐进程增量（审计用）：总数对不上时能看出是谁记的
            "writers": writers,
            "limits": {
                "max_seconds": self.limits.max_seconds,
                "max_calls": self.limits.max_calls,
                "max_tokens": self.limits.max_tokens,
            },
            # 落盘可信度：True 表示本次进程至少有一次"该写没写成"（锁超时/写失败）。
            # 有界多进程任务据此拒绝新付费请求，收尾核对时也据此判断快照是否可信。
            "persist_uncertain": persist_uncertain,
            "updated_at": time.time(),
        }

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
        """发送前原子预留；**配置了上限且不足**时抛 `BudgetExceeded`。

        D5：**记账与管制分开**——不配上限也如实计数（账本要能核对"供应商实际收到
        多少次请求"），上限只决定"是否拒绝"。0 仍然是不限：不拒绝、也不拿别的
        配置兜底成硬截止。
        """
        calls = max(1, int(calls or 1))
        tokens = max(0, int(tokens or 0))
        with self._lock:
            needs_shared = bool(self.limited and self._bounded_requires_shared())
            if self.limited:
                # 显式有界任务（配了任一上限）+ 多进程语义 + 共享计数不可用 →
                # **fail closed**：不能"静默退成每个进程各一份上限"（那样 3 个 Worker
                # 就是 3 倍额度）。无模型的离线检查/工作台读取不受影响（它们不 reserve）。
                if needs_shared and not self._shared_available():
                    raise BudgetExceeded(
                        "显式有界任务的预算要求跨进程共享计数，但共享账本（Redis）不可用："
                        "拒绝新付费请求（不得按每进程一份上限继续）")
                # 共享账本读得到、但本地落盘已经不可信（拿不到落盘锁或写入失败）：
                # 计数可能已被覆盖/丢失，此时"还剩多少额度"无从判断——同样拒绝，
                # 而不是拿一个可能是错的剩余额度继续花。
                if needs_shared and self._persist_uncertain:
                    raise BudgetExceeded(
                        "预算账本落盘状态不确定（共享计数可读但本地落盘失败或未取到锁）："
                        "拒绝新付费请求，未知状态待对账")
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
            if needs_shared and seq_remote is None:
                # 前面探到共享账本可用，这里却没能预留：多半是"加完计数才失败"，
                # 共享计数可能已被改动——不能按本地计数继续（那等于每进程一份额度）。
                self._persist_note("共享账本预留未成功返回（结果不确定）")
                raise BudgetExceeded(
                    "共享账本预留失败（结果不确定）：拒绝新付费请求，未知状态待对账")
            # 本地字段只记"本进程预留了多少"（文件是快照）；跨进程的总额度判断
            # 一律走 `calls_committed()`/`tokens_committed()` 读共享计数
            self.state.seq += 1
            seq = seq_remote if seq_remote is not None else self.state.seq
            self.state.calls_reserved += calls
            self.state.tokens_reserved += tokens
            ticket = f"{stage}-{seq}-{self._tag}"
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
            if not self._save() and needs_shared:
                # 落盘不可信：这次预留**作废**（请求不发出），并把共享计数改回原样，
                # 之后所有付费请求一律拒绝（"还剩多少额度"已无从判断）。
                # 无界任务不受影响：没有上限可越，继续按本地+共享计数如实记。
                self._undo_reservation_locked(ticket, stage, calls, tokens)
                raise BudgetExceeded(
                    "预算账本落盘失败（结果不确定）：拒绝新付费请求，未知状态待对账")
        return ticket

    def _undo_reservation_locked(self, ticket: str, stage: str,
                                 calls: int, tokens: int) -> None:
        """撤销一次刚做、但没能可信落盘的预留（必须在 `self._lock` 内调用）。"""
        self.state.open_tickets.pop(ticket, None)
        self.state.calls_reserved = max(0, self.state.calls_reserved - calls)
        self.state.tokens_reserved = max(0, self.state.tokens_reserved - tokens)
        entry = self.state.stages.get(stage)
        if isinstance(entry, dict):
            entry["reserved"] = max(0, int(entry.get("reserved") or 0) - calls)
            entry["tokens_reserved"] = max(0, int(entry.get("tokens_reserved") or 0)
                                          - tokens)
        # 共享计数按"请求没发生"改回：这次没有发出任何请求，额度不该被占
        self._release_calls_remote(calls)
        self._release_tokens_remote(tokens)
        self._persist_note("预留后落盘失败，已撤销该次预留")

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
               note: str = "", usage_known: bool = False) -> None:
        """结算一次调用（成功或失败都要结算——失败也花了钱）。

        互斥迁移：只有仍 `open` 的票据能结算。已经转 `unsettled` 的票据（取消时
        转过去的、可能仍在计费）**不得**再记成 `settled`——两边都记等于把
        "这个调用到底停下没有"藏起来；虚构票据同样拒绝。

        `usage_known`：`tokens` 是供应商**实际用量**（True）还是**预留上界**（False）。
        两者分开累计（`tokens` 与 `tokens_actual`）：未知用量**不填 0**，而是照实记在
        上界里并计一次 `tokens_unknown_calls`——否则"没取到用量"会被读成"这次没花钱"。
        """
        if not ticket:
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
            # `_take_open` 已把预留上界整笔退回共享计数（delta = -U）；这里只补记**实际
            # 用量**（+A），合起来才是"上界换成实际"。此前两处都按 (A-U) 记，实际用量被
            # 扣了两遍——共享 token 计数会一路走负，配了 `max_tokens` 也永远拒不了
            # （实机 ui-706c5ef4a5：`wm:budget:…:tokens = -63188`）。
            self._release_tokens_remote(0, actual=actual)
            entry = self.state.stages.get(stage)
            if isinstance(entry, dict):
                entry["open"] = [t for t in (entry.get("open") or [])
                                 if str(t.get("ticket")) != ticket]
                entry["settled"] = int(entry.get("settled") or 0) + 1
                entry["tokens"] = int(entry.get("tokens") or 0) + actual
                if usage_known:
                    entry["tokens_actual"] = int(entry.get("tokens_actual") or 0) + actual
                else:
                    entry["tokens_unknown_calls"] = \
                        int(entry.get("tokens_unknown_calls") or 0) + 1
                entry["tokens_reserved"] = max(
                    0, int(entry.get("tokens_reserved") or 0) - upper)
                entry["last_at"] = time.time()
                if not ok:
                    entry["last_failed"] = True
                if note:
                    entry["last_note"] = str(note)[:200]
            self.state.calls_settled += 1
            self.state.tokens_settled += actual
            if usage_known:
                self.state.tokens_actual += actual
            else:
                self.state.tokens_unknown_calls += 1
            self._save()

    def refund(self, ticket: str, *, note: str = "") -> None:
        """发送前失败 → 退回预留（这次调用没有发生）。"""
        if not ticket:
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
        if not ticket:
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
        with self._lock:
            tickets = [t for t, rec in self.state.open_tickets.items()
                       if str((rec or {}).get("stage") or "") == stage]
        moved = 0
        for t in tickets:
            if self.mark_unsettled(t, reason=reason):
                moved += 1
        return moved

    def note_progress(self, stage: str, detail: dict | None = None) -> None:
        """记一次**有效进展**（心跳不算）：等待对象、最近有效进展由调用方给出。

        只在配置了上限时记录：这是诊断信息（账本核对靠 reserve/settle 的票据），
        不限额度时每个步骤都写一次文件没有取证价值，只是 I/O。
        """
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
            # 落盘可信度：True = 本进程至少有一次该写的快照没写成（锁超时/写失败），
            # 快照数字可能不是全部事实；有界多进程任务此时已停止发新付费请求。
            "persist_uncertain": bool(self._persist_uncertain or self.state.persist_uncertain),
            # reserved = **共享**已发出调用数（跨进程时含其它进程的预留）；
            # local_reserved = 本进程预留数（文件快照口径），两者并列不混淆
            "calls": {"reserved": self.calls_committed(),
                      "local_reserved": self.state.calls_reserved,
                      "settled": self.state.calls_settled,
                      "unsettled": self.state.calls_unsettled},
            "tokens": {"committed": self.tokens_committed(),
                       "open_upper": self.state.tokens_reserved,
                       "settled": self.state.tokens_settled,
                       "unsettled": self.state.tokens_unsettled,
                       # D5 夜间补修：实际用量与"没取到用量"分开——未知用量的那部分
                       # 记的是预留**上界**，不填 0（不把"没读到"当成"没花钱"）
                       "actual": self.state.tokens_actual,
                       "unknown_calls": self.state.tokens_unknown_calls},
            # 供应商请求数**单独一列**：`step` 阶段是步骤调度（调度票据不等于请求），
            # `llm`/`backup` 才是真实 provider 请求。核对上限时要看后者。
            "provider_requests": {
                "reserved": sum(int((e or {}).get("reserved") or 0)
                                for k, e in (self.state.stages or {}).items()
                                if k in ("llm", "backup")),
                "settled": sum(int((e or {}).get("settled") or 0)
                               for k, e in (self.state.stages or {}).items()
                               if k in ("llm", "backup")),
            },
            "steps_dispatched": int(
                ((self.state.stages or {}).get("step") or {}).get("settled") or 0),
            "remaining": self.remaining(),
            "limits": {"max_seconds": self.limits.max_seconds,
                       "max_calls": self.limits.max_calls,
                       "max_tokens": self.limits.max_tokens},
            # 口径声明（机器可读）：`max_calls` 扣的是**所有已预留票据**（含步骤调度），
            # 不是"供应商实际收到多少次请求"。核对供应商请求数看 `provider_requests`，
            # 核对步骤派发看 `steps_dispatched`——三者不得互相冒充。
            "count_basis": {"max_calls": "all_reserved_tickets",
                            "provider_requests": "stages:llm+backup",
                            "steps_dispatched": "stages:step"},
            # token 三项独立：预留上界 / 实际用量 / 未取到用量（不互相折算）
            "token_basis": {"open_upper": "reserved_upper_bound",
                            "actual": "provider_reported_usage",
                            "unknown_calls": "usage_not_readable"},
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
