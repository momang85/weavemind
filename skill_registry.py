# -*- coding: utf-8 -*-
"""Skill 注册表（对标标准 C3-3.5 渐进式披露）。

- 只向 Agent 注入 description（几十 token），完整流程按需读取 SKILL.md；
- match_skills(goal) 按触发关键词命中，供 planner/步骤注入。
"""

import re
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    meta: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            head = text[3:end]
            for line in head.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = text[end + 3:].strip()
    return meta, body


def list_skills() -> list[dict]:
    out = []
    if not SKILLS_DIR.exists():
        return out
    for d in sorted(SKILLS_DIR.iterdir()):
        sk = d / "SKILL.md"
        if not sk.exists():
            continue
        meta, body = _parse_frontmatter(sk.read_text(encoding="utf-8"))
        out.append({
            "name": meta.get("name") or d.name,
            "description": meta.get("description") or body[:120],
            "owner": meta.get("owner", ""),
            "version": meta.get("version", "1"),
            "applies": [
                c.strip() for c in meta.get("applies", "").split(",") if c.strip()
            ],
            "path": str(sk),
            "keywords": _keywords(body),
        })
    return out


def _keywords(body: str) -> list[str]:
    # 从触发条件段落提取关键词
    m = re.search(r"## 触发条件\n(.+?)(?=\n## |\Z)", body, re.S)
    text = m.group(1) if m else ""
    return [
        k.strip() for k in re.split(r"[，,、。；;()（）\s]+", text)
        if len(k.strip()) >= 2
    ][:20]


def load_skill(name: str) -> dict | None:
    for s in list_skills():
        if s["name"] == name:
            return s
    return None


def get_skill_prompt(name: str) -> str:
    s = load_skill(name)
    if not s:
        return ""
    return Path(s["path"]).read_text(encoding="utf-8")


def match_skills(goal: str, capability: str = "") -> list[dict]:
    """按目标与能力命中 skill；返回按匹配度排序的列表。"""
    g = str(goal or "")
    cap_boost = {
        "report_generator": "research-report",
        "content_summary": "research-report",
        "code_execution": "game-delivery",
        "data_loader": "data-pipeline",
        "data_analyzer": "data-pipeline",
        "model_trainer": "data-pipeline",
    }
    scored = []
    for s in list_skills():
        score = 0
        for k in s["keywords"]:
            if k in g:
                score += 1
        if cap_boost.get(capability) == s["name"]:
            score += 1
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored]


def _section(body: str, title: str) -> str:
    m = re.search(rf"^## {title}\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    return m.group(1).strip() if m else ""


def get_skill_standards(name: str) -> dict:
    """渐进式披露：只取 description + 质量标准 + 反模式（可执行的标准部分）。"""
    s = load_skill(name)
    if not s:
        return {}
    meta, body = _parse_frontmatter(Path(s["path"]).read_text(encoding="utf-8"))
    return {
        "name": name,
        "description": meta.get("description", ""),
        "standards": _section(body, "质量标准"),
        "antipatterns": _section(body, "反模式"),
    }


def skill_applies(name: str, capability: str) -> bool:
    s = load_skill(name)
    if not s or not capability:
        return bool(s)
    applies = s.get("applies") or []
    return not applies or capability in applies
