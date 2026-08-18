# -*- coding: utf-8 -*-
"""结构化验收器（acceptance checker）：确定性 checklist，输出缺口报告。

验收器不直接提高报告质量，但它让"质量差在哪里"变成可读的结构化报告：
- 每条检查是确定性函数（无 LLM），输出 {pass, details, 计数...}
- run_acceptance 汇总为缺口报告 {report_id, checks, overall, gaps}
- 缺口报告写入任务工作区 acceptance_report.json，并注入反思上下文，
  让反思从"重做一遍"变成"精准补缺口"。

第一条检查：数字溯源校验（报告里的数字能否在检索结果/快照/清洗数据中找到）。
后续可扩展：章节完整性、主体归属、来源标注诚实性、图表存在性。
"""

import json
import re
from pathlib import Path


# ─────────────────────────────────────────────
# 数字提取
# ─────────────────────────────────────────────

_NUM_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(\d[\d,]*(?:\.\d+)?)\s*"
    r"(万亿|千亿|百亿|亿|万)?\s*"
    r"(美元|港元|元|人民币|%|％)?"
)


def _norm(s: str) -> str:
    """去掉逗号/全角逗号/空白，保留小数点，便于模糊匹配。"""
    s = re.sub(r"[,\uFF0C\s]+", "", str(s or ""))
    return re.sub(r"\.0+(?=\D|$)", "", s)  # 1152.0亿 → 1152亿，避免小数写法差异


def extract_financial_numbers(text: str) -> list[dict]:
    """从正文提取需要溯源的财务数字：
    - 带单位（亿/万/美元/元/%）的数字
    - 无单位但 ≥4 位有效数字的大数（如 1383）
    排除：纯年份（19xx/20xx）、URL 内的数字、无单位的个位数。"""
    t = str(text or "")
    # 屏蔽图片引用/文件路径/任务 ID（报告内嵌图表路径含 ui-xxxx，会被误当数字）
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)          # ![name](path)
    t = re.sub(r"[A-Za-z]:\\[^\s]*", " ", t)             # C:\...\路径
    t = re.sub(r"/tasks/ui-[a-z0-9]+/[^\s]*", " ", t)    # /tasks/ui-xxx/...
    url_spans = [m.span() for m in re.finditer(r"https?://\S+", t)]

    def in_url(pos: int) -> bool:
        return any(s <= pos < e for s, e in url_spans)

    nums: list[dict] = []
    for m in _NUM_UNIT_RE.finditer(t):
        pos = m.start()
        if in_url(pos):
            continue
        value_raw = m.group(1)
        unit_big = m.group(2) or ""
        unit_small = m.group(3) or ""
        digits = value_raw.replace(",", "")
        if not unit_big and not unit_small:
            if re.fullmatch(r"(19|20)\d{2}", digits):
                continue  # 纯年份
            if len(digits.replace(".", "")) < 4:
                continue  # 无单位的个位数/短数，太泛
        unit = unit_big + unit_small  # 如 "亿元" "万" "%"
        nums.append({
            "value": digits,
            "unit": unit,
            "raw": m.group(0).strip(),
            "pos": pos,
        })
    return nums


def _candidates(num: dict) -> list[str]:
    """生成匹配候选：数字+单位、数字+短单位、大数裸数字、百分比。"""
    v = num["value"]
    unit = num["unit"]
    cands: list[str] = []
    if unit:
        cands.append(_norm(v + unit))
        short = re.sub(r"^(万亿|千亿|百亿|亿|万).*$", lambda m: m.group(1), unit)
        if short != unit:
            cands.append(_norm(v + short))
        if unit.endswith("%"):
            cands.append(_norm(v + "%"))
            cands.append(_norm(v + "％"))
    # 裸数字（无单位）：用词边界匹配，避免 "789" 命中 "4789"/"2019" 的子串
    return [c for c in dict.fromkeys(cands) if c]


def _bare_match(value: str, text: str) -> bool:
    """无单位大数按词边界匹配（前后不是数字/字母/点）。"""
    return re.search(
        r"(?<![A-Za-z0-9.])\s*" + re.escape(value) + r"\s*(?![A-Za-z0-9.])",
        text,
    ) is not None


