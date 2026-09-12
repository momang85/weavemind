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

# 能力泛词：出现这些词只说明"任务类型相同"，不能证明"主题相同"。
# 目标匹配的交集必须至少含一个既不是泛词、也不是通用领域词的词。
_ABILITY_STOP = {
    "代码", "脚本", "python", "文件", "程序", "实现", "运行", "步骤", "结果",
    "图表", "绘图", "统计", "汇总", "合计", "均值", "最大值", "最小值",
    "搜索", "查询", "抓取", "提取", "整理", "输出", "生成", "要求", "标准",
    "格式", "来源", "标注", "数值", "指标", "单文件", "可直接运行", "自包含",
}

# 通用领域词：跨任务高频复现，单独命中不足以判定"同类主题"
_GENERIC_TOPIC = {
    "财务", "营收", "净利", "毛利", "现金", "利润", "股票", "市场", "行业",
    "公司", "企业", "季度", "报表", "趋势", "价格", "成交量", "成交额",
    "占比", "比例", "分布", "研报", "报告", "建议", "风险", "投资",
}

# 单键下最多保留的作用域条目数（每个作用域一条，避免无限增长）
MAX_ENTRIES_PER_KEY = int(os.environ.get("PROMPT_MAX_SCOPED_ENTRIES", "5"))

# 含这些内容说明覆盖是"针对某一次任务"的，必须能派生出作用域才允许写入
_TASK_SPECIFIC_HINTS = (
    "akshare", "tushare", "双y轴", "双 y 轴", "IPYNB", "ipynb",
    "2014", "2015", "2016", "比亚迪", "特斯拉",
)


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
    """取提示词：注册表有适用覆盖则【追加】在默认提示词之后（保留原契约，
    只叠加自迭代改进），否则返回默认版。
    作用域解析统一走 resolve_override：自迭代覆盖只对同类目标生效
    （如特斯拉的"汽车交付量"字段要求不会泄漏到腾讯任务）。"""
    ov = resolve_override(key, goal)
    if ov and str(ov.get("prompt") or "").strip():
        return str(default) + "\n\n【自迭代改进】" + str(ov["prompt"]).strip()
    return default


def _tokens(text: str) -> set[str]:
    """目标文本 → 特征词集合（2 字中文滑窗 + ≥3 字符英文词，剔除通用词）。"""
    out: set[str] = set()
    t = str(text or "").lower()
    for run in re.findall(r"[\u4e00-\u9fff]{2,6}", t):
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in _GOAL_STOP:
                out.add(bg)
    for w in re.findall(r"[a-z][a-z0-9-]{2,}", t):
        if w not in _GOAL_STOP:
            out.add(w)
    return out


def topic_keywords(text: str, limit: int = 8) -> list[str]:
    """提取可作为覆盖作用域的主题词（长词优先、可读）。

    - 中文取 2–6 字连续片段、英文取 ≥4 字符标识符；
    - 剔除通用词/能力泛词，并丢弃"完全由泛词组成"的片段（如"财务数据"）；
    - 长词覆盖短词（"贵州茅台"存在时不再保留"茅台"）。

    匹配是对称的：读写两侧都用 _tokens 分词，所以这里存长词不影响命中。
    """
    t = str(text or "").lower()
    generic = _ABILITY_STOP | _GENERIC_TOPIC | _GOAL_STOP
    candidates = re.findall(r"[\u4e00-\u9fff]{2,6}", t)
    candidates += re.findall(r"[a-z][a-z0-9-]{3,}", t)
    keep: list[str] = []
    for cand in candidates:
        if cand in generic:
            continue
        if len(cand) > 2 and all(
            cand[i:i + 2] in generic for i in range(len(cand) - 1)
        ):
            continue
        keep.append(cand)
    keep.sort(key=lambda x: (-len(x), x))
    out: list[str] = []
    for cand in keep:
        if any(cand in kept for kept in out):
            continue
        out.append(cand)
        if len(out) >= limit:
            break
    return out


def _topic_overlap(g1: str, g2: str) -> bool:
    """两个目标是否共享"主题词"（而非仅共享能力泛词/通用领域词）。"""
    inter = (_tokens(g1) & _tokens(g2)) - _ABILITY_STOP - _GENERIC_TOPIC
    return bool(inter)


def _normalize_entries(raw) -> list[dict]:
    """把"键 → 单条(旧格式) | 条目列表(新格式)"统一成条目列表。"""
    if isinstance(raw, dict):
        return [dict(raw)]
    if isinstance(raw, list):
        return [dict(e) for e in raw if isinstance(e, dict)]
    return []


def _entry_signature(entry: dict) -> tuple:
    """作用域签名：同签名视为同一条目（更新版本而非新增）。"""
    mg = entry.get("match_goal")
    if isinstance(mg, str):
        mg = [mg]
    if isinstance(mg, list) and mg:
        return tuple(sorted(str(x) for x in mg))
    if str(entry.get("scope") or "") == "global":
        return ("__global__",)
    tt = str(entry.get("trigger_task") or "").strip()
    return (tt or "__global__",)


