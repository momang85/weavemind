# -*- coding: utf-8 -*-
"""提示词注册表：LLM 自迭代（分析输出→总结问题→改进提示词）的落点。

- 基线 v1 是源码里的默认提示词；自迭代产出的改进版写入 prompts/overrides.json。
- 各环节（planner / 步骤信封 / worker 系统提示词 / 反思）在组词时先查注册表，
  有覆盖则用覆盖版，否则用默认版。默认空注册表 = 与旧行为完全一致。
"""

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()


def _overrides_path() -> Path:
    env = os.environ.get("WEAVEMIND_PROMPTS_DIR") or ""
    if env:
        d = Path(env)
    else:
        d = Path(__file__).resolve().parent / "prompts"
    return d / "overrides.json"


def load_overrides() -> dict:
    """读取全部覆盖。文件缺失/损坏时返回 {}。"""
    p = _overrides_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_prompt(key: str, default: str) -> str:
    """取提示词：注册表有覆盖则【追加】在默认提示词之后（保留原契约，
    只叠加自迭代改进），否则返回默认版。"""
    ov = load_overrides().get(key)
    if isinstance(ov, dict) and str(ov.get("prompt") or "").strip():
        return str(default) + "\n\n【自迭代改进】" + str(ov["prompt"]).strip()
    return default


def _validate_fix(key: str, prompt: str, rationale: str) -> list[str]:
    """安全校验 LLM 改进版提示词：不合格则不写入，避免自迭代把系统改坏。"""
    issues: list[str] = []
    if not prompt or len(prompt) < 40:
        issues.append("提示词过短")
    if len(prompt) > 6000:
        issues.append("提示词过长")
    if not any(k in prompt for k in ("角色", "受众", "输出", "要求", "标准", "格式", "规则", "必须", "禁止")):
        issues.append("缺少角色/受众/输出/要求等关键段")
    low = prompt.lower()
    if any(b in low for b in ("rm -rf", "del /s", "os.remove(", "shutil.rmtree", "drop table")):
        issues.append("含危险操作示例")
    if not str(rationale or "").strip():
        issues.append("缺少改进理由")
    return issues


def record_override(
    key: str, prompt: str, rationale: str,
    trigger_task: str = "", version_base: int = 1,
) -> tuple[bool, list[str]]:
    """写入一条覆盖（版本 +1）。返回 (是否成功, 问题列表)。"""
    issues = _validate_fix(key, prompt, rationale)
    if issues:
        return False, issues
    with _LOCK:
        p = _overrides_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = load_overrides()
        prev = data.get(key) or {}
        if not prev:
            # 源码默认即 v1，第一次覆盖从 v2 开始（v1→v2 迭代语义）
            ver = version_base + 1
        else:
            try:
                ver = int(prev.get("version") or 0) + 1
            except (TypeError, ValueError):
                ver = version_base + 1
        data[key] = {
            "prompt": prompt,
            "version": ver,
            "rationale": str(rationale)[:500],
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "trigger_task": str(trigger_task)[:40],
        }
        try:
            p.write_text(
                json.dumps(data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            return True, []
        except Exception as exc:
            return False, [f"写入失败: {exc}"]


def summary() -> dict:
    """供前端/日志展示的覆盖摘要。"""
    data = load_overrides()
    return {
        "keys": list(data.keys()),
        "count": len(data),
        "items": [
            {"key": k, "version": v.get("version"), "applied_at": v.get("applied_at"),
             "trigger_task": v.get("trigger_task")}
            for k, v in data.items()
        ],
    }
