# -*- coding: utf-8 -*-
"""轻量审计日志：JSON Lines 追加写，线程安全，服务不可用也不影响主流程。

记录字段：
  timestamp  ISO8601 UTC 时间
  user       操作者用户名（登录失败时为尝试的用户名）
  ip         来源 IP
  action     操作类型，如 login.success / login.failed / logout / task.submit
  target     操作对象（任务 id、用户名、分享 token 等）
  result     ok / fail / denied
  detail     可选的补充说明（目标摘要、失败原因等，已限长）
"""
import json
import os
import threading
from datetime import datetime, timezone

AUDIT_FILE = os.environ.get(
    "AUDIT_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "audit.jsonl"),
)
_lock = threading.Lock()


def audit_log(
    user: str,
    ip: str,
    action: str,
    target: str = "",
    result: str = "ok",
    detail: str | None = None,
) -> dict:
    """追加一条审计记录；任何 IO 异常只丢弃日志，不影响业务请求。"""
    record: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user": str(user or ""),
        "ip": str(ip or ""),
        "action": str(action or ""),
        "target": str(target or "")[:300],
        "result": str(result or ""),
    }
    if detail is not None:
        record["detail"] = str(detail)[:500]
    try:
        with _lock:
            os.makedirs(os.path.dirname(os.path.abspath(AUDIT_FILE)), exist_ok=True)
            with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return record


def read_audit(limit: int = 200) -> list[dict]:
    """返回最近 limit 条审计记录（默认 200，上限 1000）。"""
    try:
        limit = max(1, min(int(limit or 200), 1000))
    except (TypeError, ValueError):
        limit = 200
    rows: list[dict] = []
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return rows[-limit:]
