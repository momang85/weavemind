# -*- coding: utf-8 -*-
"""任务执行上下文：根任务 / 步骤 / 派发三层身份，跨线程与跨进程都不丢。

背景（长期规划 4.1 与 L01）：派发时编排器把**合成 id 当 task_id** 下发
（`orchestrator_v2.dispatch` 里 `dispatch_id = f"{step_id}-{uuid4().hex[:8]}"`），
worker 侧 `set_task_context(task_id)` 于是把 LLM 台账记在派发键上——根任务台账
（`llm_usage_task:{root}`，指标页与费用页读的就是它）因此恒为空。三层身份必须
拆开显式传递：

    root_task_id   用户提交的那个任务：预算、台账、审计的归属
    step_id        计划里的步骤
    dispatch_id    本步骤的某一次派发（重试/重规划会产生多个）

设计约束：

- **wire 格式版本化**：字段只增不改，`schema_version` 随结构变化；
- **兼容与拒绝策略显式**：老消息（没有 `context`）在本地模式按"用派发 id 兜底并
  记缺口"处理，银行模式缺必要身份直接拒绝执行，不猜身份；
- **跨线程显式复制**：`contextvars` 不跨线程，`bind_to_current_context` 之外的
  线程必须自己绑定（见 `worker_base` 任务线程的处理）。
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

# 结构版本：新增字段时 +1，解码端按版本决定是否可读
SCHEMA_VERSION = 1
# 本实现支持的协议版本。**未知或格式错误的版本一律拒绝**，不当作旧消息放行。
SUPPORTED_SCHEMA_VERSIONS = (1,)


VALID_MODES = ("local", "bank")
# 用于区分"键不存在"与"键存在但值为 None"（后者是协议非法，不是旧消息）
_MISSING = object()


class IdentityConfigError(RuntimeError):
    """部署模式等配置非法：显式报错，不退回宽松策略。"""


def resolve_mode(mode: str | None = None) -> str:
    """规范化并校验身份模式：**显式传入的 mode 与环境变量走同一条规则**。

    只接受 local / bank（大小写不敏感、允许首尾空白）；其他取值抛 `IdentityConfigError`。
    显式传参不是"跳过校验"的理由——`admit_dispatch(..., mode="bnak")` 必须与
    环境变量写成 `bnak` 得到同样的拒绝。
    """
    raw = mode if mode is not None else os.environ.get("WEAVEMIND_IDENTITY_MODE")
    if raw is None or str(raw).strip() == "":
        raw = "local"
    norm = str(raw).strip().lower()
    if norm not in VALID_MODES:
        raise IdentityConfigError(
            f"未知的身份模式 {raw!r}：只支持 {' / '.join(VALID_MODES)}（不退回宽松策略）"
        )
    return norm


def default_mode() -> str:
    """部署口径（环境变量）：`WEAVEMIND_IDENTITY_MODE=bank` 时按银行要求校验，默认 local。"""
    return resolve_mode(None)

IDENTITY_FIELDS = (
    "root_task_id", "step_id", "dispatch_id", "trace_id",
    "tenant_id", "workspace_id", "actor_id",
    "deadline", "budget_ref", "policy_version",
)

# 本地模式的最小身份要求；银行模式要求完整四元组（长期规划 4.1）
REQUIRED_LOCAL = ("root_task_id",)
REQUIRED_BANK = ("tenant_id", "workspace_id", "actor_id", "root_task_id")


@dataclass
class TaskExecutionContext:
    root_task_id: str = ""
    step_id: str = ""
    dispatch_id: str = ""
    trace_id: str = ""
    tenant_id: str = ""
    workspace_id: str = ""
    actor_id: str = ""
    deadline: float = 0.0
    budget_ref: str = ""
    policy_version: str = ""
    schema_version: int = SCHEMA_VERSION

    # ── 序列化 ────────────────────────────────────────────────
    def to_wire(self) -> dict:
        return asdict(self)

    @classmethod
    def from_wire(cls, data: dict | None) -> "TaskExecutionContext":
        """解码：未知字段忽略、缺字段留空（由 `gaps()` 判定是否可执行）。"""
        src = data if isinstance(data, dict) else {}
        kwargs: dict = {}
        for name in IDENTITY_FIELDS:
            if name not in src:
                continue
            value = src[name]
            kwargs[name] = float(value) if name == "deadline" else str(value or "")
        ver = src.get("schema_version", SCHEMA_VERSION)
        try:
            kwargs["schema_version"] = int(ver)
        except (TypeError, ValueError):
            kwargs["schema_version"] = SCHEMA_VERSION
        return cls(**kwargs)

    # ── 判定 ──────────────────────────────────────────────────
    def missing(self, mode: str | None = None) -> list[str]:
        """返回当前模式下缺失的必要身份字段（空列表 = 可执行）。"""
        need = REQUIRED_BANK if (mode or default_mode()) == "bank" else REQUIRED_LOCAL
        return [name for name in need if not str(getattr(self, name, "") or "").strip()]

    def accounting_task_id(self) -> str:
        """LLM 台账/预算归属：优先根任务，缺时退回派发 id（本地兜底）。"""
        return self.root_task_id or self.dispatch_id

    def labels(self) -> dict:
        """日志/审计用的一行身份标签。"""
        return {
            "root": self.root_task_id, "step": self.step_id,
            "dispatch": self.dispatch_id, "trace": self.trace_id,
        }


def make_context(
    root_task_id: str,
    step_id: str = "",
    dispatch_id: str = "",
    trace_id: str = "",
    **extra,
) -> TaskExecutionContext:
    """构造上下文（编排器侧调用）。trace_id 缺省时用派发 id，便于跨进程对账。"""
    return TaskExecutionContext(
        root_task_id=str(root_task_id or ""),
        step_id=str(step_id or ""),
        dispatch_id=str(dispatch_id or ""),
        trace_id=str(trace_id or dispatch_id or ""),
        **{k: v for k, v in extra.items() if k in IDENTITY_FIELDS},
    )


def decode_dispatch(task: dict | None, mode: str | None = None) -> tuple[TaskExecutionContext | None, list[str]]:
    """从派发载荷解出上下文。返回 `(ctx, gaps)`。

    判定顺序（协议问题一律拒绝，身份缺失按模式处理）：

    1. **协议非法即拒绝**（本地/银行都一样）：
       - `context` 键存在但不是对象（含显式 `null`）——只有**缺键**才算旧消息；
       - `schema_version` 缺失、非 JSON 整数（布尔/小数/字符串都不接受，不做 `int()` 转换）、
         或不在 `SUPPORTED_SCHEMA_VERSIONS` 里。未知版本不能伪装成"旧消息"走兼容路径。
    2. **身份缺失按模式**：银行模式缺 tenant/workspace/actor/root 一律拒绝（ctx=None）；
       本地模式用 `task_id` 兜底并把缺口写进 `gaps`（可观测，由调用方记录，不静默）。
    3. 旧消息（**没有** `context` 键）走独立的本地兼容策略，`gaps` 里带 `context` 标记。

    `mode`（显式或环境）非法时抛 `IdentityConfigError`，不退回宽松策略。
    """
    payload = task if isinstance(task, dict) else {}
    m = resolve_mode(mode)
    legacy = "context" not in payload

    if not legacy:
        raw_ctx = payload.get("context")
        if not isinstance(raw_ctx, dict):
            return None, [f"context 不是对象（协议非法）：{type(raw_ctx).__name__}"]
        raw_ver = raw_ctx.get("schema_version", _MISSING)
        if raw_ver is _MISSING:
            return None, ["schema_version 缺失（协议非法）"]
        # 只接受 JSON 整数：bool 是 int 的子类（True 的 int() 是 1），小数/字符串一律拒绝
        if isinstance(raw_ver, bool) or not isinstance(raw_ver, int):
            return None, [f"schema_version 只接受整数：{raw_ver!r}（协议非法）"]
        if raw_ver not in SUPPORTED_SCHEMA_VERSIONS:
            return None, [f"不支持的协议版本 schema_version={raw_ver}（支持 {SUPPORTED_SCHEMA_VERSIONS}）"]
        ctx = TaskExecutionContext.from_wire(raw_ctx)
        if not ctx.dispatch_id:
            ctx.dispatch_id = str(payload.get("task_id") or "")
    else:
        ctx = TaskExecutionContext(dispatch_id=str(payload.get("task_id") or ""))
        ctx.root_task_id = ctx.dispatch_id          # 旧消息只有一个 id

    if not ctx.trace_id:
        ctx.trace_id = ctx.dispatch_id

    gaps = ctx.missing(m)
    if legacy:
        gaps.append("context")          # 明确记录"这条消息没有版本化上下文"
    if m == "bank" and gaps:
        return None, gaps
    return ctx, gaps


def admit_dispatch(task: dict | None, mode: str | None = None) -> tuple[TaskExecutionContext | None, list[str], str]:
    """Worker/Critic 接收边界的唯一判定：返回 `(ctx, gaps, refuse_reason)`。

    - `ctx is None` 表示**必须拒绝执行**（协议非法 / 银行缺身份 / 模式配置非法），
      `refuse_reason` 是可回传给人看的拒绝原因；
    - `gaps` 是"本地兼容或不完整身份"的可观测记录，**调用方必须落日志/回报**，
      不能像以前那样在 admit_dispatch 里丢掉。
    """
    try:
        ctx, gaps = decode_dispatch(task, mode)
    except IdentityConfigError as exc:
        return None, [], str(exc)
    if ctx is None:
        return None, list(gaps), "拒绝执行：" + "；".join(str(g) for g in gaps)
    return ctx, list(gaps), ""


def bind_llm_accounting(ctx: TaskExecutionContext) -> str:
    """把本派发的 LLM 台账归属写进 llm_client 的任务上下文。

    跨线程不自动传播：必须在**处理该任务的线程**里调用（`worker_base` 的任务线程、
    `async_worker_base` 的 async 处理入口各自绑定）。返回实际使用的归属 id。
    """
    from llm_client import set_task_context
    tid = ctx.accounting_task_id()
    set_task_context(tid)
    return tid


def clear_llm_accounting() -> None:
    try:
        from llm_client import clear_task_context
        clear_task_context()
    except Exception:
        pass
