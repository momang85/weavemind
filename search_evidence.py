# -*- coding: utf-8 -*-
"""P1-D：检索尝试与**采纳证据**分离（唯一实现，离线可测）。

背景（RUNTIME-1022 派单第四批）：实测任务的"检索失败"往往不是一次都没成功，
而是**前几轮已经取到可用来源，后面的重试/重规划/预算拒绝把结果覆盖掉**，
最后由摘要代做收尾——报告里看不到来源，诊断也只能证明"最终退化"。这里把两件
事拆开：

1. **尝试轨迹**（`search_attempts.json`）：每次派发（含重试、重规划）一条记录，
   记查询、状态、候选数、**准入数**、失败分类与说明；只追加、有上限。
2. **采纳证据**（`search_adopted.json`）：每步保留一份"已采纳检索结果"——
   只统计**通过准入**的来源（未准入候选不算有效证据），跨轮不被更差的结果覆盖；
   更差的那次只进尝试轨迹，并在采纳记录里留 `superseded` 痕迹。

预算按阶段显式分配（`stage_plan`，总和 = 任务级上限），后一阶段预留不能
被前一阶段吃光；预算读不到时调用方按旧行为处理（不改变默认语义）。

只写 URL/计数/状态/原因，不含提示词、响应体与密钥。
"""

from __future__ import annotations

import json
import os
from typing import Any

ATTEMPTS_FILE = "search_attempts.json"
ADOPTED_FILE = "search_adopted.json"

STAGE_INITIAL = "initial"      # 首轮派发
STAGE_RETRY = "retry"          # 同一步的契约/瞬时失败重试
STAGE_REPAIR = "repair"        # 反思轮补洞 / 重规划代做
STAGES = (STAGE_INITIAL, STAGE_RETRY, STAGE_REPAIR)

_MAX_ATTEMPTS = 200
_STATUS_OK = "SUCCESS"


