"""Progress push helpers for orchestrator → Redis → web_ui → frontend.

除了通用的 `push_progress`，这里提供**统一的阶段进度源**：规划/评审/反思/执行
四类阶段都通过 phase_* 上报"进入 / 心跳 / 结束"。

动机：进度此前只在步骤执行器内部维护（`last_progress` 三个更新点），规划、评审、
反思阶段的 LLM 调用完全不进模型。实测某个规划调用卡了 6 分钟，控制台只有
"Planning (LLM thinking)…"，没有任何"还在跑 / 已等待多久"的信号。

`call_with_heartbeat` 解决"阻塞调用期间无法上报"的问题：把 LLM 调用放进工作线程，
调用方按间隔发心跳（默认 15s，`WM_PHASE_HEARTBEAT_SECONDS` 可调）。
"""
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_PHASE_LOCK = threading.Lock()
# task_id → {"phase": str, "started_at": float, "last_beat": float, "attempt": int|None}
_PHASE_STATE: dict[str, dict] = {}


def _heartbeat_interval() -> float:
    try:
        return max(5.0, float(os.environ.get("WM_PHASE_HEARTBEAT_SECONDS", "15") or 15))
    except Exception:
        return 15.0


def push_progress(messaging, task_id: str, update_type: str, payload: dict) -> None:
    """Publish partial progress to Redis. web_ui listener picks this up
    and merges into _task_results. Frontend polls every 2s and renders."""
    try:
        msg = {
            "task_id": task_id,
            "type": update_type,
            "payload": payload,
        }
        messaging.publish("orchestrator:response", msg)
        messaging.publish("orchestrator:progress", msg)
    except Exception as exc:
        logger.warning("push_progress failed for %s: %s", task_id, exc)


def _phase_event(messaging, task_id: str, phase: str, state: str,
                 detail: str = "", attempt=None, elapsed: float | None = None) -> None:
    payload = {
        "type": "phase",
        "agent": "orchestrator",
        "phase": str(phase),
        "state": state,
        "message": detail,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }
    if attempt is not None:
        payload["attempt"] = attempt
    if elapsed is not None:
        payload["elapsed_seconds"] = round(float(elapsed), 1)
    push_progress(messaging, task_id, "log", payload)


def phase_begin(messaging, task_id: str, phase: str, detail: str = "",
                attempt=None) -> None:
    """阶段开始：记录起点并发一条可读事件。"""
    now = time.time()
    with _PHASE_LOCK:
        _PHASE_STATE[task_id] = {
            "phase": str(phase), "started_at": now, "last_beat": now,
            "attempt": attempt,
        }
    _phase_event(messaging, task_id, phase, "begin",
                 detail or f"{phase} 开始" + (f"（第 {attempt} 次尝试）" if attempt else ""),
                 attempt=attempt, elapsed=0.0)


def phase_heartbeat(messaging, task_id: str, phase: str = "",
                    detail: str = "") -> None:
    """阶段心跳：更新 last_beat 并告知已等待时长（阻塞调用期间由看护线程调用）。"""
    now = time.time()
    with _PHASE_LOCK:
        state = _PHASE_STATE.get(task_id) or {}
        if phase:
            state["phase"] = str(phase)
        started = float(state.get("started_at") or now)
        state["last_beat"] = now
        _PHASE_STATE[task_id] = state
        elapsed = now - started
    current = phase or state.get("phase") or ""
    _phase_event(messaging, task_id, current, "heartbeat",
                 detail or f"{current} 进行中（已等待 {elapsed:.0f}s）",
                 attempt=state.get("attempt"), elapsed=elapsed)


def phase_end(messaging, task_id: str, phase: str = "", ok: bool = True,
              detail: str = "") -> None:
    """阶段结束：清理状态并给出耗时。"""
    now = time.time()
    with _PHASE_LOCK:
        state = _PHASE_STATE.pop(task_id, {}) or {}
    started = float(state.get("started_at") or now)
    current = phase or state.get("phase") or ""
    _phase_event(messaging, task_id, current, "end",
                 detail or f"{current} {'完成' if ok else '失败'}"
                           f"（耗时 {now - started:.0f}s）",
                 attempt=state.get("attempt"), elapsed=now - started)


def phase_state(task_id: str) -> dict:
    """读取某任务的阶段快照（供看门狗/测试）。"""
    with _PHASE_LOCK:
        return dict(_PHASE_STATE.get(task_id) or {})


def call_with_heartbeat(messaging, task_id: str, phase: str, fn, *args,
                        interval: float | None = None, **kwargs):
    """在阻塞调用期间按间隔上报阶段心跳，返回 `fn(*args, **kwargs)` 的结果。

    调用异常原样抛出（由调用方决定重试/降级）；心跳线程始终会被清理。
    """
    # 兼容性：`deadline` 是后加的调用级预算参数，若目标可调用对象不接受它
    # （自定义客户端/测试打桩），自动去掉而不是让调用直接失败。
    if "deadline" in kwargs:
        try:
            import inspect
            params = inspect.signature(fn).parameters
            has_var_kw = any(
                p.kind == p.VAR_KEYWORD for p in params.values()
            )
            if not has_var_kw and "deadline" not in params:
                kwargs.pop("deadline")
        except Exception:
            pass

    result_box: dict = {}

    def _runner():
        try:
            result_box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # 原样回抛给调用线程
            result_box["error"] = exc

    beat = interval if interval is not None else _heartbeat_interval()
    thread = threading.Thread(target=_runner, daemon=True,
                              name=f"phase-{phase}-{task_id}")
    thread.start()
    while thread.is_alive():
        thread.join(timeout=beat)
        if thread.is_alive():
            try:
                phase_heartbeat(messaging, task_id, phase)
            except Exception:
                pass
    if "error" in result_box:
        raise result_box["error"]
    return result_box.get("value")