def _entry_applies(entry: dict, goal: str) -> bool:
    """条目是否适用于该目标。带触发任务却无法解析其目标时 **不应用**
    （fail-closed：此前这里 fail-open，导致历史清理后旧覆盖全局泄漏）。"""
    if not isinstance(entry, dict):
        return False
    mg = entry.get("match_goal")
    if isinstance(mg, str):
        mg = [mg]
    if isinstance(mg, list) and mg:
        return _topic_overlap(str(goal or ""), " ".join(str(x) for x in mg))
    if str(entry.get("scope") or "") == "global":
        return True
    tt = str(entry.get("trigger_task") or "").strip()
    if tt:
        trigger_goal = _trigger_goal(tt)
        if not trigger_goal:
            return False
        return _topic_overlap(str(goal or ""), trigger_goal)
    # 既无作用域也无触发任务 → 手写维护的覆盖，视为全局（兼容旧行为）
    return True


def resolve_override(key: str, goal: str = "") -> dict | None:
    """解析某能力键当前适用的覆盖条目（**唯一读取入口**）。

    - 支持旧格式（单条 dict）与新格式（条目列表，按作用域并存）；
    - 高版本优先；无适用条目返回 None（调用方回落到源码默认值）。
    """
    entries = _normalize_entries(load_overrides().get(key))
    if not entries:
        return None
    for entry in sorted(
        entries, key=lambda e: int(e.get("version") or 0), reverse=True
    ):
        if _entry_applies(entry, goal):
            return entry
    return None


def extract_goal(text: str) -> str:
    """从指令/上下文中提取用户目标（供 override 目标匹配；无则返回原文前 200 字）。"""
    t = str(text or "")
    m = re.search(r"(?:用户目标|任务目标)[：:]\s*([^\n]+)", t)
    if m:
        return m.group(1).strip()[:200]
    return t.strip()[:200]


def _override_applies(ov: dict, goal: str) -> bool:
    """兼容旧调用：单条覆盖是否适用（内部统一走 _entry_applies）。"""
    return _entry_applies(ov, goal)


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
    """兼容旧调用：两个目标是否有共同主题词（内部走 _topic_overlap）。"""
    return _topic_overlap(g1, g2)


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
    trigger_task: str = "", version_base: int = 1, goal: str = "",
) -> tuple[bool, list[str]]:
    """写入一条覆盖（同名 key 下**按作用域并存**，版本 +1）。返回 (是否成功, 问题列表)。

    写入时必须能确定作用域：自迭代产出的是"某一次任务的教训"，若它含任务特定内容
    却派生不出主题词，就拒绝写入——此前这类条目会以裸能力键全局生效，把单个任务的
    要求（如"用 matplotlib 画 2014-2025 营收图"）追加到所有同类步骤。
    """
    issues = _validate_fix(key, prompt, rationale)
    if issues:
        return False, issues
    scope_goal = goal or (_trigger_goal(trigger_task) if trigger_task else "")
    match_goal = topic_keywords(scope_goal) if scope_goal else []
    low = str(prompt or "").lower()
    if not match_goal and any(h.lower() in low for h in _TASK_SPECIFIC_HINTS):
        return False, ["含任务特定内容但无法确定作用域（缺目标主题词）"]
    scope = "scoped" if (match_goal or trigger_task) else "global"
    with _LOCK:
        p = _overrides_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = load_overrides()
        entries = _normalize_entries(data.get(key))
        new_entry = {
            "prompt": prompt,
            "version": version_base + 1,
            "rationale": str(rationale)[:500],
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "trigger_task": str(trigger_task)[:40],
            "match_goal": match_goal,
            "scope": scope,
        }
        signature = _entry_signature(new_entry)
        merged = False
        for entry in entries:
            if _entry_signature(entry) == signature:
                try:
                    base_ver = int(entry.get("version") or 0)
                except (TypeError, ValueError):
                    base_ver = version_base
                new_entry["version"] = base_ver + 1
                entry.update(new_entry)
                merged = True
                break
        if not merged:
            entries.append(new_entry)
        entries.sort(key=lambda e: int(e.get("version") or 0), reverse=True)
        data[key] = entries[:MAX_ENTRIES_PER_KEY]
        try:
            p.write_text(
                json.dumps(data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            return True, []
        except Exception as exc:
            return False, [f"写入失败: {exc}"]


def summary() -> dict:
    """供前端/日志展示的覆盖摘要（兼容新旧格式）。"""
    data = load_overrides()
    items: list[dict] = []
    for key, raw in data.items():
        for entry in _normalize_entries(raw):
            items.append({
                "key": key,
                "version": entry.get("version"),
                "applied_at": entry.get("applied_at"),
                "trigger_task": entry.get("trigger_task"),
                "scope": entry.get("scope") or ("scoped" if entry.get("trigger_task") else "global"),
                "match_goal": entry.get("match_goal") or [],
            })
    return {"keys": list(data.keys()), "count": len(items), "items": items}