def stage_plan(total_calls: int) -> dict[str, int]:
    """把任务级检索上限显式分给三个阶段（总和 ≤ total_calls，且 ≥1）。

    默认分配：首轮 4 / 重试 1 / 补洞 1（6 次上限）。上限越小越保守：3 次时
    首轮 2、重试 1、补洞 0；2 次时只给首轮。**后阶段预留**由 `stage_reserve`
    读取，重试前不会把补洞轮想用的次数花光。
    """
    try:
        total = max(1, int(total_calls))
    except (TypeError, ValueError):
        total = 1
    if total <= 2:
        return {STAGE_INITIAL: total, STAGE_RETRY: 0, STAGE_REPAIR: 0}
    repair = 1 if total >= 4 else 0
    retry = max(1, total // 5) if total - repair > 1 else 0
    initial = max(1, total - retry - repair)
    return {STAGE_INITIAL: initial, STAGE_RETRY: retry, STAGE_REPAIR: repair}


def environment_stage_plan(total_calls: int = 0, *,
                           plan: dict[str, int] | None = None) -> dict[str, int]:
    """环境变量覆盖（`WM_SEARCH_STAGE_PLAN="initial:4,retry:1,repair:1"`）。

    解析失败或总和超过上限时回退到 `stage_plan`：显式分配不能突破总体上限
    （"预算不能无限重置"）。返回的总和恒 ≤ total_calls。
    """
    base = dict(plan or stage_plan(total_calls))
    raw = str(os.environ.get("WM_SEARCH_STAGE_PLAN") or "").strip()
    if not raw:
        return base
    parsed: dict[str, int] = {}
    for part in raw.split(","):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k = k.strip().lower()
        if k not in STAGES:
            continue
        try:
            parsed[k] = max(0, int(v.strip()))
        except (TypeError, ValueError):
            continue
    if not parsed:
        return base
    out = {s: int(parsed.get(s, base.get(s, 0))) for s in STAGES}
    total = sum(out.values())
    cap = max(1, int(total_calls or total))
    if total > cap:                                # 超上限 → 回退默认分配
        return base
    return out


def stage_reserve(plan: dict[str, int], stage: str) -> int:
    """`stage` 之后各阶段预留的次数（重试前要留出来的额度）。"""
    order = list(STAGES)
    if stage not in order:
        return 0
    idx = order.index(stage)
    return sum(int((plan or {}).get(s, 0) or 0) for s in order[idx + 1:])


def candidates_of(result: Any) -> int:
    """结果里可抓取的候选 URL 条数（非 SUCCESS / 非法 JSON / 无 http url 都算 0）。"""
    if not isinstance(result, dict) or str(result.get("status") or "") != _STATUS_OK:
        return 0
    raw = result.get("result")
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0
    return sum(1 for it in data if isinstance(it, dict)
               and str(it.get("url") or "").startswith("http"))


def _items_of(result: Any) -> list[dict]:
    if not isinstance(result, dict) or str(result.get("status") or "") != _STATUS_OK:
        return []
    raw = result.get("result")
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [it for it in data if isinstance(it, dict)
            and str(it.get("url") or "").startswith("http")]


def admitted_of(result: Any, contract: Any = None) -> int:
    """**通过准入**的来源条数（未准入候选不算有效证据）。

    无契约主体（行情/统计类目标）时准入不适用 → 等价于候选数；公司研究目标
    一律走 `source_admission.admit_many`，只数 `admitted`（宏观背景/待核验/
    被剔除都不算）。
    """
    items = _items_of(result)
    if not items:
        return 0
    if contract is None:
        return len(items)
    try:
        import source_admission as sa
        fields = sa.contract_fields(contract)
        if not (fields.get("company") or fields.get("company_id")):
            return len(items)
        res = sa.admit_many(items, contract)
        return len(res.get("admitted") or [])
    except Exception:                              # noqa: BLE001 - 准入不可用按候选数（不误伤）
        return len(items)


def _score(rec: dict) -> tuple[int, int, int]:
    return (
        int(rec.get("admitted") or 0),
        int(rec.get("candidates") or 0),
        1 if str(rec.get("status") or "") == _STATUS_OK else 0,
    )


def attempt_record(*, round_no: int, step_id: str, stage: str,
                   capability: str, query: str = "", status: str = "",
                   candidates: int = 0, admitted: int = 0,
                   reason: str = "", at: str = "") -> dict:
    """一条尝试轨迹（写进 `search_attempts.json`，只追加）。"""
    return {
        "round": int(round_no or 0),
        "step_id": str(step_id or ""),
        "stage": str(stage or STAGE_INITIAL),
        "capability": str(capability or ""),
        "query": str(query or "")[:300],
        "status": str(status or "")[:40],
        "candidates": int(candidates or 0),
        "admitted": int(admitted or 0),
        "reason": str(reason or "")[:400],
        "at": str(at or ""),
    }


class AdoptedStore:
    """任务级"已采纳检索证据"索引（每步一份，跨轮不被更差结果覆盖）。"""

    def __init__(self, data: dict | None = None):
        self.items: dict[str, dict] = {}
        self.evidence_gap: dict | None = None
        if isinstance(data, dict):
            for k, v in (data.get("steps") or {}).items():
                if isinstance(v, dict):
                    self.items[str(k)] = dict(v)
            gap = data.get("evidence_gap")
            self.evidence_gap = dict(gap) if isinstance(gap, dict) else None

    # -- 采纳 --

    def offer(self, step_id: str, result: dict, *, contract: Any = None,
              round_no: int = 0, stage: str = STAGE_INITIAL,
              at: str = "") -> dict:
        """提交一次派发结果；返回 `{chosen, adopted, reason, previous_admitted}`。

        - 首次 → 采纳；
        - 新结果**严格更好**（准入数优先，其次候选数，其次状态）→ 采纳，旧记录
          进 `superseded`（不删除）；
        - 否则保留既有采纳证据，新结果只记 `attempts`（覆盖不了）。
        """
        new_rec = {
            "step_id": str(step_id or ""),
            "round": int(round_no or 0),
            "stage": str(stage or STAGE_INITIAL),
            "status": str((result or {}).get("status") or ""),
            "candidates": candidates_of(result),
            "admitted": admitted_of(result, contract),
            "at": str(at or ""),
            "result": dict(result or {}),
        }
        prev = self.items.get(new_rec["step_id"])
        if prev is None:
            self.items[new_rec["step_id"]] = new_rec
            return {"chosen": new_rec, "adopted": True,
                    "reason": "首次采纳", "previous_admitted": 0}
        prev_attempts = list(prev.get("attempts") or [])
        prev_attempts.append({k: new_rec[k] for k in
                              ("round", "stage", "status", "candidates",
                               "admitted", "at")})
        prev["attempts"] = prev_attempts[-20:]
        if _score(new_rec) > _score(prev):
            new_rec["superseded"] = list(prev.get("superseded") or []) + [{
                "round": prev.get("round"), "stage": prev.get("stage"),
                "status": prev.get("status"),
                "candidates": prev.get("candidates"),
                "admitted": prev.get("admitted"),
            }]
            new_rec["attempts"] = list(prev.get("attempts") or [])
            self.items[new_rec["step_id"]] = new_rec
            return {"chosen": new_rec, "adopted": True,
                    "reason": "新结果准入数更好",
                    "previous_admitted": int(prev.get("admitted") or 0)}
        self.items[new_rec["step_id"]] = prev
        return {"chosen": prev, "adopted": False,
                "reason": "保留既有采纳证据（新结果未更优）",
                "previous_admitted": int(prev.get("admitted") or 0)}

    # -- 视图 --

    def result_for(self, step_id: str) -> dict | None:
        rec = self.items.get(str(step_id or ""))
        if not rec:
            return None
        return dict(rec.get("result") or {})

    def totals(self) -> dict:
        return {
            "steps": len(self.items),
            "admitted": sum(int(r.get("admitted") or 0)
                            for r in self.items.values()),
            "candidates": sum(int(r.get("candidates") or 0)
                              for r in self.items.values()),
        }

    def set_gap(self, *, reason: str, missing: list[str] | None = None,
                round_no: int = 0, at: str = "") -> None:
        """记录"没有新增材料、停止重做"的证据缺口（供报告缺口行引用）。"""
        self.evidence_gap = {
            "reason": str(reason or "")[:400],
            "missing": [str(m)[:200] for m in (missing or [])][:20],
            "round": int(round_no or 0),
            "at": str(at or ""),
            "admitted_total": self.totals()["admitted"],
        }

    def to_dict(self) -> dict:
        return {
            "note": ("检索采纳证据：step_results 保留步级最优检索结果（candidates 原样"
                     "留给下游抓取/审计）；admitted 为通过准入的来源数，pending 需"
                     "抓正文核验才转正，不在此处冒充证据。尝试轨迹见 "
                     "search_attempts.json；被更优结果替换的旧记录留在 superseded。"),
            "totals": self.totals(),
            "steps": {k: {kk: vv for kk, vv in v.items() if kk != "result"}
                      for k, v in self.items.items()},
            "step_results": {k: v.get("result") for k, v in self.items.items()},
            "evidence_gap": self.evidence_gap,
        }

    def save(self, ws_dir: Any, *, tag: str = "") -> str:
        """落盘 `search_adopted.json`（字节稳定；失败不抛）。"""
        try:
            from pathlib import Path
            ws = Path(ws_dir)
            ws.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=1,
                                 sort_keys=True)
            (ws / ADOPTED_FILE).write_bytes(payload.encode("utf-8"))
            return str(ws / ADOPTED_FILE)
        except Exception:                          # noqa: BLE001 - 辅助文件不阻断主线
            return ""


