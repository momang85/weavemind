# -*- coding: utf-8 -*-
"""P1-4 失败诊断结构化：StepDiagnosis 数据结构 + step_failure.json 读写。

设计要点：
- 诊断在替换步骤执行【完成后】落盘，replacement_outcome 已知，
  反思重做时不需要再猜"替换有没有用"；
- 反思 prompt / 重做指令只消费结构化字段（error_type / tried_alternatives /
  suggestion / replacement_outcome），不再把自然语言错误文本拼进 worker 指令，
  从源头消除"报告抄反馈"类泄漏；
- error_snippet 仅用于本地诊断审计，绝不进入反思 prompt / worker 指令。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

FAILURE_FILE = "step_failure.json"


@dataclass
class StepDiagnosis:
    """单个步骤失败的确定性诊断记录。"""

    step_id: str
    capability: str
    error_type: str
    tried_alternatives: list[str] = field(default_factory=list)
    suggestion: str = ""
    timestamp: str = ""
    replacement_step_id: str = ""
    replacement_outcome: str = ""
    # 仅本地审计用，绝不进入反思 prompt / worker 指令
    error_snippet: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StepDiagnosis":
        return cls(
            step_id=str(d.get("step_id") or ""),
            capability=str(d.get("capability") or ""),
            error_type=str(d.get("error_type") or ""),
            tried_alternatives=list(d.get("tried_alternatives") or []),
            suggestion=str(d.get("suggestion") or ""),
            timestamp=str(d.get("timestamp") or ""),
            replacement_step_id=str(d.get("replacement_step_id") or ""),
            replacement_outcome=str(d.get("replacement_outcome") or ""),
            error_snippet=str(d.get("error_snippet") or ""),
        )


def step_failure_path(task_id: str) -> Path:
    """{task_dir}/step_failure.json，与 acceptance_report.json 同层。"""
    try:
        from workspace import task_workspace
        return task_workspace(task_id) / FAILURE_FILE
    except Exception:
        return Path(FAILURE_FILE)


def read_step_failures(task_id: str) -> list[StepDiagnosis]:
    """读取任务的失败诊断列表（按写入顺序，最新在后）。"""
    p = step_failure_path(task_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out: list[StepDiagnosis] = []
    for item in data:
        if isinstance(item, dict):
            try:
                out.append(StepDiagnosis.from_dict(item))
            except Exception:
                continue
    return out


def write_step_failure(task_id: str, diag: StepDiagnosis) -> None:
    """按 step_id 去重追加：同一任务内同一步骤重做时覆盖旧诊断。"""
    p = step_failure_path(task_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = [d for d in read_step_failures(task_id) if d.step_id != diag.step_id]
        existing.append(diag)
        p.write_text(
            json.dumps([d.to_dict() for d in existing], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception:
        pass
