# -*- coding: utf-8 -*-
"""错误模式库（Roadmap 余项③）：跨任务聚合 step_failure.json，
按 (capability, error_type) 聚类为修复模板，供反思 prompt 注入。

对标标准：P1-4 失败诊断结构化的上层闭环——诊断落盘后，
本模块把"同类错误→高频建议→修复成功率"固化为可复用模板；
后续任务遇到同型失败时，反思直接拿到经过验证的修复方向。

设计要点：
- 只消费结构化字段（error_type / suggestion / tried_alternatives /
  replacement_outcome），绝不触碰 error_snippet；
- 聚合结果持久化到 error_patterns.json（运行时产物，gitignore）；
- 任何失败静默降级，绝不影响任务主线；
- 修复成功率 = 该模式中 replacement_outcome 为成功/SUCCESS 的占比。
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

PATTERN_FILE = "error_patterns.json"
_MAX_PATTERNS = int(os.environ.get("ERROR_PATTERN_MAX", "50"))
_SUCCESS_MARKERS = ("success", "ok", "pass", "有效", "成功", "修复", "resolved")


def _pattern_path() -> Path:
    """error_patterns.json 放在工作区根（与 consolidation_stats.json 同层）。"""
    try:
        from workspace import WORKSPACE_ROOT
        return WORKSPACE_ROOT / PATTERN_FILE
    except Exception:
        return Path(PATTERN_FILE)


def _iter_task_failure_files(root: Path | None = None):
    """遍历所有任务的 step_failure.json。支持 projects/<p>/<task>/ 与新平铺布局。"""
    base = root or _pattern_path().parent
    if not base.exists():
        return
    # 新布局：projects/<project>/<task_id>/step_failure.json
    proj = base / "projects"
    if proj.exists():
        for task_dir in proj.glob("*/*"):
            if task_dir.is_dir():
                yield task_dir / "step_failure.json"
    # 平铺布局：<root>/<task_id>/step_failure.json
    for task_dir in base.iterdir():
        if task_dir.is_dir():
            p = task_dir / "step_failure.json"
            if p.exists():
                yield p


def aggregate_patterns(root: Path | None = None) -> list[dict]:
    """跨任务聚合失败模式。返回按出现次数降序的模式列表：
    [{capability, error_type, count, suggestions, tried_alternatives,
      success_rate, last_seen, sample_tasks}]"""
    key_counter: Counter = Counter()
    suggestions: dict[tuple, Counter] = defaultdict(Counter)
    tried: dict[tuple, Counter] = defaultdict(Counter)
    outcomes: dict[tuple, list] = defaultdict(list)
    last_seen: dict[tuple, str] = {}
    sample_tasks: dict[tuple, list] = defaultdict(list)

    for f in _iter_task_failure_files(root):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict):
                continue
            cap = str(it.get("capability") or "unknown")[:40]
            etype = str(it.get("error_type") or "unknown")[:60]
            key = (cap, etype)
            key_counter[key] += 1
            sug = str(it.get("suggestion") or "").strip()
            if sug:
                suggestions[key][sug] += 1
            for alt in (it.get("tried_alternatives") or [])[:5]:
                a = str(alt or "").strip()
                if a:
                    tried[key][a] += 1
            oc = str(it.get("replacement_outcome") or "").strip()
            if oc:
                outcomes[key].append(oc)
            ts = str(it.get("timestamp") or "")
            if ts and ts > last_seen.get(key, ""):
                last_seen[key] = ts
            tid = str(it.get("step_id") or "").split("-")[0]
            if tid and len(sample_tasks[key]) < 3 and tid not in sample_tasks[key]:
                sample_tasks[key].append(tid)

    patterns: list[dict] = []
    for (cap, etype), cnt in key_counter.most_common(_MAX_PATTERNS):
        sug_counts = suggestions[(cap, etype)]
        top_sug = sug_counts.most_common(1)[0][0] if sug_counts else ""
        alt_counts = tried[(cap, etype)]
        outcomes_list = outcomes[(cap, etype)]
        ok = sum(1 for o in outcomes_list
                 if any(m in o.lower() for m in _SUCCESS_MARKERS))
        patterns.append({
            "capability": cap,
            "error_type": etype,
            "count": cnt,
            "top_suggestion": top_sug,
            "suggestions": [s for s, _ in sug_counts.most_common(3)],
            "tried_alternatives": [a for a, _ in alt_counts.most_common(5)],
            "success_rate": round(ok / len(outcomes_list), 2) if outcomes_list else None,
            "last_seen": last_seen.get((cap, etype), ""),
            "sample_tasks": sample_tasks.get((cap, etype), []),
        })
    return patterns


def save_pattern_library(patterns: list[dict] | None = None) -> Path:
    """持久化模式库；无参数时重新聚合。返回写入路径。"""
    if patterns is None:
        patterns = aggregate_patterns()
    p = _pattern_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "patterns": patterns,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        logger.warning("error_patterns 持久化失败: %s", str(exc)[:100])
    return p


def load_pattern_library() -> list[dict]:
    """读取已持久化的模式库；不存在或损坏返回空列表。"""
    p = _pattern_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return list(data.get("patterns") or [])
    except Exception:
        return []


def suggest_fix(capability: str, error_type: str) -> dict | None:
    """按 (capability, error_type) 精确查找修复模板；无则回退到同 error_type 最高频模式。"""
    cap = str(capability or "")
    etype = str(error_type or "")
    patterns = load_pattern_library()
    if not patterns:  # 库为空时即时聚合一次
        patterns = aggregate_patterns()
        save_pattern_library(patterns)
    for p in patterns:
        if p["capability"] == cap and p["error_type"] == etype:
            return p
    for p in patterns:  # 回退1：同类错误的高频模板
        if p["error_type"] == etype:
            return p
    for p in patterns:  # 回退2：同 capability 的最高频模板
        if p["capability"] == cap:
            return p
    return None


def build_reflection_context(limit: int = 5) -> str:
    """生成反思 prompt 可注入的"已知失败模式"文本（前 limit 个高频模式）。"""
    patterns = load_pattern_library()
    if not patterns:
        patterns = aggregate_patterns()
    if not patterns:
        return ""
    lines = []
    for p in patterns[:limit]:
        rate = f"{p['success_rate'] * 100:.0f}%" if p["success_rate"] is not None else "未知"
        lines.append(
            f"- [{p['capability']}/{p['error_type']}] 出现 {p['count']} 次，"
            f"修复成功率 {rate}；建议：{p['top_suggestion'] or '（无）'}"
        )
    return "已知失败模式库（历史同类错误的修复模板；优先采纳成功率高者）：\n" + "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--scan":
        ps = aggregate_patterns()
        print(json.dumps(ps, ensure_ascii=False, indent=1))
        print(f"\n共 {len(ps)} 个模式")
    else:
        save_pattern_library()
        print(f"模式库已持久化: {_pattern_path()}")
