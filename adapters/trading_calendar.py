# -*- coding: utf-8 -*-
"""`trading_calendar`（读侧）：**独立交易日历**——把"停牌"与"休市"分开。

为什么必须有它：缺日历的时候，"某标的当天没有行情行"有两种完全不同的解释 ——
**休市**（全市场都没交易）或**停牌**（市场开着，这只票不能交易）。两者对研究结论的影响不同：
休市只是没有那天，停牌会让 `t+5` 这类"第 N 个交易日"错位，还可能让再平衡静默跳过。
没有日历就只能拿基准序列当代理报"疑似"（见 `event_returns.suspension_suspect`）——
那是**降级方案**，不是结论。

纪律：

- **只在覆盖区间内下结论**：日期落在日历覆盖范围之外时返回 `unknown`，
  **不把"没查到"当成"休市"**（否则会把"日历太旧"误判成"那天不开市"）。
- 日历**不含停牌信息**：停牌是标的级事实，由"该交易日无行情行"推出，本模块只负责回答
  "那天市场开不开"。
- 许可随数据走：免费源日历标"仅内部试验"，不得进对外下载包。
- 缺数据不猜：没有数据集就 `unavailable` + 原因，不返回空日历冒充"全都不开市"。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "weavemind.trading_calendar/0"
CAPABILITY = "trading_calendar"
REQUIRED_COLUMNS = ("date",)
LICENSE_FREE_TRIAL = "免费源·仅内部试验"
LICENSE_PERSONAL = "个人非商业"
LICENSE_INSTITUTIONAL = "机构授权"


def data_dir() -> Path:
    env = str(os.environ.get("WEAVEMIND_CALENDAR_DIR") or "").strip()
    if env:
        return Path(env)
    try:
        import workspace as _ws
        return Path(_ws.WORKSPACE_ROOT).parent / "trading_calendar"
    except Exception:                                        # noqa: BLE001
        return Path.cwd() / "trading_calendar"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm_date(v) -> str:
    s = str(v or "").strip()
    for sep in ("-", "/", "."):
        if sep in s:
            parts = [p for p in s.split(sep) if p]
            if len(parts) >= 3:
                try:
                    return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                except ValueError:
                    return ""
    return s[:10] if len(s) >= 8 and s[:4].isdigit() else ""


def _read_rows(path: Path) -> tuple[list[str], str]:
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:                             # noqa: BLE001
            return [], f"JSON 解析失败：{str(exc)[:80]}"
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        out = []
        for r in rows or []:
            d = _norm_date(r.get("date") if isinstance(r, dict) else r)
            if d:
                out.append(d)
        return out, ""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            if not rd.fieldnames or "date" not in [c.strip().lower() for c in rd.fieldnames]:
                return [], "CSV 缺少 `date` 列"
            return [d for d in (_norm_date(r.get("date")) for r in rd) if d], ""
    except Exception as exc:                                 # noqa: BLE001
        return [], f"CSV 解析失败：{str(exc)[:80]}"


def import_file(path, *, source: str = "", license: str = LICENSE_FREE_TRIAL,
                market: str = "CN", extra: dict | None = None) -> dict:
    """原始文件 → 日历数据集（`<dir>/<dataset_id>/{sessions.csv,meta.json}`）。"""
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "reason": f"文件不存在：{p}"}
    blob = p.read_bytes()
    dataset_id = hashlib.sha256(blob).hexdigest()
    dates, err = _read_rows(p)
    if err:
        return {"ok": False, "reason": err, "dataset_id": dataset_id}
    dates = sorted(set(dates))
    if not dates:
        return {"ok": False, "reason": "日历里没有有效日期行", "dataset_id": dataset_id}
    # 相邻日期重复即视为脏数据：宁可拒绝，也不产一份"有重复交易日"的日历
    if len(dates) != len(set(dates)):
        return {"ok": False, "reason": "日历存在重复交易日", "dataset_id": dataset_id}
    d = data_dir() / dataset_id
    d.mkdir(parents=True, exist_ok=True)
    with (d / "sessions.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "session"])
        for x in dates:
            w.writerow([x, 1])
    meta = {"schema": SCHEMA, "dataset_id": dataset_id, "source": str(source or ""),
            "license": str(license or "unknown"), "market": str(market or "CN"),
            "imported_at": _now(), "raw_file": p.name, "raw_bytes": len(blob),
            "raw_sha256": dataset_id, "rows": len(dates),
            "range": [dates[0], dates[-1]], "unavailable": []}
    if extra:
        meta.update(dict(extra))
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    return {"ok": True, "dataset_id": dataset_id, "dir": str(d), "meta": meta}


def load(dataset_id: str = "") -> dict:
    """读最近/指定日历 → 载荷；缺数据返回 `unavailable`（**不返回空日历**）。"""
    base = data_dir()
    if not base.is_dir():
        return {"schema": SCHEMA, "source": "unavailable",
                "reason": f"没有日历目录（{base}）：先跑 scripts/fetch_trading_calendar.py "
                          "或用 import_file 导入",
                "sessions": [], "unavailable": [{"what": "sessions", "reason": "无日历目录"}]}
    dirs = ([base / dataset_id] if dataset_id else
            sorted([d for d in base.iterdir() if (d / "meta.json").is_file()],
                   key=lambda d: (d / "meta.json").stat().st_mtime, reverse=True))
    for d in dirs:
        mp = d / "meta.json"
        if not mp.is_file():
            continue
        meta = json.loads(mp.read_text(encoding="utf-8"))
        dates, err = _read_rows(d / "sessions.csv")
        if err or not dates:
            continue
        return {"schema": SCHEMA, "dataset_id": meta.get("dataset_id"),
                "source": meta.get("source"), "license": meta.get("license"),
                "market": meta.get("market"), "imported_at": meta.get("imported_at"),
                "range": meta.get("range"), "rows": int(meta.get("rows") or len(dates)),
                "sessions": sorted(set(dates)), "unavailable": []}
    return {"schema": SCHEMA, "source": "unavailable", "reason": "没有可用日历数据集",
            "sessions": [], "unavailable": [{"what": "sessions", "reason": "没有可用日历"}]}


# ---------------------------------------------------------------- 查询（覆盖区间外一律 unknown）

def ok(payload: dict | None) -> bool:
    """有没有**可用**日历。必须返回真正的 `bool` —— Python 的 `and` 会返回最后一个操作数，
    早先版本因此把整份 `sessions` 列表当"真值"返回，一路被写进读数与证据 JSON（文件莫名暴涨）。"""
    return bool(payload and payload.get("source") != "unavailable" and payload.get("sessions"))


def _set(payload: dict | None):
    return set(payload.get("sessions") or []) if ok(payload) else set()


def covers(payload: dict | None, date: str) -> bool:
    """该日期是否落在日历**覆盖区间内**（区间外不能下"休市"结论）。"""
    if not ok(payload):
        return False
    lo, hi = (payload.get("range") or [None, None])[:2]
    d = _norm_date(date)
    return bool(d and lo and hi and str(lo) <= d <= str(hi))


def is_session(payload: dict | None, date: str) -> bool | None:
    """交易日 → True/False；**覆盖区间外或没有日历 ⇒ None（unknown）**。"""
    d = _norm_date(date)
    if not d or not covers(payload, d):
        return None
    return d in _set(payload)


def next_session(payload: dict | None, date: str) -> str | None:
    """严格晚于 `date` 的第一个交易日（区间外为 None）。"""
    d = _norm_date(date)
    if not ok(payload) or not d:
        return None
    for s in payload["sessions"]:
        if s > d:
            return s
    return None


def prev_session(payload: dict | None, date: str) -> str | None:
    """**早于或等于** `date` 的最后一个交易日。"""
    d = _norm_date(date)
    if not ok(payload) or not d:
        return None
    out = None
    for s in payload["sessions"]:
        if s <= d:
            out = s
        else:
            break
    return out


def session_at_offset(payload: dict | None, anchor: str, offset: int) -> str | None:
    """以 `anchor` 为第 0 个交易日、按**日历**取第 `offset` 个交易日（可负）。"""
    d = _norm_date(anchor)
    if not ok(payload) or not d:
        return None
    sess = payload["sessions"]
    try:
        i = sess.index(d)
    except ValueError:
        return None
    j = i + int(offset)
    return sess[j] if 0 <= j < len(sess) else None


def sessions_between(payload: dict | None, d1: str, d2: str) -> int | None:
    """`(d1, d2]` 之间的交易日数（左开右闭）；覆盖不足 ⇒ None。"""
    a, b = _norm_date(d1), _norm_date(d2)
    if not ok(payload) or not a or not b or not (covers(payload, a) and covers(payload, b)):
        return None
    lo, hi = (a, b) if a <= b else (b, a)
    return sum(1 for s in payload["sessions"] if lo < s <= hi)


def suspend_dates(payload: dict | None, subject_dates, d1: str, d2: str) -> dict:
    """**确定性**判定：区间内"日历是交易日但标的不存在"的日期＝停牌/缺数据。

    与 `event_returns` 的 `suspension_suspect` 不同，这里在**日历覆盖完整**时是**结论**而非疑似；
    覆盖不足时如实降级并写原因（不把"日历太旧"说成"停牌"）。
    """
    if not ok(payload):
        return {"ok": False, "reason": "没有交易日历：无法把停牌与休市分开",
                "sessions": [], "suspension_dates": [], "unavailable": True}
    a, b = _norm_date(d1), _norm_date(d2)
    if not (covers(payload, a) and covers(payload, b)):
        return {"ok": False, "reason": f"日历覆盖 {payload.get('range')} 不含 {a}~{b}："
                                       "区间外不下'停牌'结论",
                "sessions": [], "suspension_dates": [], "unavailable": True}
    have = {_norm_date(x) for x in (subject_dates or [])}
    holes = [s for s in payload["sessions"] if a <= s <= b and s not in have]
    return {"ok": True, "reason": "", "range": [a, b], "compared_sessions":
            sum(1 for s in payload["sessions"] if a <= s <= b),
            "suspension_dates": holes, "unavailable": False,
            "basis": "日历判定为交易日、但标的当日无行情行 ⇒ 停牌或该标的缺数据"}
