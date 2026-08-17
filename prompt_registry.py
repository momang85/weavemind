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
_TRIGGER_GOAL_CACHE: dict[str, str] = {}
_TRIGGER_GOAL_TTL = 600.0
_TRIGGER_GOAL_TS: dict[str, float] = {}

# 目标匹配时不算"特征词"的通用词（财报/最新/总结等不能证明两个任务同类）
_GOAL_STOP = {
    "搜索", "总结", "分析", "报告", "最新", "当前", "现状", "发展", "历程",
    "集团", "财报", "历年", "年度", "数据", "信息", "内容", "情况", "相关",
    "进行", "需要", "完成", "输出", "生成", "请", "并", "与之相配", "配合",
    "the", "and", "with", "for", "report", "analysis", "search", "summary",
}


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


def get_prompt(key: str, default: str, goal: str = "") -> str:
    """取提示词：注册表有覆盖则【追加】在默认提示词之后（保留原契约，
    只叠加自迭代改进），否则返回默认版。
    goal 用于目标匹配：自迭代覆盖只对同类目标生效（如特斯拉的"汽车交付量"
    字段要求不会泄漏到腾讯任务）。"""
    ov = load_overrides().get(key)
    if isinstance(ov, dict) and str(ov.get("prompt") or "").strip():
        if not _override_applies(ov, goal):
            return default
        return str(default) + "\n\n【自迭代改进】" + str(ov["prompt"]).strip()
    return default


def extract_goal(text: str) -> str:
    """从指令/上下文中提取用户目标（供 override 目标匹配；无则返回原文前 200 字）。"""
    t = str(text or "")
    m = re.search(r"(?:用户目标|任务目标)[：:]\s*([^\n]+)", t)
    if m:
        return m.group(1).strip()[:200]
    return t.strip()[:200]


def _override_applies(ov: dict, goal: str) -> bool:
    """覆盖是否适用于当前目标：
    1) 显式 match_goal 关键词 → 目标命中才应用；
    2) 自迭代覆盖（带 trigger_task）→ 与触发任务目标有特征词交集才应用；
    3) 手动覆盖（无 trigger_task/match_goal）→ 全局应用（兼容旧行为）。"""
    g = str(goal or "").strip().lower()
    if not g:
        return True  # 无目标上下文 → 保持旧行为（全局应用，兼容测试/旧调用）
    mg = ov.get("match_goal")
    if isinstance(mg, list) and mg:
        return any(str(k).strip().lower() in g for k in mg if str(k).strip())
    tt = str(ov.get("trigger_task") or "").strip()
    if not tt:
        return True
    tgoal = _trigger_goal(tt)
    if not tgoal:
        return True  # 触发任务查不到（历史清理）→ 按旧行为应用，避免误杀有效覆盖
    return _tokens_overlap(g, tgoal.lower())


def _trigger_goal(task_id: str) -> str:
    """从 agents.db 读取触发任务的目标（带进程内缓存）。"""
    now = __import__("time").time()
    cached = _TRIGGER_GOAL_CACHE.get(task_id)
    if cached is not None and now - _TRIGGER_GOAL_TS.get(task_id, 0) < _TRIGGER_GOAL_TTL:
        return cached
    goal = ""
    try:
        import sqlite3
        db_path = os.environ.get("REGISTRY_DB") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "agents.db"
        )
        con = sqlite3.connect(db_path, timeout=3)
        try:
            row = con.execute(
                "SELECT goal FROM task_history WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row:
                goal = str(row[0] or "")
        finally:
            con.close()
    except Exception:
        pass
    _TRIGGER_GOAL_CACHE[task_id] = goal
    _TRIGGER_GOAL_TS[task_id] = now
    return goal


def _tokens_overlap(g1: str, g2: str) -> bool:
    """两个目标是否有≥1 个共同特征词（2 字中文/英文词，排除通用词）。"""
    def toks(t: str) -> set[str]:
        out: set[str] = set()
        for run in re.findall(r"[\u4e00-\u9fff]{2,6}", t):
            for i in range(len(run) - 1):
                bg = run[i:i + 2]
                if bg not in _GOAL_STOP:
                    out.add(bg)
        for w in re.findall(r"[a-z][a-z0-9-]{2,}", t):
            if w not in _GOAL_STOP:
                out.add(w)
        return out

    a, b = toks(g1), toks(g2)
    return bool(a & b)


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