def _collect_sources(workspace) -> dict[str, str]:
    """收集可溯源数据源文本：search_results / fetch_snapshot / clean_chart_data。"""
    ws = Path(workspace)
    proj = ws / "project"
    src: dict[str, str] = {}
    try:
        sr = proj / "search_results.json"
        if sr.exists():
            items = json.loads(sr.read_text(encoding="utf-8"))
            parts = []
            for it in items or []:
                if isinstance(it, dict):
                    parts.append(f"{it.get('title') or ''} {it.get('snippet') or ''}")
            src["search_results"] = "\n".join(parts)
    except Exception:
        pass
    try:
        fs = proj / "fetch_snapshot.json"
        if fs.exists():
            snaps = json.loads(fs.read_text(encoding="utf-8"))
            src["fetch_snapshot"] = "\n".join(
                f"{s.get('title') or ''} {s.get('text') or ''}" for s in snaps or []
            )
    except Exception:
        pass
    try:
        cd = proj / "clean_chart_data.json"
        if cd.exists():
            src["clean_chart_data"] = cd.read_text(encoding="utf-8")
    except Exception:
        pass
    return src


def _traceable_in_clean(num: dict, clean_text: str) -> bool:
    """在 clean_chart_data.json 中按数值+单位精确比对。"""
    try:
        data = json.loads(clean_text)
    except Exception:
        return False
    try:
        v = float(num["value"])
    except (TypeError, ValueError):
        v = None
    if v is None:
        return False
    unit = num["unit"]
    for key in ("market_data", "market_share", "macro_indicators", "market_trends"):
        for r in data.get(key) or []:
            if not isinstance(r, dict):
                continue
            try:
                rv = float(r.get("value"))
            except (TypeError, ValueError):
                continue
            ru = str(r.get("unit") or "")
            if abs(rv - v) < max(0.5, abs(v) * 0.005) and (
                not unit or not ru or unit == ru or unit in ru or ru in unit
            ):
                return True
    return False


def check_number_traceability(report: str, sources: dict, threshold: float = 0.7) -> dict:
    """数字溯源校验：报告中的财务数字能否在检索/快照/清洗数据中找到。
    返回 {pass, details, total_count, traceable_count, unverifiable_count, ...}。"""
    nums = extract_financial_numbers(report)
    total = len(nums)
    if total == 0:
        return {
            "pass": True,
            "details": "报告未检出需要溯源的财务数字",
            "total_count": 0, "traceable_count": 0, "unverifiable_count": 0,
            "untraceable": [],
        }
    src_norm = {k: _norm(v) for k, v in sources.items() if v}
    clean_text = sources.get("clean_chart_data") or ""
    traceable: list[dict] = []
    untraceable: list[dict] = []
    for n in nums:
        hit = None
        if clean_text and _traceable_in_clean(n, clean_text):
            hit = "clean_chart_data"
        else:
            if n["unit"]:
                for k, st in src_norm.items():
                    if any(c and c in st for c in _candidates(n)):
                        hit = k
                        break
            else:
                for k, st in src_norm.items():
                    if _bare_match(n["value"], st):
                        hit = k
                        break
        item = {"raw": n["raw"], "value": n["value"], "unit": n["unit"]}
        if hit:
            item["source"] = hit
            traceable.append(item)
        else:
            untraceable.append(item)
    rate = traceable.__len__() / total
    passed = rate >= threshold or total < 3
    details = (
        f"数字溯源率 {rate:.0%}（{len(traceable)}/{total}）"
        + ("" if passed else f"，低于阈值 {threshold:.0%}")
        + (f"；不可溯源示例：{'、'.join(u['raw'][:20] for u in untraceable[:5])}" if untraceable else "")
    )
    return {
        "pass": passed,
        "details": details,
        "total_count": total,
        "traceable_count": len(traceable),
        "unverifiable_count": len(untraceable),
        "untraceable": untraceable[:10],
    }


# ─────────────────────────────────────────────
# Checklist runner
# ─────────────────────────────────────────────

def run_acceptance(task_id: str, goal: str, report_text: str, workspace) -> dict:
    """运行验收 checklist，输出缺口报告。"""
    sources = _collect_sources(workspace)
    checks: dict = {}
    checks["number_traceability"] = check_number_traceability(report_text, sources)
    gaps = [c["details"] for c in checks.values() if not c["pass"]]
    overall = "pass" if not gaps else "fail"
    return {
        "report_id": str(task_id),
        "goal": str(goal or "")[:120],
        "checks": checks,
        "overall": overall,
        "gaps": gaps,
    }
