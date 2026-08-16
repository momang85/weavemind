# -*- coding: utf-8 -*-
"""知识库访问控制（对标标准 C4-4.4 与 course_core/kb_filter.py）。

权限模型：用户(user) → 职位(position) → 可访问知识文档(kb) 多对多。
数据用 CSV 模拟（生产可换数据库），见 kb_access_control_db/。
任务提交时可带 user_id/role；编排器据此过滤注入上下文，默认（无身份）不限制。
"""

import ast
import csv
import os
from pathlib import Path


class KbAccessControl:
    def __init__(self, db_dir: str | None = None):
        base = Path(db_dir) if db_dir else (
            Path(__file__).resolve().parent / "kb_access_control_db"
        )
        self._user_pos: dict[str, str] = {}
        self._pos_kb: dict[str, set[str]] = {}
        self._kb_ids: set[str] = set()
        self._load(base)

    def _load(self, base: Path) -> None:
        def _rows(name: str) -> list[dict]:
            p = base / name
            if not p.exists():
                return []
            with open(p, encoding="utf-8") as f:
                return list(csv.DictReader(f))

        for row in _rows("user.csv"):
            uid, pid = str(row.get("user_id") or "").strip(), str(row.get("position_id") or "").strip()
            if uid:
                self._user_pos[uid] = pid
        for row in _rows("position.csv"):
            pass  # 职位名仅用于展示
        for row in _rows("kb_position_ref.csv"):
            kb = str(row.get("kb_id") or "").strip()
            pids = str(row.get("position_ids") or "[]")
            try:
                ids = {str(x).strip() for x in ast.literal_eval(pids)}
            except Exception:
                ids = set()
            if kb:
                self._kb_ids.add(kb)
                for p in ids:
                    self._pos_kb.setdefault(p, set()).add(kb)

    def position_of(self, user_id: str) -> str:
        return self._user_pos.get(str(user_id), "")

    def allowed_kb_ids(self, user_id: str) -> set[str]:
        pid = self.position_of(user_id)
        if not pid:
            return set()
        return self._pos_kb.get(pid, set())

    def is_allowed(self, user_id: str, kb_id: str) -> bool:
        kb = str(kb_id)
        if not kb or kb not in self._kb_ids:
            return True  # 非受控文档默认放行
        return kb in self.allowed_kb_ids(user_id)

    def filter_contents(self, user_id: str, items: list[str]) -> list[str]:
        """按用户职位过滤上下文片段。items 视为 kb_id→内容 的文本列表，
        仅过滤可归属到受控 kb_id 的条目；无法归属的条目放行（保守）。"""
        if not user_id:
            return items
        allowed = self.allowed_kb_ids(user_id)
        out = []
        for it in items:
            kb = self._find_kb_id(it)
            if kb is None or kb in allowed:
                out.append(it)
        return out

    def _find_kb_id(self, text: str) -> str | None:
        """从片段中探测 kb_id（格式：'[kb:<id>]' 或 'kb_id=<id>'）。"""
        import re
        m = re.search(r"\[kb:([A-Za-z0-9_\-]+)\]", str(text))
        if m:
            return m.group(1)
        m = re.search(r"kb_id=([A-Za-z0-9_\-]+)", str(text))
        return m.group(1) if m else None