def attempts_payload(attempts: list[dict], *, task_id: str = "",
                     at: str = "") -> dict:
    recs = [dict(a) for a in (attempts or []) if isinstance(a, dict)]
    return {
        "task_id": str(task_id or ""),
        "generated_at": str(at or ""),
        "note": ("每次检索派发一条：阶段/状态/候选数/准入数/原因。采纳证据见 "
                 "search_adopted.json——未准入候选不计入有效证据。"),
        "count": len(recs),
        "attempts": recs[-_MAX_ATTEMPTS:],
    }


def save_attempts(ws_dir: Any, attempts: list[dict], *, task_id: str = "",
                  at: str = "") -> str:
    """落盘 `search_attempts.json`（有上限；失败不抛）。"""
    try:
        from pathlib import Path
        ws = Path(ws_dir)
        ws.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            attempts_payload(attempts, task_id=task_id, at=at),
            ensure_ascii=False, indent=1, sort_keys=True)
        (ws / ATTEMPTS_FILE).write_bytes(payload.encode("utf-8"))
        return str(ws / ATTEMPTS_FILE)
    except Exception:                              # noqa: BLE001
        return ""


def load_adopted(ws_dir: Any) -> AdoptedStore:
    """从工作区读回采纳索引（读不到返回空索引，不抛）。"""
    try:
        from pathlib import Path
        p = Path(ws_dir) / ADOPTED_FILE
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            store = AdoptedStore(data)
            for step_id, res in ((data or {}).get("step_results") or {}).items():
                rec = store.items.get(str(step_id))
                if rec is not None and isinstance(res, dict):
                    rec["result"] = res
            return store
    except Exception:                              # noqa: BLE001
        pass
    return AdoptedStore()


def stop_decision(*, stale_rounds: int, max_stale_rounds: int = 1,
                  new_search_material: bool = False) -> tuple[bool, str]:
    """无收益停损：连续 N 轮没有新增采纳来源、且计划里没有新的检索材料 → 停。

    只做判定（纯函数），由编排器决定是否跳过下一轮昂贵重做。
    """
    if new_search_material:
        return False, ""
    need = max(1, int(max_stale_rounds or 1))
    if int(stale_rounds or 0) >= need:
        return True, ("连续 %d 轮没有新增合格来源，且计划中没有新的检索材料："
                      "停止重做，按证据缺口如实收口" % int(stale_rounds or 0))
    return False, ""
